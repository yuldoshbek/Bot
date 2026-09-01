"""Досье к встрече: что нужно знать за полчаса до неё.

Собирается системой, без модели: тема, участники, документы и прошлые решения —
всё это выборки, а не рассуждения. ИИ появится в блоке 6 и будет расставлять
акценты в готовых данных, а не добывать их.

Одна тонкость, ради которой досье и написано отдельной службой: **у каждого
получателя свой список документов**. Досье рассылается всем участникам, но
человеку, которому файл не открыт, он не показывается даже названием.
"""
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.text import cut, esc
from app.core.timeutil import to_local, utcnow
from app.models import (
    Decision,
    DecisionStatus,
    Meeting,
    MeetingParticipant,
    MeetingStatus,
    NotificationPriority,
    User,
)
from app.services import documents as document_service
from app.services.notifications import enqueue

# За сколько минут до начала уходит досье.
BRIEF_MINUTES = 30
# Насколько назад смотрим в поисках связанных решений.
LOOKBACK_DAYS = 180
MAX_ITEMS = 5


async def related_decisions(
    session: AsyncSession, *, meeting: Meeting, now: datetime | None = None
) -> list[Decision]:
    """Прошлые решения, относящиеся к этой встрече.

    Связь ищется двумя способами сразу: по людям (решение, за которое отвечает
    кто-то из участников) и по прошлым встречам с теми же участниками. Совпадение
    по формулировке темы намеренно не используется: «склад» в теме встречи
    и «склад» в решении полугодовой давности — слишком часто разные склады.
    """
    now = now or utcnow()
    since = now - timedelta(days=LOOKBACK_DAYS)

    people = select(MeetingParticipant.user_id).where(
        MeetingParticipant.meeting_id == meeting.id
    )
    shared_meetings = (
        select(MeetingParticipant.meeting_id)
        .where(
            MeetingParticipant.user_id.in_(people),
            MeetingParticipant.meeting_id != meeting.id,
        )
    )
    rows = await session.execute(
        select(Decision)
        .where(
            Decision.organization_id == meeting.organization_id,
            Decision.status == DecisionStatus.OPEN,
            Decision.created_at >= since,
            or_(
                Decision.responsible_id.in_(people),
                Decision.meeting_id.in_(shared_meetings),
            ),
        )
        .order_by(Decision.created_at.desc())
        .limit(MAX_ITEMS)
    )
    return list(rows.scalars().all())


async def build(
    session: AsyncSession, *, meeting: Meeting, viewer: User, now: datetime | None = None
) -> str:
    """Текст досье для конкретного человека."""
    now = now or utcnow()
    when = to_local(meeting.start_at, viewer.timezone).strftime("%H:%M")
    owner = await session.get(User, meeting.owner_id)

    people = (
        await session.execute(
            select(User)
            .join(MeetingParticipant, MeetingParticipant.user_id == User.id)
            .where(MeetingParticipant.meeting_id == meeting.id)
            .order_by(User.full_name)
        )
    ).scalars().all()

    lines = [
        f"📋 <b>Через {BRIEF_MINUTES} минут</b>",
        "",
        f"<b>{esc(meeting.title)}</b>",
        f"🕐 {when}, ведёт {esc(owner.full_name) if owner else 'неизвестно'}",
    ]
    if len(people) > 1:
        names = ", ".join(esc(p.full_name) for p in people if p.id != meeting.owner_id)
        lines.append(f"👥 {cut(names, 200)}")

    # Список документов свой у каждого: то, что человеку не открыто, он не
    # видит и названием. Иначе досье само становилось бы утечкой.
    files = await document_service.for_meeting(session, meeting=meeting, viewer=viewer)
    if files:
        lines += ["", "<b>Документы</b>"]
        lines += [f"📎 {esc(f.title or f.file_name)}" for f in files[:MAX_ITEMS]]

    open_decisions = await related_decisions(session, meeting=meeting, now=now)
    if open_decisions:
        lines += ["", "<b>Незакрытые решения по этим людям</b>"]
        for decision in open_decisions:
            responsible = (
                await session.get(User, decision.responsible_id)
                if decision.responsible_id else None
            )
            who = f" — {esc(responsible.full_name)}" if responsible else ""
            lines.append(f"• {esc(cut(decision.title, 120))}{who}")

    return "\n".join(lines)


async def send_briefings(session: AsyncSession, now: datetime | None = None) -> int:
    """Рассылает досье по встречам, начинающимся в ближайшие полчаса.

    Ключ события содержит номер переноса: перенесённая встреча — другое время,
    и досье к ней нужно заново.
    """
    now = now or utcnow()
    soon = now + timedelta(minutes=BRIEF_MINUTES)
    meetings = (
        await session.execute(
            select(Meeting).where(
                Meeting.status == MeetingStatus.CONFIRMED,
                Meeting.start_at > now,
                Meeting.start_at <= soon,
            ).limit(50)
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
            body = await build(session, meeting=meeting, viewer=person, now=now)
            created = await enqueue(
                session,
                user_id=person.id,
                organization_id=meeting.organization_id,
                event_key=f"meeting:{meeting.id}:brief:{meeting.reschedule_count}:u{person.id}",
                kind="meeting.brief",
                priority=NotificationPriority.NORMAL,
                body=body,
                payload={"meeting_id": meeting.id},
                timezone_name=person.timezone,
            )
            sent += int(created)
    return sent
