"""Заявки на встречу и удержание выбранного окна.

Главная мысль: **окно резервируется в тот же момент, когда человек его выбрал**,
а не когда руководитель ответит. Иначе между «выбрал» и «одобрено» пройдёт час,
и то же окно успеют выбрать ещё трое.

Удержание живёт до конца рабочего дня владельца или до решения — что раньше.
Освобождает его фоновый обработчик, а не тот, кто следующим откроет календарь:
опираться на приход посетителя нельзя, посетителя может не быть неделю.

Одновременность решается схемой, а не блокировкой в коде: `excl_slot_holds_overlap`
не даст появиться двум действующим удержаниям одного окна. Десять параллельных
заявок дают одно удержание и девять отказов — и отказ здесь не ошибка, а список
других свободных окон.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.dates import parse_hhmm
from app.core.text import esc
from app.core.timeutil import to_local, utcnow
from app.models import (
    MeetingRequest,
    NotificationPriority,
    RequestStatus,
    SlotHold,
    User,
    WorkingHours,
)
from app.services import slots as slot_service
from app.services.audit import write_audit
from app.services.notifications import enqueue

# Меньше этого удерживать бессмысленно: человек не успеет даже прочитать заявку.
MIN_HOLD_MINUTES = 30


@dataclass
class RequestOutcome:
    """Итог попытки занять окно.

    Отказ несёт варианты: человеку, который только что выбрал время и получил
    «занято», нужен следующий шаг, а не сообщение об ошибке.
    """

    request: MeetingRequest | None = None
    hold: SlotHold | None = None
    reason: str | None = None
    alternatives: list[slot_service.Slot] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.request is not None


async def _hold_until(
    session: AsyncSession, owner: User, start_at: datetime, now: datetime
) -> datetime:
    """До какого момента держать окно.

    До конца рабочего дня владельца: заявка, поданная утром, не должна висеть
    до следующей недели. Но никогда не дольше самого окна — держать время,
    которое уже началось, не за чем.
    """
    tz = ZoneInfo(owner.timezone)
    local_now = to_local(now, owner.timezone)
    hours = (
        await session.execute(
            select(WorkingHours).where(
                WorkingHours.user_id == owner.id,
                WorkingHours.weekday == local_now.weekday(),
            )
        )
    ).scalar_one_or_none()

    end_time = hours.end_time if hours else parse_hhmm(settings.work_end)
    day_end = local_now.replace(
        hour=end_time.hour, minute=end_time.minute, second=0, microsecond=0
    ).astimezone(tz)

    until = max(day_end, now + timedelta(minutes=MIN_HOLD_MINUTES))
    return min(until, start_at)


async def create_request(
    session: AsyncSession,
    *,
    initiator: User,
    owner: User,
    start_at: datetime,
    duration_minutes: int,
    title: str,
    now: datetime | None = None,
) -> RequestOutcome:
    """Заявка на встречу с немедленным удержанием окна.

    Возвращает исход, а не бросает исключение: «окно только что заняли» —
    обычный ход событий при живом календаре, а не сбой.
    """
    now = now or utcnow()
    end_at = start_at + timedelta(minutes=duration_minutes)

    if start_at <= now:
        return RequestOutcome(reason="Это время уже прошло.")
    if initiator.organization_id != owner.organization_id:
        return RequestOutcome(reason="Этот человек из другой организации.")

    async def _alternatives() -> list[slot_service.Slot]:
        return await slot_service.free_slots(
            session, owner=owner, duration_minutes=duration_minutes,
            days_ahead=7, limit=5, now=now,
        )

    if not await slot_service.is_free(
        session, owner=owner, start_at=start_at, end_at=end_at
    ):
        return RequestOutcome(
            reason="Это время уже занято.", alternatives=await _alternatives()
        )

    # Проверка выше смотрит на состояние до нас, а решает — база. Между
    # проверкой и вставкой окно может занять другой: победит тот, чья вставка
    # прошла, остальные попадут сюда же и получат варианты.
    savepoint = await session.begin_nested()
    try:
        request = MeetingRequest(
            organization_id=owner.organization_id,
            initiator_id=initiator.id,
            owner_id=owner.id,
            title=title.strip()[:300],
            duration_minutes=duration_minutes,
            start_at=start_at,
            end_at=end_at,
            status=RequestStatus.NEW,
        )
        session.add(request)
        await session.flush()

        hold = SlotHold(
            owner_id=owner.id,
            request_id=request.id,
            held_by=initiator.id,
            start_at=start_at,
            end_at=end_at,
            expires_at=await _hold_until(session, owner, start_at, now),
            created_at=now,
        )
        session.add(hold)
        await session.flush()
    except IntegrityError:
        await savepoint.rollback()
        return RequestOutcome(
            reason="Это время только что заняли.", alternatives=await _alternatives()
        )

    when = to_local(start_at, owner.timezone).strftime("%d.%m в %H:%M")
    await enqueue(
        session,
        user_id=owner.id,
        organization_id=owner.organization_id,
        event_key=f"meeting_request:{request.id}:new",
        kind="meeting.request",
        priority=NotificationPriority.NORMAL,
        body=(
            f"📅 <b>Запрос встречи</b>\n\n{esc(title)}\n"
            f"Кто: {esc(initiator.full_name)}\n"
            f"Когда: {when} · {duration_minutes} мин"
        ),
        payload={"request_id": request.id},
        timezone_name=owner.timezone,
    )
    await write_audit(
        session, actor_id=initiator.id, action="meeting.request.create",
        entity_type="meeting_request", entity_id=request.id,
        after={"owner_id": owner.id, "start_at": start_at.isoformat()},
    )
    return RequestOutcome(request=request, hold=hold)


async def _release_hold(session: AsyncSession, request_id: int, now: datetime) -> None:
    """Снимает удержание. Освобождение всегда идёт вместе с решением по заявке."""
    await session.execute(
        update(SlotHold)
        .where(SlotHold.request_id == request_id, SlotHold.released_at.is_(None))
        .values(released_at=now)
    )


async def decline(
    session: AsyncSession,
    *,
    request: MeetingRequest,
    actor: User,
    reason: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Отклоняет заявку и сразу освобождает окно.

    Возвращает False, если решение по заявке уже принято: две кнопки, нажатые
    подряд, не должны отправлять инициатору два разных ответа.
    """
    now = now or utcnow()
    if request.status != RequestStatus.NEW:
        return False

    request.status = RequestStatus.DECLINED
    request.decided_by = actor.id
    request.decided_at = now
    request.decline_reason = (reason or "").strip()[:500] or None
    await _release_hold(session, request.id, now)

    initiator = await session.get(User, request.initiator_id)
    if initiator is not None:
        when = to_local(request.start_at, initiator.timezone).strftime("%d.%m в %H:%M")
        tail = f"\n\nПричина: {esc(request.decline_reason)}" if request.decline_reason else ""
        await enqueue(
            session,
            user_id=initiator.id,
            organization_id=request.organization_id,
            event_key=f"meeting_request:{request.id}:declined",
            kind="meeting.request.declined",
            priority=NotificationPriority.NORMAL,
            body=f"❌ <b>Встреча отклонена</b>\n\n{esc(request.title)}\nБыло: {when}{tail}",
            payload={"request_id": request.id},
            timezone_name=initiator.timezone,
        )
    await write_audit(
        session, actor_id=actor.id, action="meeting.request.decline",
        entity_type="meeting_request", entity_id=request.id,
        after={"reason": request.decline_reason},
    )
    return True


async def expire_holds(session: AsyncSession, now: datetime | None = None) -> int:
    """Освобождает просроченные удержания и закрывает их заявки.

    Инициатора уведомляем обязательно: человек считает, что окно за ним.
    Молчаливое освобождение — худший из возможных вариантов.
    """
    now = now or utcnow()
    holds = (
        await session.execute(
            select(SlotHold).where(
                SlotHold.released_at.is_(None), SlotHold.expires_at <= now
            ).limit(200)
        )
    ).scalars().all()
    if not holds:
        return 0

    for hold in holds:
        hold.released_at = now
        if hold.request_id is None:
            continue
        request = await session.get(MeetingRequest, hold.request_id)
        if request is None or request.status != RequestStatus.NEW:
            continue

        request.status = RequestStatus.EXPIRED
        request.decided_at = now
        initiator = await session.get(User, request.initiator_id)
        if initiator is None:
            continue
        when = to_local(request.start_at, initiator.timezone).strftime("%d.%m в %H:%M")
        await enqueue(
            session,
            user_id=initiator.id,
            organization_id=request.organization_id,
            event_key=f"meeting_request:{request.id}:expired",
            kind="meeting.request.expired",
            priority=NotificationPriority.NORMAL,
            body=(
                f"⌛️ <b>Запрос без ответа</b>\n\n{esc(request.title)}\n"
                f"Было: {when}\n\nОкно освободилось — можно выбрать другое время."
            ),
            payload={"request_id": request.id},
            timezone_name=initiator.timezone,
        )
    return len(holds)


async def pending_for(session: AsyncSession, owner: User) -> list[MeetingRequest]:
    """Заявки, ждущие решения владельца календаря."""
    return list(
        (
            await session.execute(
                select(MeetingRequest)
                .where(
                    MeetingRequest.owner_id == owner.id,
                    MeetingRequest.status == RequestStatus.NEW,
                )
                .order_by(MeetingRequest.start_at)
            )
        ).scalars().all()
    )
