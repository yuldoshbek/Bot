"""Вопрос своими словами: «что просрочено в финансах за месяц».

**Модель возвращает структуру, а не запрос.** Это главное правило сценария,
и оно же — третья граница блока. Отдать модели сочинение SQL значит отдать ей
и права: условие видимости, попавшее в тот же текст запроса, обходится
формулировкой вопроса, а не взломом. Здесь от модели приходят несколько
значений, каждое сверяется с заранее известным перечнем, а запрос собирает код.

**Поля перечислены заранее.** Всё, чего нет в `FIELDS`, отбрасывается ещё
до разбора — вместе с полем `sql`, полем `limit` и любым другим, которое модель
однажды придумает. Проверять, «не опасное ли оно», означало бы гадать;
белый список не гадает.

**Права применяются после разбора.** Разбор ничего не знает о том, кто
спрашивает: он превращает слова в значения. Кому что видно, решает
`app/services/questions.py` теми же условиями, что и списки в боте.

**Непонятый вопрос откатывается к поиску по словам.** Ответить на непонятое
всей организацией хуже, чем честно поискать по буквам: первое выглядит как
ответ, второе — как поиск. Поэтому фильтр без единого условия ответом
не считается, и по тому же пути идёт вопрос при выключенном ИИ.
"""
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import gate
from app.ai.prompts import ASK
from app.core.timeutil import utcnow
from app.models.user import User
from app.services import questions
from app.services import search as search_service
from app.services.rbac import Grant
from app.services.search import Hit

log = logging.getLogger("seta.ai.question")

PROMPT_VERSION = ASK.version

# Поля, которые принимаются от модели. Перечень заранее — единственный способ
# не зависеть от её фантазии: `sql`, `limit`, `organization_id` и всё прочее
# отбрасывается молча, не доходя ни до проверки значений, ни до запроса.
FIELDS = ("kind", "status", "priority", "overdue", "person", "department", "period")

# Длина одного присланного значения. Название отдела длиной в килобайт — это
# не название отдела, и укорачивать его до осмысленного нечем.
MAX_VALUE = 200

# Длина вопроса. Всё, что длиннее, — уже не вопрос, а пересланная переписка.
MAX_QUESTION = 500


@dataclass(slots=True)
class Answer:
    """Ответ на вопрос — и то, как он получен.

    `searched` означает, что вопрос разобрать не вышло и сработал обычный
    поиск по словам. Это не ошибка, но человек должен видеть разницу: иначе
    поиск по буквам выглядит как понятый вопрос.
    """

    hits: list[Hit] = field(default_factory=list)
    filter: questions.Filter | None = None
    searched: bool = False
    more: bool = False
    # Почему разобрать не вышло: off, provider, empty, vague. Для показа и проверок.
    reason: str = ""
    call_id: int | None = None

    @property
    def empty(self) -> bool:
        return not self.hits


def payload(raw: str) -> dict:
    """Достаёт значения из ответа модели. Только перечисленные поля.

    Модель то оборачивает JSON в тройные кавычки, то предваряет пояснением.
    Разбирается и то и другое, но содержимое просеивается одинаково: поле
    не из списка не попадает в результат ни при каких обстоятельствах.
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

    fields: dict = {}
    for key in FIELDS:
        value = data.get(key)
        if key == "overdue":
            # Единственное «да/нет» в структуре, и разбирается оно отдельно.
            # Модели то и дело присылают его строкой, а `bool("false")` — это
            # истина: вопрос «что просрочено» получил бы ответ наоборот.
            if isinstance(value, bool):
                fields[key] = value
            elif isinstance(value, str) and value.strip().lower() in ("true", "false"):
                fields[key] = value.strip().lower() == "true"
            continue
        if isinstance(value, bool):
            # Остальные поля — строки. Булево значение в них стало бы
            # условием вида «статус True», то есть мусором.
            continue
        if isinstance(value, (str, int, float)):
            cleaned = str(value).strip()[:MAX_VALUE]
            if cleaned:
                fields[key] = cleaned
    return fields


async def parse(
    session: AsyncSession,
    text: str,
    *,
    viewer: User,
    grants: dict[str, Grant],
) -> tuple[questions.Filter | None, str, int | None]:
    """Спрашивает модель и превращает ответ в проверенный фильтр.

    Возвращает (фильтр, причина отказа, строка журнала). Фильтр — `None`,
    если модель не ответила или ответила ничем: пустой фильтр означал бы
    «покажи всё», а это не ответ на вопрос.
    """
    asked = (text or "").strip()[:MAX_QUESTION]
    if not asked:
        return None, "empty", None

    outcome = await gate.ask(
        session,
        organization_id=viewer.organization_id,
        kind="ask",
        system=ASK.system,
        user=asked,
        prompt_version=PROMPT_VERSION,
        user_id=viewer.id,
    )
    if not outcome.worked:
        return None, outcome.reason or "empty", outcome.call_id

    fields = payload(outcome.text)
    if not fields:
        return None, "empty", outcome.call_id

    item = await questions.resolve(session, fields, viewer=viewer, grants=grants)
    if not item.narrow and item.possible:
        # Условий не набралось. «Все поручения организации» — не ответ
        # на вопрос, а последствие непонимания, и выглядит оно убедительно.
        return None, "vague", outcome.call_id
    return item, "", outcome.call_id


async def answer(
    session: AsyncSession,
    text: str,
    *,
    viewer: User,
    grants: dict[str, Grant],
    now: datetime | None = None,
) -> Answer:
    """Отвечает на вопрос. Не разобрав — ищет по словам, а не отказывает.

    Порядок здесь и есть свойство «выключенный ИИ ничего не ломает»: при
    `AI_ENABLED=false` вызова не происходит вовсе, а человек всё равно
    получает результат — тот же, что дала бы кнопка поиска.
    """
    item, reason, call_id = await parse(session, text, viewer=viewer, grants=grants)
    if item is None:
        return await _by_words(session, text, viewer=viewer, grants=grants, reason=reason)

    found = await questions.run(
        session, item, viewer=viewer, grants=grants, now=now or utcnow()
    )
    return Answer(
        hits=found.hits, filter=item, more=found.more, call_id=call_id
    )


async def _by_words(
    session: AsyncSession,
    text: str,
    *,
    viewer: User,
    grants: dict[str, Grant],
    reason: str,
) -> Answer:
    """Откат к обычному поиску: те же условия видимости, другой способ искать."""
    results = await search_service.search(
        session, user=viewer, grants=grants, query=text or ""
    )
    hits = (
        results.tasks + results.decisions + results.meetings
        + results.documents + results.people
    )
    return Answer(hits=hits[: questions.MAX_ROWS], searched=True, reason=reason)
