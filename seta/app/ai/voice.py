"""Голосовое поручение: наговорил — увидел черновик — подтвердил.

Сценарий целиком построен вокруг одного правила блока: **ИИ предлагает,
система решает и записывает.** Отсюда всё остальное.

**Расшифровка отдельно от разбора.** Речь в текст переводит одна модель,
формулировку разбирает другая — это разные цены, разные отказы и разные
причины ошибиться. Слитый в один вызов сценарий нельзя ни удешевить,
ни починить по частям: непонятно, кто именно не справился.

**Модель называет фрагмент речи, а не дату.** На вопрос о сроке она обязана
вернуть «завтра», «juma gacha», «через три дня» — то, что прозвучало, — а
превращает это в дату `parse_due`, тот же самый разбор, что и при наборе
руками. Разрешить модели вычислять дату значило бы завести второе описание
сроков, и оно разошлось бы с первым молча: сначала на переходе через полночь,
потом на часовых поясах.

**Исполнителя выбирает система.** Модель слышит имя. Найти по нему человека,
убедиться, что он в этой организации и что этому руководителю вообще
позволено ему поручать, — работа кода: `find_assignee` и `may_assign_to`,
те же, что и в ручном вводе. Модель про права не знает и знать не должна:
право, о котором спрашивают модель, обходится формулировкой.

**Черновик — не поручение.** Он не лежит в таблице поручений ни в каком
статусе, а живёт в состоянии диалога. Статус «черновик» в общей таблице
рано или поздно попал бы в списки, в счётчики просрочек и в выгрузки —
и объяснять, почему в отчёте поручения, которых никто не давал, пришлось бы
долго. Строка появляется ровно в момент подтверждения, обычным `create_task`.

**Право проверяется дважды, и считается вторая проверка.** Первая — чтобы
показать честный черновик; вторая, при записи, — чтобы между показом и
нажатием кнопки ничего не изменилось: человека могли перевести в другой
отдел или уволить.

Сам промпт и его версия лежат в `prompts.py` — там же, где промпты
остальных сценариев: их читают все вместе.
"""
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import gate
from app.ai.prompts import VOICE_HINT, VOICE_TASK
from app.core.dates import humanize_due, parse_due
from app.core.i18n import t
from app.core.text import cut, esc
from app.models.enums import Priority
from app.models.task import Task
from app.models.user import User
from app.services import tasks as task_service
from app.services.rbac import Grant

log = logging.getLogger("seta.ai.voice")

# Промпт и его версия живут в `prompts.py`: их читают все вместе, а не
# поодиночке, и переводу на языки интерфейса они не подлежат.
PROMPT_VERSION = VOICE_TASK.version

# Предел длины голосового. Не косметика: расшифровка стоит за минуту,
# и одна сорокаминутная запись съедает дневной бюджет целиком.
MAX_SECONDS = 180

# Поля, которые принимаются от модели. Перечень заранее — единственный способ
# не зависеть от её фантазии: всё остальное отбрасывается молча.
FIELDS = ("title", "assignee", "due", "priority")

PRIORITIES: dict[str, Priority] = {
    "low": Priority.LOW,
    "normal": Priority.NORMAL,
    "high": Priority.HIGH,
    "critical": Priority.CRITICAL,
}

@dataclass(slots=True)
class Draft:
    """Предложение, а не запись. В базе поручений ему соответствия нет."""

    transcript: str
    title: str
    assignee_id: int | None = None
    heard_name: str = ""
    due_at: datetime | None = None
    due_phrase: str = ""
    priority: Priority = Priority.NORMAL
    # Строка журнала: по ней потом видно, подтвердил человек или отказался.
    call_id: int | None = None
    # Ключи пояснений — что не сошлось. Ключи, а не готовые строки: черновик
    # переживает перезапуск в хранилище состояния, а язык берётся при показе.
    notes: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """Готов ли к записи. Поручения без исполнителя не бывает."""
        return self.assignee_id is not None and bool(self.title.strip())

    def to_state(self) -> dict:
        """В хранилище состояния кладутся только простые значения."""
        return {
            "transcript": self.transcript,
            "title": self.title,
            "assignee_id": self.assignee_id,
            "heard_name": self.heard_name,
            "due_at": self.due_at.isoformat() if self.due_at else None,
            "due_phrase": self.due_phrase,
            "priority": str(self.priority),
            "call_id": self.call_id,
            "notes": list(self.notes),
        }

    @classmethod
    def from_state(cls, data: dict | None) -> "Draft | None":
        """Восстанавливает черновик. Испорченное хранилище — не повод падать."""
        if not isinstance(data, dict) or not data.get("transcript"):
            return None
        try:
            due_raw = data.get("due_at")
            return cls(
                transcript=str(data["transcript"]),
                title=str(data.get("title", "")),
                assignee_id=data.get("assignee_id"),
                heard_name=str(data.get("heard_name", "")),
                due_at=datetime.fromisoformat(due_raw) if due_raw else None,
                due_phrase=str(data.get("due_phrase", "")),
                priority=Priority(data.get("priority", Priority.NORMAL)),
                call_id=data.get("call_id"),
                notes=[str(note) for note in data.get("notes", [])],
            )
        except (TypeError, ValueError):
            return None


def payload(raw: str) -> dict[str, str]:
    """Достаёт из ответа модели перечисленные поля — и только их.

    Ответ не по форме не считается ошибкой: модель ошибается регулярно, и
    исключение здесь означало бы, что кривой ответ ломает сценарий. Пустой
    словарь наверху превращается в черновик из голого текста.
    """
    if not raw:
        return {}
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    fields: dict[str, str] = {}
    for key in FIELDS:
        value = data.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            cleaned = str(value).strip()[:400]
            if cleaned:
                fields[key] = cleaned
    return fields


async def listen(
    session: AsyncSession, audio: bytes, *, creator: User
) -> gate.Outcome:
    """Речь в текст. Отдельным вызовом и отдельной моделью.

    Живёт здесь, а не в обработчике: какой моделью слушать и с какой подсказкой
    — свойство сценария, а не экрана. Обработчику остаётся показать результат.
    """
    return await gate.transcribe(
        session,
        audio,
        organization_id=creator.organization_id,
        user_id=creator.id,
        hint=VOICE_HINT,
    )


async def draft(
    session: AsyncSession,
    transcript: str,
    *,
    creator: User,
    grants: dict[str, Grant],
    now: datetime | None = None,
) -> Draft:
    """Строит черновик по расшифровке. Поручений при этом не появляется.

    `now` подставляется явно в проверках: срок из речи считается от точки
    отсчёта, и проверка обязана уметь её задать.
    """
    spoken = (transcript or "").strip()
    outcome = await gate.ask(
        session,
        organization_id=creator.organization_id,
        kind="voice_task",
        system=VOICE_TASK.system,
        user=spoken,
        prompt_version=PROMPT_VERSION,
        user_id=creator.id,
        needs_confirmation=True,
    )
    fields = payload(outcome.text) if outcome.worked else {}

    item = Draft(
        transcript=spoken,
        title=cut(fields.get("title") or spoken, 300),
        heard_name=fields.get("assignee", ""),
        due_phrase=fields.get("due", ""),
        priority=PRIORITIES.get(fields.get("priority", "").lower(), Priority.NORMAL),
        call_id=outcome.call_id,
    )
    if not fields:
        # Модель молчит, отключена или ответила не по форме. Речь при этом
        # не пропадает: текст становится названием, срок считает тот же разбор.
        item.notes.append("voice.note.raw")

    await _resolve_assignee(session, item, creator=creator, grants=grants)
    _resolve_due(item, creator=creator, now=now)
    return item


async def _resolve_assignee(
    session: AsyncSession, item: Draft, *, creator: User, grants: dict[str, Grant]
) -> None:
    """Ищет названного человека и проверяет право поручать ему.

    Отвергает система, а не модель. Проверка та же, что в ручном вводе:
    два описания одного права разошлись бы, и разошлись бы именно там,
    где право `task.create` есть у всех, а область у каждого своя.
    """
    name = item.heard_name.strip()
    if not name:
        item.notes.append("voice.note.no_assignee")
        return

    found = await task_service.find_assignee(session, creator.organization_id, name)
    if not found:
        item.notes.append("voice.note.assignee_unknown")
        return
    if len(found) > 1:
        item.notes.append("voice.note.assignee_many")
        return

    person = found[0]
    if not await task_service.may_assign_to(
        session, actor=creator, grants=grants, assignee=person
    ):
        item.notes.append("voice.note.assignee_denied")
        return

    item.assignee_id = person.id


def _resolve_due(item: Draft, *, creator: User, now: datetime | None = None) -> None:
    """Считает срок тем же разбором, что и при наборе руками.

    Сначала по выделенному фрагменту, потом по всей фразе: модель могла
    не выделить срок, но сказан он был — и `parse_due` найдёт его сам.
    """
    item.due_at = parse_due(item.due_phrase, creator.timezone, now=now)
    if item.due_at is None:
        item.due_at = parse_due(item.transcript, creator.timezone, now=now)
    if item.due_at is None:
        item.notes.append("voice.note.no_due")


async def confirm(
    session: AsyncSession,
    item: Draft,
    *,
    creator: User,
    grants: dict[str, Grant],
    on_behalf_of_id: int | None = None,
) -> Task:
    """Единственное место, где черновик становится поручением.

    Расшифровка попадает в описание: спорить о формулировке потом будут с ней,
    а не с пересказом модели.
    """
    if not item.ready:
        raise task_service.TaskError(t("voice.err.not_ready", creator.locale))

    assignee = await session.get(User, item.assignee_id)
    if assignee is None:
        raise task_service.TaskError(t("task.new.executor_not_found", creator.locale))

    # Вторая проверка права — та, что защищает данные. Первая была для показа.
    if not await task_service.may_assign_to(
        session, actor=creator, grants=grants, assignee=assignee
    ):
        raise task_service.TaskError(t("task.new.cannot_assign", creator.locale))

    task = await task_service.create_task(
        session,
        creator=creator,
        assignee=assignee,
        title=item.title,
        description=item.transcript if item.transcript != item.title else None,
        due_at=item.due_at,
        priority=item.priority,
        on_behalf_of_id=on_behalf_of_id,
    )
    await gate.mark_confirmed(session, item.call_id, confirmed=True)
    return task


async def decline(session: AsyncSession, item: Draft) -> None:
    """Отказ. Ничего не создаётся, но в журнале он виден.

    Отметка нужна не для статистики: по ней потом отвечают на вопрос
    «система сама завела поручение или человек подтвердил».
    """
    await gate.mark_confirmed(session, item.call_id, confirmed=False)


async def pick(
    session: AsyncSession,
    item: Draft,
    *,
    person_id: int,
    creator: User,
    grants: dict[str, Grant],
) -> bool:
    """Ставит исполнителя, выбранного человеком из списка.

    Проверка права здесь не формальность: кнопку могли нажать старую,
    а список — собрать до перевода человека в другой отдел.
    """
    person = await session.get(User, person_id)
    if person is None:
        return False
    if not await task_service.may_assign_to(
        session, actor=creator, grants=grants, assignee=person
    ):
        return False
    item.assignee_id = person.id
    item.heard_name = person.full_name
    item.notes = [
        note for note in item.notes if not note.startswith("voice.note.assignee")
        and note != "voice.note.no_assignee"
    ]
    return True


def render(item: Draft, locale: str | None, *, assignee_name: str = "",
           timezone_name: str | None = None) -> str:
    """Карточка черновика на языке смотрящего.

    Первой строкой — что это ещё не поручение. Человек нажимает «Подтвердить»
    по тому, что прочитал, и прочитать он должен именно это.
    """
    from app.services.tasks import priority_title

    lines = [
        f"<b>{t('voice.draft.title', locale)}</b>",
        t("voice.draft.not_yet", locale),
        "",
        f"📋 {esc(cut(item.title, 200))}",
        f"👤 {esc(assignee_name) if assignee_name else '—'}",
        "⏰ " + (
            humanize_due(item.due_at, timezone_name, locale) if item.due_at
            else t("task.field.no_due", locale)
        ),
        f"🔺 {priority_title(item.priority, locale)}",
    ]
    if item.notes:
        lines.append("")
        lines += [
            "• " + t(note, locale, name=esc(cut(item.heard_name, 60)))
            for note in item.notes
        ]
    if item.transcript and item.transcript != item.title:
        lines += ["", f"<i>{t('voice.draft.heard', locale)}: "
                      f"{esc(cut(item.transcript, 400))}</i>"]
    return "\n".join(lines)
