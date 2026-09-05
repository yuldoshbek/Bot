"""Экран руководителя: один вопрос — один блок.

Критерий готовности блока 5 сформулирован не через функции, а через время:
руководитель открывает **один экран** и за пять секунд понимает четыре вещи.

    что сейчас       — идущая встреча или ближайшая
    что дальше       — остаток дня и первое свободное окно
    что требует решения — заявки, проверки, зависшие решения
    что просрочено   — сводкой по отделам, поштучно только личный контроль

Отсюда два правила этой службы.

**Сводка, а не поток.** Решение Р-10: руководителю просрочки приходят одним
блоком, а не по одной. Исключение — поручения на личном контроле: он сам их
пометил, значит хочет знать поимённо. Экран повторяет то же распределение,
иначе он снова превратится в ленту.

**Пусто — значит пусто.** Блок без содержимого не рисуется вовсе. «Просрочек: 0»
и «Заявок: 0» на экране, который читают за пять секунд, — это четыре строки шума
между двумя важными.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import to_local, utcnow
from app.models import (
    Decision,
    DecisionStatus,
    Meeting,
    MeetingParticipant,
    MeetingRequest,
    MeetingStatus,
    RequestStatus,
    Task,
    TaskStatus,
    User,
)
from app.models.org import Department
from app.services import analytics, quotas
from app.services import slots as slot_service
from app.services.analytics import Metric
from app.services.rbac import Grant, has_permission

# Сколько ближайших встреч показывать в блоке «дальше».
NEXT_MEETINGS = 4
# Сколько отделов помещается в сводку просрочек, прежде чем она сама станет лентой.
TOP_DEPARTMENTS = 5
# Сколько поручений личного контроля показывать поимённо.
PERSONAL_LIMIT = 5

# Поручение ещё ждут — те же статусы, что у контроля сроков.
PENDING_STATUSES = (
    TaskStatus.NEW,
    TaskStatus.ACKNOWLEDGED,
    TaskStatus.IN_PROGRESS,
    TaskStatus.BLOCKED,
    TaskStatus.OVERDUE,
)


@dataclass(slots=True)
class Board:
    """Собранный экран. Пустые поля обработчик просто не рисует."""

    day: datetime
    timezone: str = "Asia/Tashkent"

    running: list[Meeting] = field(default_factory=list)
    ahead: list[Meeting] = field(default_factory=list)
    free_slot: slot_service.Slot | None = None

    requests_waiting: int = 0
    requests_over_quota: int = 0
    to_review: int = 0
    stale_decisions: int = 0

    overdue_total: int = 0
    overdue_by_department: list[tuple[str, int]] = field(default_factory=list)
    # Хвост сводки: отделы, не поместившиеся в верхние строки. Существует
    # ради того, чтобы сумма сводки сходилась с общим числом.
    overdue_other: int = 0
    personal_overdue: list[Task] = field(default_factory=list)

    metrics: list[Metric] = field(default_factory=list)

    @property
    def needs_decision(self) -> int:
        """Сколько всего ждёт именно его решения."""
        return self.requests_waiting + self.to_review + self.stale_decisions

    @property
    def quiet(self) -> bool:
        """Ничего не происходит: встреч нет, решений не ждут, просрочек нет."""
        return (
            not self.running
            and not self.ahead
            and not self.needs_decision
            and not self.overdue_total
        )


async def build(
    session: AsyncSession,
    *,
    viewer: User,
    grants: dict[str, Grant],
    now: datetime | None = None,
    with_metrics: bool = True,
) -> Board:
    """Собирает экран. Каждый блок — один запрос, ни одного внутри цикла."""
    now = now or utcnow()
    local = to_local(now, viewer.timezone)
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(now.tzinfo)
    day_end = day_start + timedelta(days=1)

    board = Board(day=now, timezone=viewer.timezone)

    # ── Что сейчас и что дальше ─────────────────────────────────────────────
    today = (
        await session.execute(
            select(Meeting)
            .join(MeetingParticipant, MeetingParticipant.meeting_id == Meeting.id)
            .where(
                MeetingParticipant.user_id == viewer.id,
                Meeting.status != MeetingStatus.CANCELLED,
                Meeting.start_at >= day_start,
                Meeting.start_at < day_end,
            )
            .order_by(Meeting.start_at)
            .distinct()
        )
    ).scalars().all()
    board.running = [m for m in today if m.start_at <= now < m.end_at]
    board.ahead = [m for m in today if m.start_at > now][:NEXT_MEETINGS]

    # Первое свободное окно сегодня — ответ на «что ещё успею».
    if has_permission(grants, "meeting.approve"):
        found = await slot_service.free_slots(
            session, owner=viewer, duration_minutes=30, days_ahead=0, limit=1, now=now
        )
        board.free_slot = found[0] if found else None

    # ── Что требует решения ─────────────────────────────────────────────────
    if has_permission(grants, "meeting.approve"):
        board.requests_waiting = int(
            await session.scalar(
                select(func.count(MeetingRequest.id)).where(
                    MeetingRequest.owner_id == viewer.id,
                    MeetingRequest.status == RequestStatus.NEW,
                )
            )
            or 0
        )
        if board.requests_waiting:
            board.requests_over_quota = len(await quotas.over_quota_requests(session, owner=viewer))

    board.to_review = int(
        await session.scalar(
            select(func.count(Task.id)).where(
                Task.reviewer_id == viewer.id, Task.status == TaskStatus.REVIEW
            )
        )
        or 0
    )

    if has_permission(grants, "decision.close"):
        board.stale_decisions = int(
            await session.scalar(
                select(func.count(Decision.id)).where(
                    Decision.organization_id == viewer.organization_id,
                    Decision.status == DecisionStatus.OPEN,
                    Decision.due_date.is_not(None),
                    Decision.due_date < now,
                )
            )
            or 0
        )

    # ── Что просрочено ──────────────────────────────────────────────────────
    overdue = (
        Task.organization_id == viewer.organization_id,
        Task.status.in_(PENDING_STATUSES),
        Task.due_at.is_not(None),
        Task.due_at < now,
    )
    board.overdue_total = int(await session.scalar(select(func.count(Task.id)).where(*overdue)) or 0)

    if board.overdue_total:
        # Сводка по отделам — один запрос с группировкой по самому столбцу:
        # группировать по `coalesce(...)` нельзя, Postgres не сопоставляет
        # выражение в SELECT с выражением в GROUP BY, когда в них разные
        # подстановки. Подпись «вне отделов» ставится над сгруппированным
        # столбцом — это выражение от него и потому допустимо.
        rows = (
            await session.execute(
                select(
                    func.coalesce(Department.name, "вне отделов"),
                    func.count(Task.id),
                )
                .outerjoin(Department, Department.id == Task.department_id)
                .where(*overdue)
                .group_by(Department.name)
                .order_by(func.count(Task.id).desc())
            )
        ).all()
        # Отделов немного, поэтому берём все и складываем хвост здесь.
        # Обрезать выборку в запросе нельзя: сводка, сумма которой не сходится
        # с общим числом, — это экран, которому перестают верить.
        counted = [(row[0], int(row[1])) for row in rows]
        board.overdue_by_department = counted[:TOP_DEPARTMENTS]
        board.overdue_other = sum(count for _, count in counted[TOP_DEPARTMENTS:])

        # Личный контроль — исключение из правила сводки: руководитель сам
        # пометил эти поручения, значит хочет видеть их поимённо.
        board.personal_overdue = list(
            (
                await session.execute(
                    select(Task)
                    .where(*overdue, Task.personal_control.is_(True))
                    .order_by(Task.due_at)
                    .limit(PERSONAL_LIMIT)
                )
            ).scalars().all()
        )

    # ── Показатели ──────────────────────────────────────────────────────────
    if with_metrics:
        audience = await analytics.audience_for(session, viewer=viewer, grants=grants)
        if audience is not None and not audience.empty:
            board.metrics = await analytics.headline(
                session,
                audience=audience,
                period=analytics.month_back(now),
                now=now,
            )

    return board
