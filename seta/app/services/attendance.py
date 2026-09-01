"""Явка на встречу и оценка её пользы.

Две вещи, которые фиксируются после того, как встреча состоялась: кто пришёл
и стоила ли она потраченного времени. Обе — сырьё для аналитики блока 6, обе
пишутся один раз и защищены от повторов уникальностью в схеме, а не проверкой
в коде.

Система фиксирует факт и не делает выводов. «Опоздал на 7 минут» — запись,
а не обвинение: причину знает руководитель, а не таблица.
"""
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.text import esc
from app.core.timeutil import to_local, utcnow
from app.models import (
    AttendanceSource,
    Meeting,
    MeetingAttendance,
    MeetingParticipant,
    MeetingRating,
    MeetingStatus,
    NotificationPriority,
    User,
)
from app.services.audit import write_audit
from app.services.notifications import enqueue
from app.services.rbac import can_access_object, has_permission, load_grants

# За сколько минут до начала появляется кнопка «Я на месте».
CHECKIN_OPENS_MINUTES = 5

SCORE_LABELS = {1: "Полезная", 0: "Нейтральная", -1: "Бесполезная"}


# ── Явка ────────────────────────────────────────────────────────────────────
async def open_checkins(session: AsyncSession, now: datetime | None = None) -> int:
    """Рассылает участникам кнопку «Я на месте» за пять минут до начала.

    Ключ события содержит номер переноса: перенесённая встреча — это другое
    время, и напоминание о ней должно уйти заново.
    """
    now = now or utcnow()
    soon = now + timedelta(minutes=CHECKIN_OPENS_MINUTES)
    meetings = (
        await session.execute(
            select(Meeting).where(
                Meeting.status == MeetingStatus.CONFIRMED,
                Meeting.start_at > now,
                Meeting.start_at <= soon,
            ).limit(100)
        )
    ).scalars().all()

    sent = 0
    for meeting in meetings:
        people = (
            await session.execute(
                select(User)
                .join(MeetingParticipant, MeetingParticipant.user_id == User.id)
                .where(MeetingParticipant.meeting_id == meeting.id)
            )
        ).scalars().all()
        for person in people:
            when = to_local(meeting.start_at, person.timezone).strftime("%H:%M")
            created = await enqueue(
                session,
                user_id=person.id,
                organization_id=meeting.organization_id,
                event_key=f"meeting:{meeting.id}:checkin:{meeting.reschedule_count}:u{person.id}",
                kind="meeting.checkin",
                # Срочное по существу: письмо, доставленное после встречи,
                # не нужно никому.
                priority=NotificationPriority.CRITICAL,
                body=(
                    f"🕔 <b>Через {CHECKIN_OPENS_MINUTES} минут</b>\n\n"
                    f"{esc(meeting.title)}\nНачало в {when}"
                ),
                payload={"meeting_id": meeting.id, "checkin": True},
                timezone_name=person.timezone,
            )
            sent += int(created)
    return sent


async def check_in(
    session: AsyncSession,
    *,
    meeting: Meeting,
    user: User,
    now: datetime | None = None,
) -> tuple[bool, str | None]:
    """Человек отмечается сам. Возвращает (получилось, причина отказа).

    Окно открыто за пять минут до начала и закрывается вместе со встречей.
    Отметка после окончания — запись задним числом, она обесценивает весь
    журнал явки, поэтому не принимается; исправить может ассистент.

    Отметка после начала принимается и записывается как опоздание: человек,
    вошедший на седьмой минуте, присутствовал, и притворяться, что его не
    было, незачем.
    """
    now = now or utcnow()
    if meeting.status == MeetingStatus.CANCELLED:
        return False, "Встреча отменена."
    if now < meeting.start_at - timedelta(minutes=CHECKIN_OPENS_MINUTES):
        return False, "Отметиться можно за пять минут до начала."
    if now > meeting.end_at:
        return False, "Встреча закончилась — отметку теперь ставит ассистент."

    late = max(0, int((now - meeting.start_at).total_seconds() // 60))
    savepoint = await session.begin_nested()
    try:
        session.add(MeetingAttendance(
            meeting_id=meeting.id, user_id=user.id, present=True,
            checked_in_at=now, late_minutes=late,
            source=AttendanceSource.SELF, marked_by=user.id,
        ))
        await session.flush()
    except IntegrityError:
        # Двойное нажатие: запись уже есть, и это не ошибка.
        await savepoint.rollback()
        return False, "Вы уже отметились."
    return True, None


async def correct(
    session: AsyncSession,
    *,
    meeting: Meeting,
    user_id: int,
    actor: User,
    present: bool,
    late_minutes: int = 0,
    now: datetime | None = None,
) -> tuple[bool, str | None]:
    """Правка явки ассистентом или руководителем.

    Ограничения по времени здесь нет намеренно: правка тем и нужна, что
    делается после встречи, когда стало ясно, кто был.
    """
    now = now or utcnow()
    if actor.organization_id != meeting.organization_id:
        return False, "Встреча другой организации."

    grants = await load_grants(session, actor)
    if not has_permission(grants, "meeting.attendance"):
        return False, "Отмечать явку может руководитель или его ассистент."
    if not await can_access_object(
        session, actor, grants, "meeting.attendance", owner_id=meeting.owner_id
    ):
        return False, "Эта встреча вам не открыта."

    record = (
        await session.execute(
            select(MeetingAttendance).where(
                MeetingAttendance.meeting_id == meeting.id,
                MeetingAttendance.user_id == user_id,
            )
        )
    ).scalar_one_or_none()

    if record is None:
        record = MeetingAttendance(meeting_id=meeting.id, user_id=user_id)
        session.add(record)
    record.present = present
    record.late_minutes = max(0, late_minutes)
    record.checked_in_at = now if present else None
    record.source = AttendanceSource.ASSISTANT
    record.marked_by = actor.id
    await session.flush()

    await write_audit(
        session, actor_id=actor.id, action="meeting.attendance.correct",
        entity_type="meeting", entity_id=meeting.id,
        after={"user_id": user_id, "present": present},
    )
    return True, None


async def roll_call(session: AsyncSession, meeting: Meeting) -> list[tuple[User, bool, int]]:
    """Кто был на встрече: человек, был ли, на сколько опоздал.

    Участник без отметки считается отсутствовавшим — но именно как «нет
    записи», а не как установленный прогул.
    """
    people = (
        await session.execute(
            select(User)
            .join(MeetingParticipant, MeetingParticipant.user_id == User.id)
            .where(MeetingParticipant.meeting_id == meeting.id)
            .order_by(User.full_name)
        )
    ).scalars().all()
    marks = {
        row.user_id: row
        for row in (
            await session.execute(
                select(MeetingAttendance).where(MeetingAttendance.meeting_id == meeting.id)
            )
        ).scalars().all()
    }
    result = []
    for person in people:
        mark = marks.get(person.id)
        result.append((person, bool(mark and mark.present), mark.late_minutes if mark else 0))
    return result


# ── Оценка ──────────────────────────────────────────────────────────────────
async def rate(
    session: AsyncSession,
    *,
    meeting: Meeting,
    actor: User,
    score: int,
    comment: str | None = None,
    voice_file_id: str | None = None,
    now: datetime | None = None,
) -> tuple[bool, str | None]:
    """Оценка встречи одним касанием: 1, 0 или -1.

    Повторное нажатие меняет оценку, а не заводит вторую: передумать —
    нормально, а две оценки одной встречи от одного человека — нет.
    """
    now = now or utcnow()
    if score not in SCORE_LABELS:
        return False, "Оценка бывает только «полезная», «нейтральная» или «бесполезная»."
    if actor.organization_id != meeting.organization_id:
        return False, "Встреча другой организации."
    if meeting.status == MeetingStatus.CANCELLED:
        return False, "Отменённую встречу не оценивают."
    if now < meeting.end_at:
        return False, "Встреча ещё не закончилась."

    grants = await load_grants(session, actor)
    if not has_permission(grants, "meeting.rate"):
        return False, "Оценку ставит руководитель."
    if not await can_access_object(
        session, actor, grants, "meeting.rate", owner_id=meeting.owner_id
    ):
        return False, "Эта встреча вам не открыта."

    record = (
        await session.execute(
            select(MeetingRating).where(
                MeetingRating.meeting_id == meeting.id,
                MeetingRating.rated_by == actor.id,
            )
        )
    ).scalar_one_or_none()

    if record is None:
        record = MeetingRating(
            meeting_id=meeting.id, rated_by=actor.id, score=score, created_at=now
        )
        session.add(record)
    else:
        record.score = score
    if comment is not None:
        record.comment = comment.strip()[:2000] or None
    if voice_file_id is not None:
        # Расшифровка — блок 6. Пока сохраняем сам файл: пересказать голос
        # текстом позже можно, восстановить потерянный файл — нет.
        record.voice_file_id = voice_file_id
    await session.flush()
    return True, None


async def rating_for(
    session: AsyncSession, *, meeting: Meeting, viewer: User
) -> MeetingRating | None:
    """Оценка конкретной встречи — только руководителю и ассистенту.

    Сотрудник не должен видеть, как оценили встречу с его участием: это
    оценка встречи, а не человека, но прочитано будет именно как второе.
    """
    if viewer.organization_id != meeting.organization_id:
        return None
    grants = await load_grants(session, viewer)
    # Право ставить оценку — у руководителя, право отмечать явку — у него же
    # и у ассистента. Начальник отдела видит встречи своих людей, но не
    # оценки: «встреча была бесполезной» слишком легко читается как оценка
    # человека, а не события.
    allowed = (
        viewer.id == meeting.owner_id
        or has_permission(grants, "meeting.rate")
        or has_permission(grants, "meeting.attendance")
    )
    if not allowed:
        return None
    return (
        await session.execute(
            select(MeetingRating).where(MeetingRating.meeting_id == meeting.id)
        )
    ).scalars().first()


async def average_score(
    session: AsyncSession, *, organization_id: int, since: datetime
) -> tuple[float | None, int]:
    """Средняя польза встреч по организации: обезличенно.

    В общих выборках оценки живут без привязки к встрече и человеку —
    так их можно показывать кому угодно, не раскрывая ничего личного.
    """
    row = (
        await session.execute(
            select(func.avg(MeetingRating.score), func.count(MeetingRating.id))
            .join(Meeting, Meeting.id == MeetingRating.meeting_id)
            .where(
                Meeting.organization_id == organization_id,
                MeetingRating.created_at >= since,
            )
        )
    ).one()
    average, count = row
    return (float(average) if average is not None else None), int(count)
