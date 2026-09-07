"""Вопрос своими словами: разобранный фильтр превращается в выборку.

Здесь нет ни одного обращения к модели, и это главное свойство модуля.
Модель работает этажом выше (`app/ai/question.py`) и отдаёт сюда **структуру**:
вид записей, статус, приоритет, названное имя, названный отдел, период.
Всё остальное отбрасывается ещё до этого места.

**Почему структура, а не запрос.** Отдать модели сочинение SQL значит отдать
ей и права: любое условие видимости, вписанное в тот же запрос, обходится
формулировкой вопроса. Здесь запрос собирает код, а от модели приходят только
значения, каждое из которых сверяется с заранее известным перечнем.

**Права применяются после разбора, теми же условиями.** Условия видимости
берутся из тех же `visible_filter`, что и у списков в боте, и добавляются
к запросу всегда — независимо от того, что попросила модель. Спросивший про
чужой отдел получает **пустой ответ, а не отказ**: отказ сам по себе был бы
ответом «там что-то есть».

**Названия превращает система.** Модель слышит «в финансах» и «у Каримова» —
найти отдел и человека, и только внутри этой организации, работа кода.
Названное, но не найденное не отбрасывается: фильтр становится заведомо
пустым (`possible = False`). Отбросить его значило бы ответить шире, чем
спрашивали, — на вопрос «что у Каримова» показать поручения всех.

**Период называет модель, а считает код.** Границы «за месяц» вычисляются
от местного дня спрашивающего — той же арифметикой, что и везде в системе.
Разрешить модели присылать даты значило бы завести второе описание календаря.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import t
from app.core.text import esc
from app.core.timeutil import to_local, to_utc
from app.models.decision import Decision
from app.models.enums import DecisionStatus, MeetingStatus, Priority, TaskStatus
from app.models.meeting import Meeting, MeetingParticipant
from app.models.org import Department
from app.models.task import Task
from app.models.user import User
from app.services import decisions as decision_service
from app.services import meetings as meeting_service
from app.services import tasks as task_service
from app.services.rbac import Grant, visible_department_ids
from app.services.search import Hit

# О чём вообще можно спросить. Список короткий намеренно: каждый вид требует
# своего запроса, своего условия видимости и своей подписи, а вид, о котором
# забыли что-нибудь из трёх, отвечает неправильно молча.
KINDS = ("task", "decision", "meeting")

# Сколько строк показываем. Предел задаёт система, а не модель: попросив
# тысячу, вопрос превратился бы в выгрузку всей базы одним сообщением.
MAX_ROWS = 10

# Сколько однофамильцев готовы принять за одного. Столько же, сколько отдаёт
# поиск исполнителя, — иначе «все Салимовы» означало бы разное в двух местах.
MAX_PEOPLE = 10

# Статусы по видам. Ключ — то, что может прислать модель (нижним регистром),
# значение — то, что лежит в базе. Статус чужого вида отбрасывается: «open»
# у поручения не значит ничего, и притворяться, что значит, нельзя.
STATUSES: dict[str, dict[str, str]] = {
    "task": {item.value.lower(): item.value for item in TaskStatus},
    "decision": {item.value.lower(): item.value for item in DecisionStatus},
    "meeting": {item.value.lower(): item.value for item in MeetingStatus},
}

PRIORITIES: dict[str, str] = {item.value.lower(): item.value for item in Priority}

# Периоды с направлением внутри названия. Направление здесь не случайность:
# «просрочено за месяц» смотрит назад, а «встречи на неделе» — вперёд, и одно
# слово «месяц» без направления отвечало бы на половину вопросов наоборот.
PERIODS = (
    "today",
    "tomorrow",
    "week",
    "month",
    "past_week",
    "past_month",
    "past_quarter",
)

# Сколько дней в каждом периоде и куда они отсчитываются от местного дня.
_SPAN: dict[str, tuple[int, int]] = {
    "today": (0, 1),
    "tomorrow": (1, 2),
    "week": (0, 7),
    "month": (0, 30),
    "past_week": (-7, 1),
    "past_month": (-30, 1),
    "past_quarter": (-90, 1),
}

# По какому столбцу период считается у каждого вида. Столбец один и назван
# здесь, а не выбирается по ходу: «поручения за месяц» — это про срок,
# и человек должен видеть, что понято именно так.
DATE_FIELD = {"task": "ask.by.due", "decision": "ask.by.created", "meeting": "ask.by.start"}


@dataclass(slots=True)
class Filter:
    """Разобранный вопрос. Ни одного значения отсюда не пришло сырым.

    `possible = False` означает: названное в вопросе не нашлось, и ответ обязан
    быть пустым. Это не ошибка и не отказ — так же выглядит вопрос про отдел,
    которого этому человеку не видно.
    """

    kind: str = "task"
    status: str = ""
    priority: str = ""
    overdue: bool = False
    person_ids: list[int] = field(default_factory=list)
    person_names: list[str] = field(default_factory=list)
    department_id: int | None = None
    department_name: str = ""
    period: str = ""
    possible: bool = True
    # Ключи пояснений, а не готовые строки: язык берётся при показе.
    notes: list[str] = field(default_factory=list)

    @property
    def narrow(self) -> bool:
        """Есть ли в вопросе хоть одно условие.

        Фильтр без условий — это не разобранный вопрос, а «покажи всё».
        Отвечать на непонятое всей организацией хуже, чем честно поискать
        по словам, поэтому сценарий на таком фильтре откатывается к поиску.
        """
        return bool(
            self.status
            or self.priority
            or self.overdue
            or self.person_ids
            or self.department_id
            or self.period
        )


@dataclass(slots=True)
class Found:
    """Ответ на вопрос. `more` честно говорит, что показано не всё."""

    hits: list[Hit] = field(default_factory=list)
    more: bool = False

    @property
    def empty(self) -> bool:
        return not self.hits


def window(
    period: str, *, now: datetime, timezone_name: str
) -> tuple[datetime | None, datetime | None]:
    """Границы периода — от местного дня спрашивающего, а не от дня сервера.

    Считает код: модель называет период словом, и на этом её участие в датах
    заканчивается. Позволить ей присылать даты значило бы завести второе
    описание календаря, и разошлось бы оно на переходе через полночь.
    """
    span = _SPAN.get(period)
    if span is None:
        return None, None
    local = to_local(now, timezone_name)
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    since = day + timedelta(days=span[0])
    until = day + timedelta(days=span[1])
    return to_utc(since), to_utc(until)


async def resolve(
    session: AsyncSession, raw: dict, *, viewer: User, grants: dict[str, Grant]
) -> Filter:
    """Превращает присланные моделью значения в проверенный фильтр.

    Ни одно значение не попадает в запрос как есть. Незнакомое отбрасывается
    молча — фантазия модели не должна становиться условием выборки, — а
    названное, но не найденное делает фильтр заведомо пустым.
    """
    kind = str(raw.get("kind", "")).strip().lower()
    item = Filter(kind=kind if kind in KINDS else "task")

    status = str(raw.get("status", "")).strip().lower()
    item.status = STATUSES[item.kind].get(status, "")

    # Приоритет есть только у поручений. У решения и встречи его нет вовсе,
    # и притворяться, что условие применено, нельзя.
    if item.kind == "task":
        item.priority = PRIORITIES.get(str(raw.get("priority", "")).strip().lower(), "")

    # Ровно `True`, а не «похоже на да»: строку «false» приводить к булеву
    # нельзя — она истинна, и вопрос получил бы ответ наоборот.
    item.overdue = raw.get("overdue") is True and item.kind in ("task", "decision")

    period = str(raw.get("period", "")).strip().lower()
    item.period = period if period in PERIODS else ""

    heard_person = str(raw.get("person", "")).strip()
    if heard_person:
        people = await task_service.find_assignee(
            session, viewer.organization_id, heard_person
        )
        people = people[:MAX_PEOPLE]
        if not people:
            # Названного человека в организации нет. Убрать условие значило бы
            # ответить шире вопроса: на «что у Каримова» показать всех.
            item.possible = False
            item.notes.append("ask.note.no_person")
        else:
            item.person_ids = [person.id for person in people]
            item.person_names = [person.full_name for person in people]
            if len(people) > 1:
                item.notes.append("ask.note.many_people")

    heard_department = str(raw.get("department", "")).strip()
    if heard_department:
        department = await _department(session, viewer.organization_id, heard_department)
        if department is None:
            # Пояснения здесь нет намеренно. «Отдел не найден» и «отдел есть,
            # но вам его не видно» обязаны выглядеть одинаково: разные ответы
            # сами по себе сообщали бы, что за стеной что-то есть.
            item.possible = False
        else:
            item.department_id = department.id
            item.department_name = department.name

    return item


async def _department(
    session: AsyncSession, organization_id: int, name: str
) -> Department | None:
    """Отдел по названию — только внутри этой организации.

    Совпадение по вхождению: модель слышит «в финансах», а отдел называется
    «Moliya va buxgalteriya». Шаблонные знаки экранируются — иначе название
    из одного «%» вернуло бы первый попавшийся отдел.
    """
    cleaned = name.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    if not cleaned:
        return None
    return (
        await session.execute(
            select(Department)
            .where(
                Department.organization_id == organization_id,
                func.lower(Department.name).like(f"%{cleaned}%", escape="\\"),
            )
            .order_by(func.length(Department.name), Department.id)
            .limit(1)
        )
    ).scalars().first()


async def run(
    session: AsyncSession,
    item: Filter,
    *,
    viewer: User,
    grants: dict[str, Grant],
    now: datetime,
) -> Found:
    """Выполняет разобранный вопрос. Права — в запросе, а не после него.

    Условия видимости берутся из тех же служб, что отвечают за доступ
    к отдельной записи, и добавляются всегда. Своих условий доступа здесь нет
    ни одного: второе описание одного правила разошлось бы с первым молча.
    """
    if not item.possible:
        # Названо то, чего нет или чего не видно. Пустой ответ, а не отказ.
        return Found()

    visible = await visible_department_ids(session, viewer)
    since, until = window(item.period, now=now, timezone_name=viewer.timezone)

    if item.kind == "decision":
        return await _decisions(
            session, item, viewer=viewer, grants=grants, visible=visible,
            now=now, since=since, until=until,
        )
    if item.kind == "meeting":
        return await _meetings(
            session, item, viewer=viewer, grants=grants, visible=visible,
            since=since, until=until,
        )
    return await _tasks(
        session, item, viewer=viewer, grants=grants, visible=visible,
        now=now, since=since, until=until,
    )


def _cut(rows: list, hits: list[Hit]) -> Found:
    """Отрезает лишнее и честно говорит, что отрезало.

    Запрашивается на строку больше предела: иначе «показано не всё» пришлось бы
    угадывать по совпадению длины с пределом, а ровно десять найденных
    выглядели бы как обрезанные.
    """
    return Found(hits=hits[:MAX_ROWS], more=len(rows) > MAX_ROWS)


async def _tasks(
    session: AsyncSession, item: Filter, *, viewer: User, grants: dict[str, Grant],
    visible: set[int], now: datetime, since: datetime | None, until: datetime | None,
) -> Found:
    where = list(task_service.visible_filter(viewer, grants, visible))
    if item.status:
        where.append(Task.status == item.status)
    if item.priority:
        where.append(Task.priority == item.priority)
    if item.overdue:
        # Правило просрочки одно на всю систему: сводка, контроль сроков
        # и ответ на вопрос обязаны считать одинаково.
        where.extend(task_service.overdue_filter(now))
    if item.person_ids:
        where.append(Task.assignee_id.in_(item.person_ids))
    if item.department_id is not None:
        where.append(Task.department_id == item.department_id)
    if since is not None:
        where.extend([Task.due_at.is_not(None), Task.due_at >= since, Task.due_at < until])

    rows = list(
        (
            await session.execute(
                select(Task).where(*where).order_by(Task.due_at.asc().nulls_last(), Task.id)
                .limit(MAX_ROWS + 1)
            )
        ).scalars().all()
    )
    return _cut(rows, [
        Hit(kind="task", id=row.id, title=row.title,
            subtitle="search.kind.task", when=row.due_at)
        for row in rows
    ])


async def _decisions(
    session: AsyncSession, item: Filter, *, viewer: User, grants: dict[str, Grant],
    visible: set[int], now: datetime, since: datetime | None, until: datetime | None,
) -> Found:
    where = list(decision_service.visible_filter(viewer, grants, visible))
    if item.status:
        where.append(Decision.status == item.status)
    if item.overdue:
        where.extend(decision_service.overdue_filter(now))
    if item.person_ids:
        where.append(Decision.responsible_id.in_(item.person_ids))
    if item.department_id is not None:
        # У решения своего отдела нет — есть отдел автора и ответственного.
        # Та же связь, по которой решения видны начальнику отдела.
        people = select(User.id).where(
            User.organization_id == viewer.organization_id,
            User.department_id == item.department_id,
        )
        where.append(
            or_(Decision.author_id.in_(people), Decision.responsible_id.in_(people))
        )
    if since is not None:
        where.extend([Decision.created_at >= since, Decision.created_at < until])

    rows = list(
        (
            await session.execute(
                select(Decision).where(*where).order_by(Decision.created_at.desc())
                .limit(MAX_ROWS + 1)
            )
        ).scalars().all()
    )
    return _cut(rows, [
        Hit(kind="decision", id=row.id, title=row.title,
            subtitle="search.kind.decision", when=row.created_at)
        for row in rows
    ])


async def _meetings(
    session: AsyncSession, item: Filter, *, viewer: User, grants: dict[str, Grant],
    visible: set[int], since: datetime | None, until: datetime | None,
) -> Found:
    where = list(meeting_service.visible_filter(viewer, grants, visible))
    if item.status:
        where.append(Meeting.status == item.status)
    if item.person_ids:
        where.append(
            or_(
                Meeting.owner_id.in_(item.person_ids),
                Meeting.id.in_(
                    select(MeetingParticipant.meeting_id).where(
                        MeetingParticipant.user_id.in_(item.person_ids)
                    )
                ),
            )
        )
    if item.department_id is not None:
        owners = select(User.id).where(
            User.organization_id == viewer.organization_id,
            User.department_id == item.department_id,
        )
        where.append(Meeting.owner_id.in_(owners))
    if since is not None:
        where.extend([Meeting.start_at >= since, Meeting.start_at < until])

    rows = list(
        (
            await session.execute(
                select(Meeting).where(*where).order_by(Meeting.start_at).limit(MAX_ROWS + 1)
            )
        ).scalars().all()
    )
    return _cut(rows, [
        Hit(kind="meeting", id=row.id, title=row.title,
            subtitle="search.kind.meeting", when=row.start_at)
        for row in rows
    ])


def describe(item: Filter, locale: str | None = None) -> str:
    """Как вопрос понят — одной строкой, до ответа.

    Показывается всегда, а не только при неудаче. Человек, увидевший
    «поручения · просроченные · за месяц», сам заметит, что спрашивал про
    встречи, — и переспросит. Молчаливо неправильно понятый вопрос выглядит
    как правдивый ответ, и это худшая из возможных ошибок сценария.
    """
    parts = [t(f"ask.kind.{item.kind}", locale)]
    if item.overdue:
        parts.append(t("ask.overdue", locale))
    if item.status:
        parts.append(_status_word(item.kind, item.status, locale))
    if item.priority:
        parts.append(task_service.priority_title(item.priority, locale))
    # Название отдела и имя человека пишут люди, а строка уходит в сообщение
    # с разметкой. Отдел с угловой скобкой в названии не сломает сообщение —
    # он сломал бы отправку целиком, и человек не получил бы ответа вовсе.
    if item.department_name:
        parts.append(esc(item.department_name))
    if item.person_names:
        parts.append(", ".join(esc(name) for name in item.person_names))
    if item.period:
        # Вместе с периодом называется столбец, по которому он считается.
        # «За месяц» у поручений — это про срок, а не про дату создания,
        # и человек, спросивший про выданные, должен это увидеть сразу.
        parts.append(
            f"{t(f'ask.period.{item.period}', locale)} "
            f"{t(DATE_FIELD[item.kind], locale)}"
        )
    return t("ask.understood", locale, parts=" · ".join(parts))


def _status_word(kind: str, status: str, locale: str | None = None) -> str:
    """Название статуса на языке человека — из словаря того вида, чей статус."""
    if kind == "task":
        return task_service.status_title(status, locale)
    if kind == "decision":
        return t(f"decision.status.{status.lower()}", locale)
    return t(f"meeting.status.{status.lower()}", locale)

