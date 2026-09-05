"""Досье к встрече: что нужно знать за полчаса до неё.

Собирается системой, без модели: тема, участники, документы и прошлые решения —
всё это выборки, а не рассуждения. ИИ появится в блоке 6 и будет расставлять
акценты в готовых данных, а не добывать их.

Одна тонкость, ради которой досье и написано отдельной службой: **у каждого
получателя свой список документов**. Досье рассылается всем участникам, но
человеку, которому файл не открыт, он не показывается даже названием.
"""
from dataclasses import dataclass, field
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


@dataclass(slots=True)
class Shared:
    """Часть досье, одинаковая для всех участников встречи.

    Своим у каждого получателя остаётся только список документов — их видимость
    зависит от человека. Тема, ведущий, состав и прошлые решения одни и те же,
    и добывать их заново на каждого — это те же выборки, повторённые столько
    раз, сколько человек в комнате.
    """

    owner: User | None = None
    people: list[User] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    responsible_names: dict[int, str] = field(default_factory=dict)


async def shared_for(
    session: AsyncSession, *, meeting: Meeting, now: datetime | None = None
) -> Shared:
    """Собирает общую часть досье — один раз на встречу."""
    now = now or utcnow()
    owner = await session.get(User, meeting.owner_id)
    people = list(
        (
            await session.execute(
                select(User)
                .join(MeetingParticipant, MeetingParticipant.user_id == User.id)
                .where(MeetingParticipant.meeting_id == meeting.id)
                .order_by(User.full_name)
            )
        ).scalars().all()
    )
    decisions = await related_decisions(session, meeting=meeting, now=now)

    # Имена ответственных — одним запросом, а не по одному внутри цикла.
    wanted = {d.responsible_id for d in decisions if d.responsible_id}
    names: dict[int, str] = {}
    if wanted:
        names = {
            row[0]: row[1]
            for row in (
                await session.execute(
                    select(User.id, User.full_name).where(User.id.in_(wanted))
                )
            ).all()
        }
    return Shared(owner=owner, people=people, decisions=decisions, responsible_names=names)


async def build(
    session: AsyncSession,
    *,
    meeting: Meeting,
    viewer: User,
    now: datetime | None = None,
    shared: Shared | None = None,
) -> str:
    """Текст досье для конкретного человека.

    `shared` передаёт рассылка, чтобы не собирать общую часть заново на каждого
    участника. Вызов без него остаётся корректным — просто дороже.
    """
    now = now or utcnow()
    shared = shared or await shared_for(session, meeting=meeting, now=now)
    when = to_local(meeting.start_at, viewer.timezone).strftime("%H:%M")
    owner = shared.owner
    people = shared.people

    lines = [
        f"📋 <b>Через {BRIEF_MINUTES} минут</b>",
        "",
        f"<b>{esc(meeting.title)}</b>",
        f"🕐 {when}, ведёт {esc(owner.full_name) if owner else 'неизвестно'}",
    ]
    if len(people) > 1:
        # Сначала обрезаем, потом экранируем: обратный порядок режет строку
        # посреди `&lt;`, и Telegram не доставляет сообщение целиком.
        names = ", ".join(p.full_name for p in people if p.id != meeting.owner_id)
        lines.append(f"👥 {esc(cut(names, 200))}")

    # Список документов свой у каждого: то, что человеку не открыто, он не
    # видит и названием. Иначе досье само становилось бы утечкой.
    files = await document_service.for_meeting(session, meeting=meeting, viewer=viewer)
    if files:
        lines += ["", "<b>Документы</b>"]
        lines += [f"📎 {esc(f.title or f.file_name)}" for f in files[:MAX_ITEMS]]

    if shared.decisions:
        lines += ["", "<b>Незакрытые решения по этим людям</b>"]
        for decision in shared.decisions:
            name = shared.responsible_names.get(decision.responsible_id or 0)
            who = f" — {esc(name)}" if name else ""
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
        # Общая часть — один раз на встречу, дальше по каждому получателю
        # добираются только его документы.
        shared = await shared_for(session, meeting=meeting, now=now)
        for person in shared.people:
            body = await build(
                session, meeting=meeting, viewer=person, now=now, shared=shared
            )
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
