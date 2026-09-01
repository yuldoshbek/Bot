"""Лимиты времени руководителя: сколько его часов полагается отделу и человеку.

Квота здесь — не запрет, а видимая цена. Сотрудник, отправляя заявку, видит
остаток; превышение заявку не блокирует, а помечает. Срочный вопрос не должен
упираться в норму, но и расходоваться время не должно молча.

Личное исключение сильнее отдельского: «этому человеку — два часа в неделю»
задаётся ровно там, где нужно, и не требует переписывать норму всего отдела.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import to_local
from app.models import (
    Meeting,
    MeetingParticipant,
    MeetingRequest,
    MeetingStatus,
    QuotaPeriod,
    RequestStatus,
    TimeQuota,
    User,
)

PERIOD_LABELS = {QuotaPeriod.WEEK: "на неделю", QuotaPeriod.MONTH: "на месяц"}


@dataclass
class QuotaView:
    """Норма, расход и остаток. limit=None — нормы нет, ограничений тоже."""

    limit: int | None
    spent: int
    period: str
    period_start: datetime
    period_end: datetime

    @property
    def left(self) -> int | None:
        if self.limit is None:
            return None
        return self.limit - self.spent

    @property
    def unlimited(self) -> bool:
        return self.limit is None

    def render(self) -> str:
        if self.unlimited:
            return "Лимит времени не задан."
        left = max(0, self.left or 0)
        over = "" if (self.left or 0) >= 0 else f" (перебор {-(self.left or 0)} мин)"
        return (
            f"Лимит {PERIOD_LABELS.get(self.period, '')}: {self.limit} мин · "
            f"израсходовано {self.spent} · остаток {left}{over}"
        )


def period_bounds(period: str, now: datetime, tz_name: str) -> tuple[datetime, datetime]:
    """Границы текущего периода по местному календарю владельца.

    Неделя начинается в понедельник, месяц — первого числа. Считать по UTC
    нельзя: в Ташкенте месяц начинается на пять часов раньше, и последние
    встречи месяца попали бы в следующий.
    """
    tz = ZoneInfo(tz_name)
    local = to_local(now, tz_name)
    if period == QuotaPeriod.MONTH:
        start_local = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start_local.month == 12:
            end_local = start_local.replace(year=start_local.year + 1, month=1)
        else:
            end_local = start_local.replace(month=start_local.month + 1)
    else:
        start_local = (local - timedelta(days=local.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end_local = start_local + timedelta(days=7)
    return start_local.astimezone(tz), end_local.astimezone(tz)


async def quota_for(
    session: AsyncSession, *, owner: User, subject: User
) -> TimeQuota | None:
    """Норма для этого человека: личная, иначе отдельская, иначе никакой."""
    rows = (
        await session.execute(
            select(TimeQuota).where(
                TimeQuota.owner_id == owner.id,
                or_(
                    TimeQuota.subject_user_id == subject.id,
                    TimeQuota.subject_department_id == subject.department_id,
                ),
            )
        )
    ).scalars().all()
    personal = [q for q in rows if q.subject_user_id == subject.id]
    if personal:
        return personal[0]
    departmental = [
        q for q in rows
        if q.subject_user_id is None
        and subject.department_id is not None
        and q.subject_department_id == subject.department_id
    ]
    return departmental[0] if departmental else None


async def spent_minutes(
    session: AsyncSession, *, owner: User, subject: User, start: datetime, end: datetime
) -> int:
    """Сколько минут владельца этот человек уже занял в периоде.

    Отменённые встречи не считаются: отменённая встреча времени не съела.
    Всё остальное — запланированное, идущее, завершённое — считается: время
    занято в календаре, даже если встреча ещё не состоялась.
    """
    total = await session.scalar(
        select(
            func.coalesce(
                func.sum(
                    func.extract("epoch", Meeting.end_at - Meeting.start_at) / 60
                ),
                0,
            )
        )
        .join(MeetingParticipant, MeetingParticipant.meeting_id == Meeting.id)
        .where(
            Meeting.owner_id == owner.id,
            Meeting.status != MeetingStatus.CANCELLED,
            Meeting.start_at >= start,
            Meeting.start_at < end,
            MeetingParticipant.user_id == subject.id,
            MeetingParticipant.user_id != owner.id,
        )
    )
    return int(total or 0)


async def view(
    session: AsyncSession, *, owner: User, subject: User, now: datetime
) -> QuotaView:
    """Норма, расход и остаток на сейчас — то, что видит сотрудник при запросе."""
    quota = await quota_for(session, owner=owner, subject=subject)
    period = quota.period if quota else QuotaPeriod.WEEK
    start, end = period_bounds(period, now, owner.timezone)
    used = await spent_minutes(session, owner=owner, subject=subject, start=start, end=end)
    return QuotaView(
        limit=quota.minutes if quota else None,
        spent=used,
        period=period,
        period_start=start,
        period_end=end,
    )


async def would_exceed(
    session: AsyncSession, *, owner: User, subject: User, minutes: int, now: datetime
) -> bool:
    """Выйдет ли эта заявка за норму. Ответ помечает заявку, но не отклоняет её."""
    current = await view(session, owner=owner, subject=subject, now=now)
    if current.unlimited:
        return False
    return current.spent + minutes > (current.limit or 0)


async def over_quota_requests(
    session: AsyncSession, *, owner: User
) -> list[MeetingRequest]:
    """Заявки сверх нормы, ждущие решения: руководитель видит их отдельно."""
    return list(
        (
            await session.execute(
                select(MeetingRequest)
                .where(
                    MeetingRequest.owner_id == owner.id,
                    MeetingRequest.status == RequestStatus.NEW,
                    MeetingRequest.over_quota.is_(True),
                )
                .order_by(MeetingRequest.start_at)
            )
        ).scalars().all()
    )
