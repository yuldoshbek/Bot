"""Индикатор доступности руководителя.

Руководитель одной кнопкой сообщает, что он на месте и готов принимать.
Это не заменяет календарь, а работает поверх него:

  OPEN  - принимает сейчас: сотрудники видят индикатор и могут обратиться без заявки,
          при желании открываются и поздние окна вне рабочих часов;
  BUSY  - занят: заявки принимаются, но обращаться сейчас не нужно;
  DND   - не беспокоить: придерживаются даже срочные обращения;
  OFFLINE - индикатор не выставлен, работают обычные правила календаря.

У состояния всегда есть срок: вечного "доступен" не бывает - иначе индикатор
через неделю перестанет отражать реальность и ему перестанут верить.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import fmt_time, utcnow
from app.models.enums import Availability
from app.models.schedule import AvailabilityLog, AvailabilityState
from app.models.user import User
from app.services.audit import write_audit

DEFAULT_DURATION_MINUTES = 60

STATE_LABELS: dict[Availability, str] = {
    Availability.OPEN: "Доступен для приёма",
    Availability.BUSY: "Занят",
    Availability.DND: "Не беспокоить",
    Availability.OFFLINE: "Индикатор не выставлен",
}

STATE_MARKS: dict[Availability, str] = {
    Availability.OPEN: "🟢",
    Availability.BUSY: "🟡",
    Availability.DND: "🔴",
    Availability.OFFLINE: "⚪",
}


@dataclass(slots=True)
class AvailabilityView:
    """Готовое к показу состояние: уже с учётом истёкшего срока."""

    state: Availability
    note: str | None
    until_at: datetime | None
    opens_late_slots: bool

    @property
    def is_open(self) -> bool:
        return self.state == Availability.OPEN

    def render(self, tz_name: str | None = None) -> str:
        mark = STATE_MARKS[self.state]
        label = STATE_LABELS[self.state]
        parts = [f"{mark} {label}"]
        if self.until_at and self.state != Availability.OFFLINE:
            parts.append(f"до {fmt_time(self.until_at, tz_name)}")
        if self.note:
            parts.append(f"— {self.note}")
        return " ".join(parts)


async def get_state(session: AsyncSession, user_id: int) -> AvailabilityState | None:
    return (
        await session.execute(
            select(AvailabilityState).where(AvailabilityState.user_id == user_id)
        )
    ).scalar_one_or_none()


async def get_view(session: AsyncSession, user_id: int) -> AvailabilityView:
    """Состояние с проверкой срока: истёкшее показывается как OFFLINE."""
    state = await get_state(session, user_id)
    if state is None:
        return AvailabilityView(Availability.OFFLINE, None, None, False)

    current = Availability(state.state)
    if state.until_at is not None and state.until_at <= utcnow():
        current = Availability.OFFLINE

    if current == Availability.OFFLINE:
        return AvailabilityView(Availability.OFFLINE, None, None, False)

    return AvailabilityView(
        state=current,
        note=state.note,
        until_at=state.until_at,
        opens_late_slots=state.opens_late_slots,
    )


async def set_state(
    session: AsyncSession,
    *,
    user: User,
    state: Availability,
    minutes: int | None = DEFAULT_DURATION_MINUTES,
    note: str | None = None,
    opens_late_slots: bool = False,
    changed_by: int | None = None,
    visible_to_all: bool = True,
) -> AvailabilityView:
    """Переключает индикатор и закрывает предыдущий отрезок в истории."""
    now = utcnow()
    until = now + timedelta(minutes=minutes) if minutes else None
    actor_id = changed_by or user.id

    current = await get_state(session, user.id)
    before = None
    if current is not None:
        before = {"state": current.state, "until_at": current.until_at.isoformat() if current.until_at else None}
        open_log = (
            await session.execute(
                select(AvailabilityLog)
                .where(AvailabilityLog.user_id == user.id, AvailabilityLog.ended_at.is_(None))
                .order_by(AvailabilityLog.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if open_log is not None:
            open_log.ended_at = now

        current.state = state
        current.note = note
        current.until_at = until
        current.changed_at = now
        current.changed_by = actor_id
        current.opens_late_slots = opens_late_slots
        current.visible_to_all = visible_to_all
    else:
        current = AvailabilityState(
            user_id=user.id,
            state=state,
            note=note,
            until_at=until,
            changed_at=now,
            changed_by=actor_id,
            opens_late_slots=opens_late_slots,
            visible_to_all=visible_to_all,
        )
        session.add(current)

    session.add(
        AvailabilityLog(
            user_id=user.id,
            state=state,
            note=note,
            started_at=now,
            ended_at=None,
            changed_by=actor_id,
        )
    )
    await session.flush()

    await write_audit(
        session,
        actor_id=actor_id,
        on_behalf_of_id=user.id if actor_id != user.id else None,
        action="availability.set",
        entity_type="availability",
        entity_id=user.id,
        before=before,
        after={
            "state": state,
            "until_at": until.isoformat() if until else None,
            "opens_late_slots": opens_late_slots,
            "note": note,
        },
    )

    return AvailabilityView(state=state, note=note, until_at=until, opens_late_slots=opens_late_slots)


async def clear_state(session: AsyncSession, *, user: User, changed_by: int | None = None) -> None:
    await set_state(
        session,
        user=user,
        state=Availability.OFFLINE,
        minutes=None,
        note=None,
        opens_late_slots=False,
        changed_by=changed_by,
    )


async def open_executives(session: AsyncSession, organization_id: int) -> list[tuple[User, AvailabilityView]]:
    """Кто из руководителей сейчас принимает - для главного экрана сотрудника."""
    rows = await session.execute(
        select(User, AvailabilityState)
        .join(AvailabilityState, AvailabilityState.user_id == User.id)
        .where(
            User.organization_id == organization_id,
            AvailabilityState.state == Availability.OPEN,
            AvailabilityState.visible_to_all.is_(True),
        )
    )
    result: list[tuple[User, AvailabilityView]] = []
    now = utcnow()
    for user, state in rows.all():
        if state.until_at is not None and state.until_at <= now:
            continue
        result.append(
            (
                user,
                AvailabilityView(
                    state=Availability.OPEN,
                    note=state.note,
                    until_at=state.until_at,
                    opens_late_slots=state.opens_late_slots,
                ),
            )
        )
    return result
