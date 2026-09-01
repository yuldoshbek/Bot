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

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.dates import parse_hhmm
from app.core.text import esc
from app.core.timeutil import to_local, utcnow
from app.models import (
    Meeting,
    MeetingParticipant,
    MeetingRequest,
    MeetingStatus,
    NotificationPriority,
    ParticipantRole,
    RequestStatus,
    SlotHold,
    User,
    WorkingHours,
)
from app.services import quotas, slots as slot_service
from app.services.audit import write_audit
from app.services.notifications import enqueue
from app.services.rbac import Grant, Scope, can_access_object, has_permission, load_grants

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
            # Норма не запрещает, а помечает: срочный вопрос не должен
            # упираться в лимит, но и расходоваться время не должно молча.
            over_quota=await quotas.would_exceed(
                session, owner=owner, subject=initiator,
                minutes=duration_minutes, now=now,
            ),
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
            + ("\n\n⚠️ Сверх лимита времени" if request.over_quota else "")
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


# ── Жизненный цикл встречи ──────────────────────────────────────────────────
@dataclass
class Result:
    """Исход действия над встречей.

    Причина отказа — обычный ответ, а не исключение: «время только что заняли»
    и «это не ваша встреча» человек должен прочитать, а не увидеть сбой.
    """

    meeting: Meeting | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.meeting is not None


async def _may(
    session: AsyncSession,
    actor: User,
    permission: str,
    *,
    owner_id: int,
    related: set[int] | None = None,
    department_id: int | None = None,
) -> bool:
    """Право есть и открыто именно к этой встрече."""
    grants = await load_grants(session, actor)
    if not has_permission(grants, permission):
        return False
    return await can_access_object(
        session, actor, grants, permission,
        owner_id=owner_id, related_user_ids=related, department_id=department_id,
    )


async def participants_of(session: AsyncSession, meeting: Meeting) -> list[User]:
    """Все, кого встреча касается. Владелец среди них — он организатор."""
    return list(
        (
            await session.execute(
                select(User)
                .join(MeetingParticipant, MeetingParticipant.user_id == User.id)
                .where(MeetingParticipant.meeting_id == meeting.id)
            )
        ).scalars().all()
    )


async def _tell_everyone(
    session: AsyncSession,
    meeting: Meeting,
    *,
    key: str,
    kind: str,
    header: str,
    tail: str = "",
    priority: NotificationPriority = NotificationPriority.NORMAL,
    skip_id: int | None = None,
) -> int:
    """Одно событие — по письму каждому участнику.

    Ключ события общий для всех, различается только получателем: повторное
    нажатие кнопки не рассылает второй круг писем, за это отвечает
    уникальность `event_key` в схеме.
    """
    sent = 0
    for person in await participants_of(session, meeting):
        if person.id == skip_id:
            continue
        when = to_local(meeting.start_at, person.timezone).strftime("%d.%m в %H:%M")
        created = await enqueue(
            session,
            user_id=person.id,
            organization_id=meeting.organization_id,
            event_key=f"{key}:u{person.id}",
            kind=kind,
            priority=priority,
            body=f"{header}\n\n{esc(meeting.title)}\nКогда: {when}{tail}",
            payload={"meeting_id": meeting.id},
            timezone_name=person.timezone,
        )
        sent += int(created)
    return sent


async def approve(
    session: AsyncSession,
    *,
    request: MeetingRequest,
    actor: User,
    now: datetime | None = None,
) -> Result:
    """Подтверждает заявку и превращает её во встречу."""
    now = now or utcnow()
    if request.status != RequestStatus.NEW:
        return Result(reason="Решение по этой заявке уже принято.")
    if actor.organization_id != request.organization_id:
        return Result(reason="Заявка другой организации.")
    if not await _may(
        session, actor, "meeting.approve",
        owner_id=request.owner_id, related={request.initiator_id},
    ):
        return Result(reason="Подтверждать встречи может руководитель или его ассистент.")

    # Удержание защищало окно от заявок, но не от встречи, созданной напрямую.
    # Решает база: пересечение в календаре владельца физически невозможно.
    savepoint = await session.begin_nested()
    try:
        meeting = Meeting(
            organization_id=request.organization_id,
            owner_id=request.owner_id,
            title=request.title,
            start_at=request.start_at,
            end_at=request.end_at,
            status=MeetingStatus.CONFIRMED,
            created_by=actor.id,
            on_behalf_of_id=request.owner_id if actor.id != request.owner_id else None,
            request_id=request.id,
        )
        session.add(meeting)
        await session.flush()
    except IntegrityError:
        await savepoint.rollback()
        return Result(reason="Это время в календаре уже занято другой встречей.")

    for user_id, role in (
        (request.owner_id, ParticipantRole.ORGANIZER),
        (request.initiator_id, ParticipantRole.REQUIRED),
    ):
        session.add(MeetingParticipant(
            meeting_id=meeting.id, user_id=user_id, role=role, created_at=now
        ))

    request.status = RequestStatus.APPROVED
    request.decided_by = actor.id
    request.decided_at = now
    request.meeting_id = meeting.id
    await _release_hold(session, request.id, now)
    await session.flush()

    await _tell_everyone(
        session, meeting,
        key=f"meeting:{meeting.id}:confirmed",
        kind="meeting.confirmed",
        header="✅ <b>Встреча подтверждена</b>",
    )
    await write_audit(
        session, actor_id=actor.id, action="meeting.request.approve",
        entity_type="meeting", entity_id=meeting.id,
        after={"request_id": request.id, "start_at": meeting.start_at.isoformat()},
    )
    return Result(meeting=meeting)


async def reschedule(
    session: AsyncSession,
    *,
    meeting: Meeting,
    actor: User,
    new_start: datetime,
    reason: str,
    now: datetime | None = None,
) -> Result:
    """Переносит встречу и объясняет участникам, почему.

    Перенос без причины не принимается: получить «встреча теперь в 17:00»
    без объяснения — худший вид уведомления.
    """
    now = now or utcnow()
    reason = (reason or "").strip()
    if not reason:
        return Result(reason="Нужна причина переноса — её увидят все участники.")
    if meeting.status == MeetingStatus.CANCELLED:
        return Result(reason="Встреча отменена, переносить нечего.")
    if actor.organization_id != meeting.organization_id:
        return Result(reason="Встреча другой организации.")

    people = await participants_of(session, meeting)
    if not await _may(
        session, actor, "meeting.reschedule",
        owner_id=meeting.owner_id, related={p.id for p in people},
    ):
        return Result(reason="Переносить встречу может руководитель или его ассистент.")

    duration = meeting.end_at - meeting.start_at
    new_end = new_start + duration
    if new_start <= now:
        return Result(reason="Это время уже прошло.")
    owner = await session.get(User, meeting.owner_id)
    if owner is None:
        return Result(reason="Владелец календаря не найден.")
    if not await slot_service.is_free(
        session, owner=owner, start_at=new_start, end_at=new_end,
        exclude_meeting_id=meeting.id,
    ):
        return Result(reason="В это время у руководителя уже что-то стоит.")

    savepoint = await session.begin_nested()
    try:
        meeting.start_at = new_start
        meeting.end_at = new_end
        meeting.reschedule_count += 1
        await session.flush()
    except IntegrityError:
        await savepoint.rollback()
        return Result(reason="Это время только что заняли.")

    # Номер переноса в ключе: второй перенос — это новое событие, о нём
    # обязаны сообщить. Без версии повтор считался бы уже отправленным.
    await _tell_everyone(
        session, meeting,
        key=f"meeting:{meeting.id}:moved:{meeting.reschedule_count}",
        kind="meeting.rescheduled",
        header="🔄 <b>Встреча перенесена</b>",
        tail=f"\n\nПричина: {esc(reason)}",
    )
    await write_audit(
        session, actor_id=actor.id, action="meeting.reschedule",
        entity_type="meeting", entity_id=meeting.id,
        after={"start_at": new_start.isoformat()}, reason=reason,
    )
    return Result(meeting=meeting)


async def cancel(
    session: AsyncSession,
    *,
    meeting: Meeting,
    actor: User,
    reason: str,
    now: datetime | None = None,
) -> Result:
    """Отменяет встречу. Время сразу возвращается в оборот."""
    now = now or utcnow()
    reason = (reason or "").strip()
    if not reason:
        return Result(reason="Нужна причина отмены — её увидят все участники.")
    if meeting.status == MeetingStatus.CANCELLED:
        return Result(reason="Встреча уже отменена.")
    if actor.organization_id != meeting.organization_id:
        return Result(reason="Встреча другой организации.")

    people = await participants_of(session, meeting)
    if not await _may(
        session, actor, "meeting.cancel",
        owner_id=meeting.owner_id, related={p.id for p in people},
    ):
        return Result(reason="Отменять встречу может руководитель или его ассистент.")

    meeting.status = MeetingStatus.CANCELLED
    meeting.cancelled_at = now
    meeting.cancel_reason = reason[:500]
    await session.flush()

    await _tell_everyone(
        session, meeting,
        key=f"meeting:{meeting.id}:cancelled",
        kind="meeting.cancelled",
        header="🚫 <b>Встреча отменена</b>",
        tail=f"\n\nПричина: {esc(reason)}",
    )
    await write_audit(
        session, actor_id=actor.id, action="meeting.cancel",
        entity_type="meeting", entity_id=meeting.id, reason=reason,
    )
    return Result(meeting=meeting)


async def quick(
    session: AsyncSession,
    *,
    organizer: User,
    participant_ids: list[int],
    title: str,
    start_at: datetime,
    duration_minutes: int = 30,
    now: datetime | None = None,
) -> Result:
    """Быстрое совещание: три поля и рассылка.

    Заявок и подтверждений здесь нет — это инструмент того, кто и так вправе
    собрать людей. Уведомление уходит как срочное: совещание через двадцать
    минут, доставленное после тихих часов, бессмысленно.
    """
    now = now or utcnow()
    title = (title or "").strip()
    if not title:
        return Result(reason="Нужна тема совещания.")
    if start_at <= now:
        return Result(reason="Это время уже прошло.")
    if not await _may(
        session, organizer, "meeting.create", owner_id=organizer.id
    ):
        return Result(reason="Нет права создавать встречи.")

    end_at = start_at + timedelta(minutes=duration_minutes)
    people = list(
        (
            await session.execute(
                select(User).where(
                    User.id.in_(set(participant_ids) - {organizer.id}),
                    User.organization_id == organizer.organization_id,
                )
            )
        ).scalars().all()
    )
    if not people:
        return Result(reason="Некого собирать: участники не найдены в вашей организации.")

    savepoint = await session.begin_nested()
    try:
        meeting = Meeting(
            organization_id=organizer.organization_id,
            owner_id=organizer.id,
            title=title[:300],
            start_at=start_at,
            end_at=end_at,
            status=MeetingStatus.CONFIRMED,
            created_by=organizer.id,
        )
        session.add(meeting)
        await session.flush()
    except IntegrityError:
        await savepoint.rollback()
        return Result(reason="В это время у вас уже стоит другая встреча.")

    session.add(MeetingParticipant(
        meeting_id=meeting.id, user_id=organizer.id,
        role=ParticipantRole.ORGANIZER, created_at=now,
    ))
    for person in people:
        session.add(MeetingParticipant(
            meeting_id=meeting.id, user_id=person.id,
            role=ParticipantRole.REQUIRED, created_at=now,
        ))
    await session.flush()

    await _tell_everyone(
        session, meeting,
        key=f"meeting:{meeting.id}:called",
        kind="meeting.quick",
        header="📣 <b>Срочное совещание</b>",
        tail=f"\n\nСобирает: {esc(organizer.full_name)}",
        priority=NotificationPriority.CRITICAL,
        skip_id=organizer.id,
    )
    await write_audit(
        session, actor_id=organizer.id, action="meeting.quick",
        entity_type="meeting", entity_id=meeting.id,
        after={"participants": [p.id for p in people]},
    )
    return Result(meeting=meeting)


async def finish(
    session: AsyncSession,
    *,
    meeting: Meeting,
    actor: User,
    now: datetime | None = None,
) -> Result:
    """Завершает встречу. Это момент, когда собираются итоги.

    Завершение необратимо: «встреча состоялась» — факт, а не состояние, которое
    можно переключать туда-обратно. Карточка при этом остаётся: решения
    и поручения к ней продолжают привязываться.
    """
    now = now or utcnow()
    if actor.organization_id != meeting.organization_id:
        return Result(reason="Встреча другой организации.")
    if meeting.status == MeetingStatus.CANCELLED:
        return Result(reason="Встреча отменена, завершать нечего.")
    if meeting.status == MeetingStatus.FINISHED:
        return Result(reason="Встреча уже завершена.")
    if now < meeting.start_at:
        return Result(reason="Встреча ещё не началась.")

    people = await participants_of(session, meeting)
    if not await _may(
        session, actor, "meeting.finish",
        owner_id=meeting.owner_id, related={p.id for p in people},
    ):
        return Result(reason="Завершить встречу может организатор или его ассистент.")

    meeting.status = MeetingStatus.FINISHED
    meeting.finished_at = now
    await session.flush()

    await write_audit(
        session, actor_id=actor.id, action="meeting.finish",
        entity_type="meeting", entity_id=meeting.id,
        after={"finished_at": now.isoformat()},
    )
    return Result(meeting=meeting)


async def by_participant(
    session: AsyncSession, *, user: User, since: datetime, until: datetime,
    statuses: tuple[str, ...] | None = None,
) -> list[Meeting]:
    """Встречи человека за период. Общая выборка для карточек, досье и выгрузки."""
    query = (
        select(Meeting)
        .join(MeetingParticipant, MeetingParticipant.meeting_id == Meeting.id)
        .where(
            MeetingParticipant.user_id == user.id,
            Meeting.start_at >= since,
            Meeting.start_at < until,
        )
    )
    if statuses:
        query = query.where(Meeting.status.in_(statuses))
    return list(
        (await session.execute(query.order_by(Meeting.start_at).distinct())).scalars().all()
    )


def visible_filter(user: User, grants: dict[str, Grant], visible_departments: set[int]) -> list:
    """Условие видимости встреч в SQL: участник, владелец или область права."""
    if not has_permission(grants, "meeting.read"):
        return [Meeting.id.is_(None)]

    same_org = Meeting.organization_id == user.organization_id
    mine = or_(
        Meeting.owner_id == user.id,
        Meeting.id.in_(
            select(MeetingParticipant.meeting_id).where(
                MeetingParticipant.user_id == user.id
            )
        ),
    )
    scope = grants["meeting.read"].scope
    if scope == Scope.ORGANIZATION:
        return [same_org]
    if scope == Scope.DEPARTMENT and visible_departments:
        in_department = select(User.id).where(User.department_id.in_(visible_departments))
        return [same_org, or_(mine, Meeting.owner_id.in_(in_department))]
    return [same_org, mine]
