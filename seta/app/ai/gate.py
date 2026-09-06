"""Единственная дверь к языковой модели. Через неё проходят все сценарии.

Здесь живут три из четырёх границ, а четвёртая — в `provider.py`:

* **ИИ не пишет в базу.** Отсюда возвращается текст, и только текст. Ни одна
  функция этого модуля не принимает сессию для записи чего-либо, кроме
  собственного журнала. Записать поручение по ответу модели можно лишь
  отдельным осознанным действием — и после подтверждения человеком.
* **Бюджет с жёстким потолком.** Расход считается до вызова. Не хватает —
  вызова не происходит вовсе.
* **Журнал.** Строка создаётся до обращения и дописывается после.

**Почему расход пишется до ответа.** Оборвавшийся вызов оплачен поставщиком
так же, как удавшийся. Если писать по факту успеха, потолок обходится
повторными обрывами: каждый стоит денег и ни один не учтён.

**Почему `Outcome`, а не исключение.** Отказ ИИ — обычное дело: выключен,
бюджет исчерпан, поставщик молчит. Сценарий обязан продолжить работу без него,
а не ловить исключение в каждом месте. Исключение здесь означало бы, что
падение ИИ ломает функцию, — ровно то, чего блок не допускает.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.local import Local, Mixed
from app.ai.provider import Answer, Fake, Heard, Provider, ProviderError
from app.core.config import settings
from app.core.timeutil import utcnow
from app.models.ai import AiCall

log = logging.getLogger("seta.ai")

# Поставщик по умолчанию — подставной. Настоящий ставится при запуске, если
# ИИ включён и ключ задан. Так система поднимается без ключа и без сети.
_provider: Provider = Fake()


def use(provider: Provider) -> None:
    """Ставит поставщика. Вызывается при запуске и в проверках."""
    global _provider
    _provider = provider


def current() -> Provider:
    return _provider


def configure() -> Provider:
    """Собирает поставщика по настройкам. Вызывается при запуске.

    Речь и текст разведены намеренно: своя служба расшифровки работает
    и без ключа OpenAI, а текстовая модель без ключа не работает вовсе.
    Поэтому голосовое читается в тексте даже при `AI_ENABLED=false` —
    это разные умения и разные деньги.
    """
    text: Provider = Fake()
    if settings.stt_url:
        use(Mixed(voice=Local(settings.stt_url), text=text))
    else:
        use(text)
    log.info("поставщик ИИ: %s", current().name)
    return current()


def hearing() -> bool:
    """Есть ли кому слушать речь.

    Своя служба слушает всегда; платная — только при включённом ИИ.
    """
    return bool(settings.stt_url) or settings.ai_enabled


@dataclass(slots=True)
class Outcome:
    """Что вышло. `text` пуст, если ИИ не сработал — это не ошибка."""

    text: str = ""
    ok: bool = False
    # Почему не сработало: off, budget, provider, empty. Для журнала и проверок.
    reason: str = ""
    call_id: int | None = None
    cost_usd: float = 0.0
    # Расшифровка целиком, с разметкой по времени: по паузам текст разбивается
    # на абзацы. Для текстовых вызовов остаётся пустой.
    heard: Heard | None = None

    @property
    def worked(self) -> bool:
        return self.ok and bool(self.text)


async def spent(
    session: AsyncSession, organization_id: int, *, since: datetime
) -> float:
    """Сколько потрачено с указанного момента."""
    total = await session.scalar(
        select(func.coalesce(func.sum(AiCall.cost_usd), 0)).where(
            AiCall.organization_id == organization_id,
            AiCall.started_at >= since,
        )
    )
    return float(total or 0)


async def budget_left(
    session: AsyncSession, organization_id: int, *, now: datetime | None = None
) -> tuple[bool, str]:
    """Есть ли ещё бюджет. Возвращает (можно, причина отказа).

    Оба потолка проверяются, а не один: дневной защищает от заклинившего
    цикла, месячный — от ровного, но слишком дорогого расхода, который
    дневной предел пропускает каждый день по чуть-чуть.
    """
    now = now or utcnow()
    day = await spent(session, organization_id, since=now - timedelta(days=1))
    if day >= settings.ai_daily_budget_usd:
        return False, "дневной предел"
    month = await spent(session, organization_id, since=now - timedelta(days=30))
    if month >= settings.ai_monthly_budget_usd:
        return False, "месячный предел"
    return True, ""


async def ask(
    session: AsyncSession,
    *,
    organization_id: int,
    kind: str,
    system: str,
    user: str,
    prompt_version: str,
    model: str | None = None,
    user_id: int | None = None,
    needs_confirmation: bool = False,
) -> Outcome:
    """Спрашивает модель. Никогда не бросает исключение наружу.

    Порядок важен: выключен → бюджет → журнал → вызов. Проверка бюджета стоит
    до создания строки журнала, иначе сам отказ по бюджету плодил бы строки
    и запись о нём считалась бы расходом.
    """
    if not settings.ai_enabled:
        return Outcome(reason="off")

    allowed, why = await budget_left(session, organization_id)
    if not allowed:
        # Не «вызвали и выбросили», а не вызвали вовсе: за обращение платят
        # в момент обращения, и потолок обязан останавливать до него.
        log.warning("ИИ остановлен: %s", why)
        return Outcome(reason="budget")

    chosen = model or settings.ai_model_routine
    call = AiCall(
        organization_id=organization_id,
        user_id=user_id,
        kind=kind,
        model=chosen,
        prompt_version=prompt_version,
        confirmed=None if not needs_confirmation else False,
    )
    session.add(call)
    # Строка обязана существовать до обращения: оборвавшийся вызов оплачен
    # так же, как удавшийся, и должен попасть в расход.
    await session.flush()

    try:
        answer: Answer = await current().ask(
            system=system, user=user, model=chosen
        )
    except ProviderError as error:
        call.finished_at = utcnow()
        call.ok = False
        call.error = str(error)[:2000]
        await session.flush()
        log.warning("ИИ не ответил: %s", error)
        return Outcome(reason="provider", call_id=call.id)

    call.finished_at = utcnow()
    call.ok = True
    call.tokens_in = answer.tokens_in
    call.tokens_out = answer.tokens_out
    call.cost_usd = answer.cost_usd
    await session.flush()

    text = (answer.text or "").strip()
    if not text:
        # Пустой ответ — не успех: сценарий должен показать своё, а не пустоту.
        return Outcome(reason="empty", call_id=call.id, cost_usd=answer.cost_usd)

    return Outcome(
        text=text, ok=True, call_id=call.id, cost_usd=answer.cost_usd
    )


async def transcribe(
    session: AsyncSession,
    audio: bytes,
    *,
    organization_id: int,
    user_id: int | None = None,
    hint: str = "",
) -> Outcome:
    """Расшифровывает речь. Отдельно от разбора: это разные модели и разные цены.

    Бюджет проверяется только для платной расшифровки. Своя служба денег
    не стоит, и останавливать её из-за исчерпанного потолка текстовой модели
    значило бы выключать бесплатное вместе с платным.
    """
    if not hearing():
        return Outcome(reason="off")

    speaker = current()
    if not getattr(speaker, "free_voice", False):
        allowed, why = await budget_left(session, organization_id)
        if not allowed:
            log.warning("ИИ остановлен: %s", why)
            return Outcome(reason="budget")

    call = AiCall(
        organization_id=organization_id,
        user_id=user_id,
        kind="voice_transcribe",
        # Какой моделью слушали. У своей службы имя приходит в ответе
        # и дописывается ниже: журнал должен знать, кто именно расшифровал.
        model=speaker.name if speaker.free_voice else settings.ai_model_voice,
        prompt_version="-",
    )
    session.add(call)
    await session.flush()

    try:
        heard: Heard = await speaker.transcribe(
            audio, model=settings.ai_model_voice, hint=hint
        )
    except ProviderError as error:
        call.finished_at = utcnow()
        call.ok = False
        call.error = str(error)[:2000]
        await session.flush()
        return Outcome(reason="provider", call_id=call.id)

    call.finished_at = utcnow()
    call.ok = True
    call.cost_usd = heard.cost_usd
    if heard.model:
        call.model = heard.model[:64]
    await session.flush()

    text = (heard.text or "").strip()
    if not text:
        return Outcome(reason="empty", call_id=call.id, cost_usd=heard.cost_usd)
    return Outcome(
        text=text, ok=True, call_id=call.id, cost_usd=heard.cost_usd, heard=heard
    )


async def mark_confirmed(
    session: AsyncSession, call_id: int | None, *, confirmed: bool
) -> None:
    """Отмечает, чем кончилось предложение модели.

    Единственный способ потом ответить на вопрос «система сама завела поручение
    или человек подтвердил». Без этой отметки журнал показывает только расход.
    """
    if call_id is None:
        return
    call = await session.get(AiCall, call_id)
    if call is not None:
        call.confirmed = confirmed
        await session.flush()
