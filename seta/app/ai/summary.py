"""Утренняя сводка словами: те же цифры, но с расставленными акцентами.

Сводка с цифрами уже есть, и она остаётся основной. Модель добавляет сверху
несколько строк о том, с чего начать день. Выключенный ИИ убирает эти строки
и не трогает больше ничего — письмо уходит слово в слово прежнее.

**Ни одной цифры от модели.** Числа она не пишет вовсе: расставляет метки,
значения подставляет код. Само правило — общее для всех сценариев и живёт
в `app/ai/tokens.py`; здесь только границы вступления и список меток.

**Кириллица выводится правилом.** Модель пишет по-узбекски латиницей, а
письменность меняет тот же `to_cyrillic`, что и весь остальной интерфейс.
Просить у модели кириллицу означало бы завести второй источник узбекского
письма рядом с тем, который уже проверен.

**Вступление пишется раз в сутки, в фоновом цикле.** На экран «Мой день»
оно не идёт: экран открывают десятки раз в день, и каждое открытие стоило бы
денег ради строк, которые человек уже прочитал утром.
"""
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import gate, tokens
from app.ai.prompts import MORNING_DIGEST
from app.core.i18n import DERIVED_LOCALE, normalize
from app.core.timeutil import to_local
from app.core.translit import to_cyrillic
from app.models.user import User
from app.services.dashboard import Board

log = logging.getLogger("seta.ai.summary")

PROMPT_VERSION = MORNING_DIGEST.version

# Границы годного вступления. Меньше четырёх строк — это не акценты, а подпись;
# больше семи — второй экран поверх первого, и его перестанут читать.
MIN_LINES = 3
MAX_LINES = 8
MAX_CHARS = 900

# Что означает каждая метка. Список закрытый: метку, которой здесь нет,
# модель придумала, и такой ответ негоден целиком.
MEANING: dict[str, str] = {
    "meetings": "встреч сегодня",
    "next_at": "во сколько ближайшая встреча",
    "now_until": "до скольких идёт встреча, которая сейчас",
    "free_at": "с какого времени первое свободное окно",
    "requests": "заявок на встречу ждут ответа",
    "to_review": "работ ждут проверки",
    "stale": "решений просрочено",
    "overdue": "поручений просрочено",
    "overdue_top": "отдел с наибольшим числом просрочек",
    "overdue_top_count": "сколько просрочек в этом отделе",
    "personal": "просрочек на личном контроле",
}

# На каком языке отвечать. Узбекская кириллица здесь не значится намеренно:
# её выводит правило, а не модель.
LANGUAGE = {
    "ru": "русский",
    "uz": "узбекский латиницей",
}


def facts(board: Board) -> dict[str, str]:
    """Что сегодня известно — метка и посчитанное значение.

    Берётся с того же экрана, который человек увидит следом. Второй источник
    тех же чисел разошёлся бы с первым, и объяснить расхождение было бы нечем.

    Пустые значения не передаются вовсе: «просрочек ноль» модели знать незачем,
    а перечисленный ноль она непременно упомянет.
    """
    values: dict[str, str] = {}
    tz = board.timezone

    if board.meetings_today:
        values["meetings"] = str(board.meetings_today)
    if board.running:
        values["now_until"] = f"{to_local(board.running[0].end_at, tz):%H:%M}"
    if board.ahead:
        values["next_at"] = f"{to_local(board.ahead[0].start_at, tz):%H:%M}"
    if board.free_slot:
        values["free_at"] = f"{to_local(board.free_slot.start, tz):%H:%M}"

    if board.requests_waiting:
        values["requests"] = str(board.requests_waiting)
    if board.to_review:
        values["to_review"] = str(board.to_review)
    if board.stale_decisions:
        values["stale"] = str(board.stale_decisions)

    if board.overdue_total:
        values["overdue"] = str(board.overdue_total)
        if board.overdue_by_department:
            name, count = board.overdue_by_department[0]
            if name:
                values["overdue_top"] = name
                values["overdue_top_count"] = str(count)
    if board.personal_overdue:
        values["personal"] = str(len(board.personal_overdue))
    return values


def ask_text(values: dict[str, str], locale: str | None) -> str:
    """Вопрос модели: язык ответа и список меток со значениями."""
    language = LANGUAGE.get(normalize(locale), LANGUAGE["uz"])
    lines = [f"Язык ответа: {language}.", "", "Метки и значения:"]
    lines += [
        f"  {{{token}}} = {value} — {MEANING[token]}"
        for token, value in values.items()
        if token in MEANING
    ]
    return "\n".join(lines)


def usable(raw: str, values: dict[str, str]) -> str | None:
    """Границы вступления. Само правило — общее, в `app/ai/tokens.py`."""
    return tokens.usable(
        raw, values,
        min_lines=MIN_LINES, max_lines=MAX_LINES, max_chars=MAX_CHARS,
    )


def fill(text: str, values: dict[str, str]) -> str:
    """Подстановка значений — тем же правилом, что и во всех сценариях."""
    return tokens.fill(text, values)


async def accents(session: AsyncSession, board: Board, viewer: User) -> str:
    """Вступление к сводке. Пустая строка — вступления нет, и это не ошибка.

    Ни один отказ модели не отменяет письма: сводка уходит в любом случае,
    просто без первых строк.
    """
    values = facts(board)
    if not values:
        return ""

    locale = normalize(viewer.locale)
    outcome = await gate.ask(
        session,
        organization_id=viewer.organization_id,
        kind="digest",
        system=MORNING_DIGEST.system,
        user=ask_text(values, locale),
        prompt_version=PROMPT_VERSION,
    )
    if not outcome.worked:
        return ""

    text = usable(outcome.text, values)
    if text is None:
        log.warning("вступление к сводке отвергнуто: %r", outcome.text[:120])
        return ""

    if locale == DERIVED_LOCALE:
        # Письменность меняется правилом, метки его переживают: разбор
        # пропускает всё, что стоит в фигурных скобках.
        text = to_cyrillic(text)
    return fill(text, values)
