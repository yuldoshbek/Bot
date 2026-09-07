"""Выводы к недельному отчёту: тренды поверх посчитанных показателей.

Отчёт с числами уже есть, и он остаётся основным. Модель добавляет сверху
несколько строк о том, что изменилось и что из этого следует.

**Старшая модель — только здесь.** Сценарий её не называет: он говорит,
что делает (`weekly_report`), а модель по этому виду вызова выбирает
`app/ai/models.py`. Недельный отчёт пишется раз в неделю на организацию,
и разница в качестве рассуждения тут заметна; во всех остальных сценариях
она не стоила бы своих денег — вызовов там на порядки больше.

**Числа модель не пишет.** Правило общее для всех сценариев и живёт
в `app/ai/tokens.py`: метки расставляет модель, значения подставляет код —
из тех же показателей, которые человек увидит следом.

**Прошлая неделя — тоже метка.** У каждого показателя есть `{ключ}` и
`{ключ}_was`. Иначе модель, рассуждая о тренде, назвала бы прошлое значение
своими словами, и проверить его стало бы нечем.

**Название показателя модель не переписывает.** Оно берётся из словаря
и подставляется меткой, как и число: переведённое моделью «Загрузка календаря»
разошлось бы с тем, что написано строкой ниже в самой таблице.
"""
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import gate, tokens
from app.ai.prompts import WEEKLY_REPORT
from app.core.i18n import DERIVED_LOCALE, normalize, t
from app.core.translit import to_cyrillic
from app.models.user import User
from app.services.weekly import Report

log = logging.getLogger("seta.ai.report")

PROMPT_VERSION = WEEKLY_REPORT.version

# Выводов больше, чем у утренней сводки: неделя — это связи между показателями,
# а связь короче двух предложений не объясняется.
MIN_LINES = 3
MAX_LINES = 9
MAX_CHARS = 1200

LANGUAGE = {"ru": "русский", "uz": "узбекский латиницей"}


def facts(report: Report) -> dict[str, str]:
    """Метки и значения — с того же отчёта, который человек увидит следом."""
    values: dict[str, str] = {}
    for line in report.lines:
        if line.now.value is None:
            continue
        values[line.key] = line.now.shown()
        if line.before is not None and line.before.value is not None:
            values[f"{line.key}_was"] = line.before.shown()
    return values


def ask_text(report: Report, values: dict[str, str], locale: str | None) -> str:
    """Вопрос модели: язык ответа, показатели, значения и прошлые значения."""
    language = LANGUAGE.get(normalize(locale), LANGUAGE["uz"])
    lines = [f"Язык ответа: {language}.", "", "Показатели за неделю:"]
    for line in report.lines:
        if line.key not in values:
            continue
        # Название берём по-русски: это вопрос модели, а не текст для человека.
        title = t(f"metric.{line.key}", "ru")
        past = (
            f", неделей раньше {{{line.key}_was}}"
            if f"{line.key}_was" in values else ""
        )
        lines.append(f"  {title}: {{{line.key}}}{past}")
    return "\n".join(lines)


async def words(session: AsyncSession, report: Report, viewer: User) -> str:
    """Вступление к отчёту. Пустая строка — вступления нет, и это не ошибка."""
    values = facts(report)
    if not values:
        return ""

    locale = normalize(viewer.locale)
    outcome = await gate.ask(
        session,
        organization_id=report.organization_id,
        kind="weekly_report",
        system=WEEKLY_REPORT.system,
        user=ask_text(report, values, locale),
        prompt_version=PROMPT_VERSION,
    )
    if not outcome.worked:
        return ""

    text = tokens.usable(
        outcome.text, values,
        min_lines=MIN_LINES, max_lines=MAX_LINES, max_chars=MAX_CHARS,
    )
    if text is None:
        log.warning("выводы к отчёту отвергнуты: %r", outcome.text[:120])
        return ""

    if locale == DERIVED_LOCALE:
        # Кириллица выводится правилом, как и весь остальной интерфейс.
        text = to_cyrillic(text)
    return tokens.fill(text, values)
