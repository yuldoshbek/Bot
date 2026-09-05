"""Пятнадцать показателей раздела 14 архитектуры.

Всё считает база. Ни один показатель не собирается перебором строк в Python:
на тридцати поручениях это работает, на тридцати тысячах кладёт бота. Одна
метрика — один агрегирующий запрос, из которого приходит уже готовое число.

Два правила, без которых показателям нельзя верить.

**Нет данных — это не ноль.** «Пунктуальность 0%» при отсутствии отметок явки
читается как «все опаздывают», хотя означает «никто не отмечался». Поэтому
метрика без данных возвращает отдельное состояние, и на экране так и написано.
Ноль здесь врёт громче, чем пустота.

Различает эти два случая **знаменатель**, а не числитель. Нет отметок явки —
делить не на что, показателя не существует. Есть рабочие часы, но нет встреч —
знаменатель настоящий, и ноль настоящий: календарь действительно пуст. Поэтому
загрузка календаря без встреч показывает 0%, а без заданных рабочих часов
молчит.

**Область считается по правам смотрящего.** Начальник отдела видит цифры своего
отдела, руководитель — всей организации, сотрудник — не видит аналитику вовсе.
Показатель, посчитанный шире, чем человеку открыто, — это утечка, просто
в виде среднего.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import utcnow
from app.models import (
    Decision,
    Meeting,
    MeetingAttendance,
    MeetingParticipant,
    MeetingRequest,
    MeetingStatus,
    ExtensionStatus,
    Priority,
    RequestStatus,
    Task,
    TaskEvent,
    TaskEventKind,
    TaskExtension,
    TaskStatus,
    User,
    UserStatus,
    WorkingHours,
)
from app.models.org import Department
from app.services.rbac import Grant, Scope, has_permission, scope_of, visible_department_ids

# Поручение считается закрытым, когда его больше не ждут.
CLOSED_STATUSES = (TaskStatus.DONE, TaskStatus.CANCELLED)
# Встреча состоялась, если её не отменили.
LIVE_MEETINGS = (
    MeetingStatus.PLANNED,
    MeetingStatus.CONFIRMED,
    MeetingStatus.IN_PROGRESS,
    MeetingStatus.FINISHED,
)
# Сколько раз тема должна повториться, чтобы считаться нерешаемой (показатель 11).
REPEAT_THRESHOLD = 4
# Сколько встреч подряд в одном дне считается пробкой (показатель 13).
JAM_MEETINGS_PER_DAY = 4
# Горизонт прогноза перегрузки (показатель 15).
FORECAST_DAYS = 7


@dataclass(slots=True)
class Metric:
    """Один показатель, готовый к показу.

    `value is None` означает «нет данных» — это не ноль и рисуется иначе.
    `rows` заполняется у рейтинговых показателей: топ инициаторов, топ отделов.
    """

    key: str
    title: str
    value: float | None = None
    unit: str = ""
    detail: str = ""
    rows: list[tuple[str, float]] = field(default_factory=list)

    @property
    def no_data(self) -> bool:
        return self.value is None and not self.rows

    def render(self) -> str:
        """Строка для сообщения. Пустой показатель говорит «нет данных»."""
        if self.no_data:
            return f"{self.title}: нет данных"
        if self.value is None:
            head = self.title
        else:
            shown = f"{self.value:.0f}" if float(self.value).is_integer() else f"{self.value:.1f}"
            head = f"{self.title}: {shown}{self.unit}"
        return f"{head} — {self.detail}" if self.detail else head


@dataclass(slots=True)
class Period:
    """Отрезок, за который считаются показатели."""

    since: datetime
    until: datetime
    title: str = ""

    @property
    def days(self) -> int:
        return max(1, (self.until - self.since).days)


def month_back(now: datetime | None = None, days: int = 30) -> Period:
    """Последние N дней — период по умолчанию для дашборда."""
    now = now or utcnow()
    return Period(since=now - timedelta(days=days), until=now, title=f"за {days} дней")


@dataclass(slots=True)
class Audience:
    """Кого именно охватывает расчёт.

    `everyone` — вся организация; иначе считаем только по `user_ids`. Множество
    собирается один раз и передаётся во все показатели: пятнадцать метрик,
    каждая из которых заново выясняет состав отдела, — это пятнадцать лишних
    запросов на одно открытие экрана.
    """

    organization_id: int
    everyone: bool = False
    user_ids: set[int] = field(default_factory=set)
    department_ids: set[int] = field(default_factory=set)
    # Границы суток считаются в этом поясе: «понедельник» в Ташкенте начинается
    # на пять часов раньше UTC, и по UTC часть утра уехала бы в воскресенье.
    timezone: str = "Asia/Tashkent"

    @property
    def empty(self) -> bool:
        return not self.everyone and not self.user_ids


async def audience_for(
    session: AsyncSession, *, viewer: User, grants: dict[str, Grant]
) -> Audience | None:
    """Область расчёта по правам смотрящего. None — аналитика ему не открыта.

    Право `analytics.read_org` даёт организацию целиком, `analytics.read`
    с областью DEPARTMENT — свой отдел и вложенные. Рядовой сотрудник не
    получает ни того, ни другого: средние по чужой работе — это тоже данные.
    """
    if has_permission(grants, "analytics.read_org"):
        return Audience(
            organization_id=viewer.organization_id, everyone=True, timezone=viewer.timezone
        )

    if not has_permission(grants, "analytics.read"):
        return None

    scope = scope_of(grants, "analytics.read")
    if scope == Scope.ORGANIZATION:
        return Audience(
            organization_id=viewer.organization_id, everyone=True, timezone=viewer.timezone
        )

    if scope == Scope.DEPARTMENT:
        departments = await visible_department_ids(session, viewer)
        if not departments:
            # Отдела нет — область пуста. Расширять её до организации нельзя:
            # незаполненное поле не должно открывать больше, чем заполненное.
            return Audience(
                organization_id=viewer.organization_id, timezone=viewer.timezone
            )
        people = set(
            (
                await session.execute(
                    select(User.id).where(
                        User.organization_id == viewer.organization_id,
                        User.department_id.in_(departments),
                    )
                )
            ).scalars().all()
        )
        return Audience(
            organization_id=viewer.organization_id,
            user_ids=people,
            department_ids=departments,
            timezone=viewer.timezone,
        )

    return Audience(
        organization_id=viewer.organization_id,
        user_ids={viewer.id},
        timezone=viewer.timezone,
    )


# ── Общие куски условий ─────────────────────────────────────────────────────
def _tasks_in(audience: Audience):
    """Условие «поручение входит в область». Один и тот же кусок у семи метрик."""
    conditions = [Task.organization_id == audience.organization_id]
    if not audience.everyone:
        conditions.append(Task.assignee_id.in_(audience.user_ids or {0}))
    return conditions


def _meetings_in(audience: Audience):
    conditions = [
        Meeting.organization_id == audience.organization_id,
        Meeting.status.in_(LIVE_MEETINGS),
    ]
    if not audience.everyone:
        conditions.append(Meeting.owner_id.in_(audience.user_ids or {0}))
    return conditions


def _minutes(start, end):
    """Длительность в минутах как выражение базы, а не разность в Python."""
    return func.extract("epoch", end - start) / 60.0


async def _names(session: AsyncSession, ids: set[int]) -> dict[int, str]:
    """Имена одним запросом: рейтинги без этого превратились бы в цикл запросов."""
    ids = {i for i in ids if i}
    if not ids:
        return {}
    rows = await session.execute(select(User.id, User.full_name).where(User.id.in_(ids)))
    return {row[0]: row[1] for row in rows.all()}


# ── 1. Загрузка календаря ───────────────────────────────────────────────────
async def calendar_load(
    session: AsyncSession, *, audience: Audience, period: Period
) -> Metric:
    """Минуты встреч ÷ доступные рабочие минуты.

    Знаменатель берётся из рабочих часов, а не из «восьми часов в сутки»:
    у руководителя с приёмом до 19:00 и у сотрудника с обедом разные сутки,
    и общий делитель превратил бы показатель в художественное число.
    """
    metric = Metric(key="calendar_load", title="Загрузка календаря", unit="%")

    busy = await session.scalar(
        select(func.coalesce(func.sum(_minutes(Meeting.start_at, Meeting.end_at)), 0.0))
        .where(
            *_meetings_in(audience),
            Meeting.start_at >= period.since,
            Meeting.start_at < period.until,
        )
    )

    hours_conditions = [WorkingHours.is_working.is_(True)]
    if audience.everyone:
        hours_conditions.append(
            WorkingHours.user_id.in_(
                select(User.id).where(
                    User.organization_id == audience.organization_id,
                    User.status == UserStatus.ACTIVE,
                )
            )
        )
    else:
        hours_conditions.append(WorkingHours.user_id.in_(audience.user_ids or {0}))

    # Рабочие минуты недели: разность времён даёт интервал прямо в базе.
    # Обед вычитается — на встречу его никто не отдаёт.
    day = func.extract("epoch", WorkingHours.end_time - WorkingHours.start_time) / 60.0
    lunch = case(
        (
            and_(WorkingHours.lunch_start.is_not(None), WorkingHours.lunch_end.is_not(None)),
            func.extract("epoch", WorkingHours.lunch_end - WorkingHours.lunch_start) / 60.0,
        ),
        else_=0.0,
    )
    weekly = await session.scalar(
        select(func.coalesce(func.sum(day - lunch), 0.0)).where(*hours_conditions)
    )
    available = float(weekly or 0.0) * period.days / 7.0
    if available <= 0:
        metric.detail = "рабочие часы не заданы"
        return metric

    metric.value = round(float(busy or 0.0) / available * 100, 1)
    metric.detail = f"{int(busy or 0)} мин из {int(available)}"
    return metric


# ── 2. Кто расходует время руководителя ─────────────────────────────────────
async def time_spenders(
    session: AsyncSession, *, audience: Audience, period: Period, limit: int = 5
) -> Metric:
    """Сумма минут по инициаторам встреч. Рейтинг, а не одно число."""
    metric = Metric(key="time_spenders", title="Кто расходует время", unit=" мин")

    rows = (
        await session.execute(
            select(
                MeetingParticipant.user_id,
                func.sum(_minutes(Meeting.start_at, Meeting.end_at)).label("minutes"),
            )
            .join(Meeting, Meeting.id == MeetingParticipant.meeting_id)
            .where(
                *_meetings_in(audience),
                Meeting.start_at >= period.since,
                Meeting.start_at < period.until,
                MeetingParticipant.user_id != Meeting.owner_id,
            )
            .group_by(MeetingParticipant.user_id)
            .order_by(func.sum(_minutes(Meeting.start_at, Meeting.end_at)).desc())
            .limit(limit)
        )
    ).all()
    if not rows:
        return metric

    names = await _names(session, {row[0] for row in rows})
    metric.rows = [(names.get(row[0], "неизвестно"), round(float(row[1]), 0)) for row in rows]
    metric.detail = f"верхние {len(metric.rows)}"
    return metric


# ── 3. Стоимость совещания ──────────────────────────────────────────────────
async def meeting_cost(
    session: AsyncSession, *, audience: Audience, period: Period, limit: int = 5
) -> Metric:
    """Участники × длительность в человеко-часах. Топ самых дорогих."""
    metric = Metric(key="meeting_cost", title="Стоимость совещаний", unit=" чел·ч")

    # После соединения с участниками каждая встреча даёт строку на человека,
    # поэтому сумма длительностей и есть человеко-минуты: отдельно умножать
    # на число участников не нужно, это была бы вторая такая же множитель.
    person_hours = func.sum(_minutes(Meeting.start_at, Meeting.end_at)) / 60.0
    rows = (
        await session.execute(
            select(Meeting.title, person_hours.label("hours"))
            .join(MeetingParticipant, MeetingParticipant.meeting_id == Meeting.id)
            .where(
                *_meetings_in(audience),
                Meeting.start_at >= period.since,
                Meeting.start_at < period.until,
            )
            .group_by(Meeting.id, Meeting.title)
            .order_by(person_hours.desc())
            .limit(limit)
        )
    ).all()
    if not rows:
        return metric

    metric.rows = [(row[0], round(float(row[1]), 1)) for row in rows]
    metric.value = round(sum(value for _, value in metric.rows), 1)
    metric.detail = f"верхние {len(metric.rows)}"
    return metric


# ── 4. Пунктуальность ───────────────────────────────────────────────────────
async def punctuality(
    session: AsyncSession, *, audience: Audience, period: Period
) -> Metric:
    """Доля опозданий среди отметок явки.

    Считается только по тем, кто отмечался: участник без отметки — это «нет
    записи», а не установленный прогул, и попадать в знаменатель он не должен.
    """
    metric = Metric(key="punctuality", title="Пунктуальность", unit="%")

    conditions = [
        Meeting.organization_id == audience.organization_id,
        Meeting.start_at >= period.since,
        Meeting.start_at < period.until,
    ]
    if not audience.everyone:
        conditions.append(MeetingAttendance.user_id.in_(audience.user_ids or {0}))

    row = (
        await session.execute(
            select(
                func.count(MeetingAttendance.id),
                func.sum(case((MeetingAttendance.late_minutes > 0, 1), else_=0)),
            )
            .join(Meeting, Meeting.id == MeetingAttendance.meeting_id)
            .where(*conditions, MeetingAttendance.present.is_(True))
        )
    ).one()
    total, late = int(row[0] or 0), int(row[1] or 0)
    if total == 0:
        metric.detail = "отметок явки нет"
        return metric

    metric.value = round((total - late) / total * 100, 1)
    metric.detail = f"вовремя {total - late} из {total}"
    return metric


# ── 5. Дисциплина сроков ────────────────────────────────────────────────────
async def deadline_discipline(
    session: AsyncSession, *, audience: Audience, period: Period
) -> Metric:
    """Доля выполненных в срок среди завершённых со сроком.

    В знаменатель идут только поручения, у которых срок был и которые уже
    закрыты: незавершённое поручение ещё ничего не говорит о дисциплине.
    """
    metric = Metric(key="deadline_discipline", title="Дисциплина сроков", unit="%")

    row = (
        await session.execute(
            select(
                func.count(Task.id),
                func.sum(case((Task.completed_at <= Task.due_at, 1), else_=0)),
            ).where(
                *_tasks_in(audience),
                Task.status == TaskStatus.DONE,
                Task.due_at.is_not(None),
                Task.completed_at.is_not(None),
                Task.completed_at >= period.since,
                Task.completed_at < period.until,
            )
        )
    ).one()
    total, in_time = int(row[0] or 0), int(row[1] or 0)
    if total == 0:
        metric.detail = "завершённых поручений со сроком нет"
        return metric

    metric.value = round(in_time / total * 100, 1)
    metric.detail = f"в срок {in_time} из {total}"
    return metric


# ── 6. Хронические переносы ─────────────────────────────────────────────────
async def chronic_extensions(
    session: AsyncSession, *, audience: Audience, period: Period, limit: int = 5
) -> Metric:
    """Одобренные продления по исполнителям.

    Считаются именно одобренные: отклонённая просьба ничего не сдвинула,
    и ставить её человеку в счёт нельзя.
    """
    metric = Metric(key="chronic_extensions", title="Хронические переносы", unit=" продл.")

    rows = (
        await session.execute(
            select(Task.assignee_id, func.count(TaskExtension.id).label("times"))
            .join(Task, Task.id == TaskExtension.task_id)
            .where(
                *_tasks_in(audience),
                TaskExtension.status == ExtensionStatus.APPROVED,
                TaskExtension.decided_at >= period.since,
                TaskExtension.decided_at < period.until,
            )
            .group_by(Task.assignee_id)
            .order_by(func.count(TaskExtension.id).desc())
            .limit(limit)
        )
    ).all()
    if not rows:
        metric.detail = "одобренных продлений нет"
        return metric

    names = await _names(session, {row[0] for row in rows})
    metric.rows = [(names.get(row[0], "неизвестно"), float(row[1])) for row in rows]
    metric.value = float(sum(value for _, value in metric.rows))
    return metric


# ── 7. Скорость реакции ─────────────────────────────────────────────────────
async def reaction_time(
    session: AsyncSession, *, audience: Audience, period: Period
) -> Metric:
    """Часы от выдачи поручения до его принятия исполнителем.

    Непринятые в знаменатель не идут: поручение, которое ещё не приняли,
    не имеет времени реакции — у него есть только возраст.
    """
    metric = Metric(key="reaction_time", title="Скорость реакции", unit=" ч")

    row = (
        await session.execute(
            select(
                func.count(Task.id),
                func.avg(func.extract("epoch", Task.accepted_at - Task.created_at) / 3600.0),
            ).where(
                *_tasks_in(audience),
                Task.accepted_at.is_not(None),
                Task.created_at >= period.since,
                Task.created_at < period.until,
            )
        )
    ).one()
    total, average = int(row[0] or 0), row[1]
    if total == 0 or average is None:
        metric.detail = "принятых поручений нет"
        return metric

    metric.value = round(float(average), 1)
    metric.detail = f"по {total} поручениям"
    return metric


# ── 8. Возвраты на доработку ────────────────────────────────────────────────
async def rework_rate(
    session: AsyncSession, *, audience: Audience, period: Period
) -> Metric:
    """Доля поручений, хотя бы раз возвращённых на доработку.

    Знаменатель — поручения, которые вообще проходили проверку: без проверки
    вернуть было некуда, и включать их значило бы занижать показатель.
    """
    metric = Metric(key="rework_rate", title="Возвраты на доработку", unit="%")

    reviewed = select(Task.id).where(
        *_tasks_in(audience),
        Task.requires_review.is_(True),
        Task.submitted_at.is_not(None),
        Task.submitted_at >= period.since,
        Task.submitted_at < period.until,
    ).subquery()

    total = await session.scalar(select(func.count()).select_from(reviewed))
    if not total:
        metric.detail = "поручений на проверке не было"
        return metric

    returned = await session.scalar(
        select(func.count(func.distinct(TaskEvent.task_id))).where(
            TaskEvent.task_id.in_(select(reviewed.c.id)),
            TaskEvent.kind == TaskEventKind.RETURNED,
        )
    )
    metric.value = round(int(returned or 0) / int(total) * 100, 1)
    metric.detail = f"вернули {int(returned or 0)} из {int(total)}"
    return metric


# ── 9. Встречи без результата ───────────────────────────────────────────────
async def fruitless_meetings(
    session: AsyncSession, *, audience: Audience, period: Period
) -> Metric:
    """Доля завершённых встреч, не породивших ни решения, ни поручения.

    Считаются только завершённые: у будущей встречи результата ещё нет
    по определению, и попадание в знаменатель делало бы показатель
    зависимым от того, сколько всего запланировано.
    """
    metric = Metric(key="fruitless_meetings", title="Встречи без результата", unit="%")

    finished = select(Meeting.id).where(
        Meeting.organization_id == audience.organization_id,
        Meeting.status == MeetingStatus.FINISHED,
        Meeting.start_at >= period.since,
        Meeting.start_at < period.until,
        *([] if audience.everyone else [Meeting.owner_id.in_(audience.user_ids or {0})]),
    ).subquery()

    total = await session.scalar(select(func.count()).select_from(finished))
    if not total:
        metric.detail = "завершённых встреч нет"
        return metric

    with_result = await session.scalar(
        select(func.count(func.distinct(finished.c.id))).where(
            or_(
                finished.c.id.in_(select(Decision.meeting_id).where(Decision.meeting_id.is_not(None))),
                finished.c.id.in_(select(Task.meeting_id).where(Task.meeting_id.is_not(None))),
            )
        )
    )
    empty = int(total) - int(with_result or 0)
    metric.value = round(empty / int(total) * 100, 1)
    metric.detail = f"без итогов {empty} из {int(total)}"
    return metric


# ── 10. Скорость решений руководителя ───────────────────────────────────────
async def decision_speed(
    session: AsyncSession, *, audience: Audience, period: Period
) -> Metric:
    """Часы от заявки на встречу до ответа руководителя.

    Заявки, истёкшие без ответа, в среднее не идут — но считаются отдельно
    и показываются в пояснении: молчание тоже ответ, просто худший.
    """
    metric = Metric(key="decision_speed", title="Скорость решений", unit=" ч")

    conditions = [
        MeetingRequest.organization_id == audience.organization_id,
        MeetingRequest.created_at >= period.since,
        MeetingRequest.created_at < period.until,
    ]
    if not audience.everyone:
        conditions.append(MeetingRequest.owner_id.in_(audience.user_ids or {0}))

    row = (
        await session.execute(
            select(
                func.count(MeetingRequest.id),
                func.avg(
                    func.extract("epoch", MeetingRequest.decided_at - MeetingRequest.created_at)
                    / 3600.0
                ),
                func.sum(case((MeetingRequest.status == RequestStatus.EXPIRED, 1), else_=0)),
            ).where(
                *conditions,
                or_(
                    MeetingRequest.decided_at.is_not(None),
                    MeetingRequest.status == RequestStatus.EXPIRED,
                ),
            )
        )
    ).one()
    total, average, expired = int(row[0] or 0), row[1], int(row[2] or 0)
    if total == 0 or average is None:
        metric.detail = "рассмотренных заявок нет"
        return metric

    metric.value = round(float(average), 1)
    metric.detail = f"по {total} заявкам"
    if expired:
        metric.detail += f", без ответа истекло {expired}"
    return metric


# ── 11. Повторяющиеся темы ──────────────────────────────────────────────────
async def repeating_topics(
    session: AsyncSession, *, audience: Audience, period: Period, limit: int = 5
) -> Metric:
    """Одна тема на четырёх и более встречах: вопрос не решается.

    Темы сравниваются по нормализованной формулировке — регистр и лишние
    пробелы не должны превращать одно совещание в два разных.
    """
    metric = Metric(key="repeating_topics", title="Повторяющиеся темы", unit=" встреч")

    topic = func.lower(func.btrim(Meeting.title))
    rows = (
        await session.execute(
            select(topic.label("topic"), func.count(Meeting.id).label("times"))
            .where(
                *_meetings_in(audience),
                Meeting.start_at >= period.since,
                Meeting.start_at < period.until,
            )
            .group_by(topic)
            .having(func.count(Meeting.id) >= REPEAT_THRESHOLD)
            .order_by(func.count(Meeting.id).desc())
            .limit(limit)
        )
    ).all()
    if not rows:
        metric.detail = f"тем, повторённых {REPEAT_THRESHOLD} раза и больше, нет"
        return metric

    metric.rows = [(row[0], float(row[1])) for row in rows]
    metric.value = float(len(metric.rows))
    return metric


# ── 12. Доступность руководителя ────────────────────────────────────────────
async def availability_lag(
    session: AsyncSession, *, audience: Audience, period: Period
) -> Metric:
    """Сколько дней в среднем ждут свободного окна.

    Считается по одобренным заявкам: от подачи до начала встречи. Отклонённые
    и истёкшие не показывают доступность — они показывают отказ.
    """
    metric = Metric(key="availability_lag", title="Ожидание окна", unit=" дн.")

    conditions = [
        MeetingRequest.organization_id == audience.organization_id,
        MeetingRequest.status == RequestStatus.APPROVED,
        MeetingRequest.created_at >= period.since,
        MeetingRequest.created_at < period.until,
    ]
    if not audience.everyone:
        conditions.append(MeetingRequest.owner_id.in_(audience.user_ids or {0}))

    row = (
        await session.execute(
            select(
                func.count(MeetingRequest.id),
                func.avg(
                    func.extract("epoch", MeetingRequest.start_at - MeetingRequest.created_at)
                    / 86400.0
                ),
            ).where(*conditions)
        )
    ).one()
    total, average = int(row[0] or 0), row[1]
    if total == 0 or average is None:
        metric.detail = "одобренных заявок нет"
        return metric

    metric.value = round(float(average), 1)
    metric.detail = f"по {total} заявкам"
    return metric


# ── 13. Пробки в расписании ─────────────────────────────────────────────────
async def schedule_jams(
    session: AsyncSession, *, audience: Audience, period: Period, limit: int = 5
) -> Metric:
    """Дни, где встреч больше предела: расписание без единой паузы.

    День берётся в часовом поясе организации, а не в UTC: «понедельник»
    у нас начинается на пять часов раньше, и по UTC часть утра уехала бы
    в воскресенье.
    """
    metric = Metric(key="schedule_jams", title="Пробки в расписании", unit=" дн.")

    local_day = func.date(func.timezone(audience.timezone, Meeting.start_at))
    rows = (
        await session.execute(
            select(local_day.label("day"), func.count(Meeting.id).label("times"))
            .where(
                *_meetings_in(audience),
                Meeting.start_at >= period.since,
                Meeting.start_at < period.until,
            )
            .group_by(local_day)
            .having(func.count(Meeting.id) >= JAM_MEETINGS_PER_DAY)
            .order_by(func.count(Meeting.id).desc())
            .limit(limit)
        )
    ).all()
    if not rows:
        metric.detail = f"дней с {JAM_MEETINGS_PER_DAY} и более встречами нет"
        return metric

    metric.rows = [(row[0].strftime("%d.%m"), float(row[1])) for row in rows]
    metric.value = float(len(metric.rows))
    return metric


# ── 14. Тренд просрочек по отделам ──────────────────────────────────────────
async def overdue_trend(
    session: AsyncSession, *, audience: Audience, period: Period, limit: int = 5
) -> Metric:
    """Просрочки этого периода против предыдущего такой же длины, по отделам.

    Знак важнее величины: вопрос не «сколько просрочек», а «стало хуже
    или лучше». Плюс — деградация, минус — улучшение.
    """
    metric = Metric(key="overdue_trend", title="Тренд просрочек", unit="")

    length = period.until - period.since
    previous_since = period.since - length

    overdue = case(
        (
            and_(
                Task.due_at.is_not(None),
                or_(
                    and_(Task.completed_at.is_not(None), Task.completed_at > Task.due_at),
                    and_(Task.completed_at.is_(None), Task.due_at < period.until),
                ),
            ),
            1,
        ),
        else_=0,
    )
    rows = (
        await session.execute(
            select(
                Department.name,
                func.sum(case((Task.created_at >= period.since, overdue), else_=0)).label("now_"),
                func.sum(case((Task.created_at < period.since, overdue), else_=0)).label("was"),
            )
            .join(Department, Department.id == Task.department_id)
            .where(
                *_tasks_in(audience),
                Task.created_at >= previous_since,
                Task.created_at < period.until,
            )
            .group_by(Department.name)
            .limit(limit)
        )
    ).all()
    if not rows:
        metric.detail = "поручений с отделом нет"
        return metric

    metric.rows = [(row[0], float(int(row[1] or 0) - int(row[2] or 0))) for row in rows]
    metric.rows.sort(key=lambda item: item[1], reverse=True)
    metric.value = float(sum(value for _, value in metric.rows))
    metric.detail = "плюс — стало хуже, минус — лучше"
    return metric


# ── 15. Прогноз перегрузки ──────────────────────────────────────────────────
async def overload_forecast(
    session: AsyncSession, *, audience: Audience, now: datetime | None = None
) -> Metric:
    """Встречи и сроки следующей недели: предупредить заранее, а не постфактум.

    Единственный показатель, смотрящий вперёд, поэтому период у него свой
    и не зависит от выбранного на экране.
    """
    now = now or utcnow()
    horizon = now + timedelta(days=FORECAST_DAYS)
    metric = Metric(key="overload_forecast", title="Ближайшая неделя", unit="")

    meetings = await session.scalar(
        select(func.count(Meeting.id)).where(
            *_meetings_in(audience), Meeting.start_at >= now, Meeting.start_at < horizon
        )
    )
    minutes = await session.scalar(
        select(func.coalesce(func.sum(_minutes(Meeting.start_at, Meeting.end_at)), 0.0)).where(
            *_meetings_in(audience), Meeting.start_at >= now, Meeting.start_at < horizon
        )
    )
    deadlines = await session.scalar(
        select(func.count(Task.id)).where(
            *_tasks_in(audience),
            Task.status.not_in(CLOSED_STATUSES),
            Task.due_at >= now,
            Task.due_at < horizon,
        )
    )
    critical = await session.scalar(
        select(func.count(Task.id)).where(
            *_tasks_in(audience),
            Task.status.not_in(CLOSED_STATUSES),
            Task.priority.in_((Priority.HIGH, Priority.CRITICAL)),
            Task.due_at >= now,
            Task.due_at < horizon,
        )
    )

    meetings, deadlines = int(meetings or 0), int(deadlines or 0)
    if not meetings and not deadlines:
        metric.detail = "встреч и сроков впереди нет"
        return metric

    metric.value = float(meetings + deadlines)
    parts = [f"встреч {meetings} на {int(minutes or 0)} мин", f"сроков {deadlines}"]
    if critical:
        parts.append(f"из них важных {int(critical)}")
    metric.detail = ", ".join(parts)
    return metric


# ── Сборка ──────────────────────────────────────────────────────────────────
# Порядок соответствует разделу 14 архитектуры. На главный экран руководителя
# выносятся 1, 5, 9, 14 и 15 — остальные живут в разделе аналитики.
HEADLINE_KEYS = (
    "calendar_load",
    "deadline_discipline",
    "fruitless_meetings",
    "overdue_trend",
    "overload_forecast",
)


async def all_metrics(
    session: AsyncSession,
    *,
    audience: Audience,
    period: Period,
    now: datetime | None = None,
) -> list[Metric]:
    """Все пятнадцать по порядку. Пустая область даёт пустой список, а не нули."""
    if audience.empty:
        return []
    now = now or utcnow()
    return [
        await calendar_load(session, audience=audience, period=period),
        await time_spenders(session, audience=audience, period=period),
        await meeting_cost(session, audience=audience, period=period),
        await punctuality(session, audience=audience, period=period),
        await deadline_discipline(session, audience=audience, period=period),
        await chronic_extensions(session, audience=audience, period=period),
        await reaction_time(session, audience=audience, period=period),
        await rework_rate(session, audience=audience, period=period),
        await fruitless_meetings(session, audience=audience, period=period),
        await decision_speed(session, audience=audience, period=period),
        await repeating_topics(session, audience=audience, period=period),
        await availability_lag(session, audience=audience, period=period),
        await schedule_jams(session, audience=audience, period=period),
        await overdue_trend(session, audience=audience, period=period),
        await overload_forecast(session, audience=audience, now=now),
    ]


async def headline(
    session: AsyncSession,
    *,
    audience: Audience,
    period: Period,
    now: datetime | None = None,
) -> list[Metric]:
    """Пять показателей главного экрана — без расчёта остальных десяти."""
    if audience.empty:
        return []
    now = now or utcnow()
    return [
        await calendar_load(session, audience=audience, period=period),
        await deadline_discipline(session, audience=audience, period=period),
        await fruitless_meetings(session, audience=audience, period=period),
        await overdue_trend(session, audience=audience, period=period),
        await overload_forecast(session, audience=audience, now=now),
    ]
