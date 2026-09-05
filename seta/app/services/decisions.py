"""Повестка встречи и реестр решений — что обсуждали и что решили.

Живут в одном месте, потому что связаны одним движением: пункт повестки
рассматривают и по нему принимают решение. Разводить их по разным службам —
значит каждый раз импортировать одну из другой.

Главное правило реестра: решение не удаляется. Его закрывают как выполненное
или отменяют с причиной, но строка остаётся. Реестр, из которого пропадают
записи, перестаёт отвечать на вопрос «что мы тогда решили».
"""
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import utcnow
from app.models import (
    AgendaItem,
    Decision,
    DecisionStatus,
    Meeting,
    MeetingParticipant,
    MeetingStatus,
    Task,
    User,
)
from app.services.audit import write_audit
from app.services.rbac import (
    Grant,
    Scope,
    can_access_object,
    has_permission,
    load_grants,
    scope_of,
    visible_department_ids,
)

STATUS_LABELS = {
    DecisionStatus.OPEN: "В работе",
    DecisionStatus.DONE: "Выполнено",
    DecisionStatus.CANCELLED: "Отменено",
}


@dataclass
class Outcome:
    """Исход действия: сама запись или причина отказа, читаемая человеком."""

    item: AgendaItem | Decision | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.item is not None


async def _participants(session: AsyncSession, meeting_id: int) -> set[int]:
    rows = await session.execute(
        select(MeetingParticipant.user_id).where(MeetingParticipant.meeting_id == meeting_id)
    )
    return {row[0] for row in rows.all()}


async def _may_touch_meeting(
    session: AsyncSession, actor: User, meeting: Meeting, permission: str
) -> bool:
    grants = await load_grants(session, actor)
    if not has_permission(grants, permission):
        return False
    return await can_access_object(
        session, actor, grants, permission,
        owner_id=meeting.owner_id,
        related_user_ids=await _participants(session, meeting.id),
    )


# ── Повестка ────────────────────────────────────────────────────────────────
async def add_agenda_item(
    session: AsyncSession,
    *,
    meeting: Meeting,
    actor: User,
    title: str,
    note: str | None = None,
) -> Outcome:
    """Добавляет пункт повестки в конец списка."""
    title = (title or "").strip()
    if len(title) < 3:
        return Outcome(reason="Слишком коротко: пункт повестки — хотя бы три знака.")
    if actor.organization_id != meeting.organization_id:
        return Outcome(reason="Встреча другой организации.")
    if meeting.status == MeetingStatus.CANCELLED:
        return Outcome(reason="Встреча отменена.")
    if meeting.status == MeetingStatus.FINISHED:
        return Outcome(reason="Встреча завершена, повестку уже не меняют.")
    if not await _may_touch_meeting(session, actor, meeting, "meeting.finish"):
        return Outcome(reason="Повестку ведёт организатор встречи или его ассистент.")

    last = await session.scalar(
        select(func.max(AgendaItem.position)).where(AgendaItem.meeting_id == meeting.id)
    )
    item = AgendaItem(
        meeting_id=meeting.id,
        position=(last or 0) + 1,
        title=title[:300],
        note=(note or "").strip()[:2000] or None,
        created_by=actor.id,
    )
    session.add(item)
    await session.flush()
    return Outcome(item=item)


async def agenda_of(session: AsyncSession, meeting: Meeting) -> list[AgendaItem]:
    """Повестка по порядку. Порядок задан полем, а не временем создания."""
    return list(
        (
            await session.execute(
                select(AgendaItem)
                .where(AgendaItem.meeting_id == meeting.id)
                .order_by(AgendaItem.position, AgendaItem.id)
            )
        ).scalars().all()
    )


async def mark_covered(
    session: AsyncSession, *, item: AgendaItem, meeting: Meeting, actor: User,
    covered: bool = True, now: datetime | None = None,
) -> Outcome:
    """Отмечает пункт рассмотренным. Обратное движение тоже разрешено."""
    now = now or utcnow()
    if item.meeting_id != meeting.id:
        return Outcome(reason="Этот пункт из другой встречи.")
    if not await _may_touch_meeting(session, actor, meeting, "meeting.finish"):
        return Outcome(reason="Отмечать пункты может организатор встречи или ассистент.")
    item.covered = covered
    item.covered_at = now if covered else None
    await session.flush()
    return Outcome(item=item)


# ── Решения ─────────────────────────────────────────────────────────────────
async def create(
    session: AsyncSession,
    *,
    actor: User,
    title: str,
    details: str | None = None,
    meeting: Meeting | None = None,
    agenda_item: AgendaItem | None = None,
    responsible: User | None = None,
    due_date: datetime | None = None,
) -> Outcome:
    """Вносит решение в реестр.

    Встреча необязательна: решение бывает принято и вне совещания, а через
    полгода вопрос «что мы решили по складу» задают без привязки к дате.
    """
    title = (title or "").strip()
    if len(title) < 3:
        return Outcome(reason="Нужна формулировка решения — хотя бы три знака.")
    grants = await load_grants(session, actor)
    if not has_permission(grants, "decision.create"):
        return Outcome(reason="Вносить решения может руководитель, ассистент или начальник отдела.")
    if meeting is not None:
        if meeting.organization_id != actor.organization_id:
            return Outcome(reason="Встреча другой организации.")
        if not await _may_touch_meeting(session, actor, meeting, "decision.create"):
            return Outcome(reason="Эта встреча вам не открыта.")
    if responsible is not None and responsible.organization_id != actor.organization_id:
        return Outcome(reason="Ответственный из другой организации.")

    decision = Decision(
        organization_id=actor.organization_id,
        meeting_id=meeting.id if meeting else None,
        agenda_item_id=agenda_item.id if agenda_item else None,
        title=title[:500],
        details=(details or "").strip()[:4000] or None,
        author_id=actor.id,
        responsible_id=responsible.id if responsible else None,
        due_date=due_date,
        status=DecisionStatus.OPEN,
    )
    session.add(decision)
    await session.flush()

    await write_audit(
        session, actor_id=actor.id, action="decision.create",
        entity_type="decision", entity_id=decision.id,
        after={"title": decision.title, "meeting_id": decision.meeting_id},
    )
    return Outcome(item=decision)


async def close(
    session: AsyncSession,
    *,
    decision: Decision,
    actor: User,
    done: bool,
    reason: str | None = None,
    now: datetime | None = None,
) -> Outcome:
    """Закрывает решение выполненным или отменяет его с причиной.

    Отмена требует причины: строка остаётся в реестре навсегда, и через год
    «отменено» без объяснения не отвечает ни на один вопрос.
    """
    now = now or utcnow()
    if decision.organization_id != actor.organization_id:
        return Outcome(reason="Решение другой организации.")
    if decision.status != DecisionStatus.OPEN:
        return Outcome(reason=f"Решение уже закрыто: {STATUS_LABELS[decision.status]}.")

    grants = await load_grants(session, actor)
    if not has_permission(grants, "decision.close"):
        return Outcome(reason="Закрывать решения может руководитель или его ассистент.")
    # Право закрывать не равно доступу к этому решению. Сегодня `decision.close`
    # есть только у ролей с областью ORGANIZATION, которым открыто всё, и разницы
    # не видно. Разница появится в тот день, когда право выдадут начальнику
    # отдела: без этой строки он закрывал бы чужие решения по номеру.
    if not await may_read(session, decision=decision, viewer=actor):
        return Outcome(reason="Это решение вам не открыто.")

    reason = (reason or "").strip()
    if not done and not reason:
        return Outcome(reason="Нужна причина отмены — она останется в реестре.")

    decision.status = DecisionStatus.DONE if done else DecisionStatus.CANCELLED
    decision.closed_at = now
    decision.closed_by = actor.id
    if not done:
        decision.cancel_reason = reason[:1000]
    await session.flush()

    await write_audit(
        session, actor_id=actor.id, action="decision.close",
        entity_type="decision", entity_id=decision.id,
        after={"status": decision.status}, reason=reason or None,
    )
    return Outcome(item=decision)


async def may_read(session: AsyncSession, *, decision: Decision, viewer: User) -> bool:
    """Открыто ли решение этому человеку. Единственная точка ответа на вопрос.

    Отвечает ровно то же, что условие `visible_filter` в SQL. Пара нужна потому,
    что список нельзя фильтровать после `LIMIT`, а одну запись нельзя открывать
    выборкой: «загрузим пятьсот видимых и посмотрим, есть ли он там» врёт,
    как только решений становится больше пятисот, — и врёт автору про его
    собственное решение. Совпадение проверяется прогоном по матрице
    «решение × человек» в `smoke_block4.py`.
    """
    if viewer.organization_id != decision.organization_id:
        return False

    grants = await load_grants(session, viewer)
    scope = scope_of(grants, "decision.read")
    if scope is None:
        return False
    if scope == Scope.ORGANIZATION:
        return True

    mine = viewer.id in (decision.author_id, decision.responsible_id)
    if scope == Scope.DEPARTMENT:
        # Порядок ветвей повторяет visible_filter буквально, включая случай
        # «отделов не видно вообще»: там условие запроса заведомо ложно,
        # значит и здесь ответ «нет» — даже на собственное решение.
        visible = await visible_department_ids(session, viewer)
        if not visible:
            return False
        if mine:
            return True
        people = set(
            (
                await session.execute(
                    select(User.id).where(User.department_id.in_(visible))
                )
            ).scalars().all()
        )
        return decision.author_id in people or decision.responsible_id in people
    return mine


def visible_filter(
    user: User, grants: dict[str, Grant], visible_departments: set[int]
) -> list:
    """Условие видимости решений — в SQL, а не после выборки.

    Отфильтровать список уже после `LIMIT` значит показать пустую страницу там,
    где должны быть свои записи, и решить, что права работают.

    Всегда возвращает список условий: одна форма ответа вместо развилки, на
    которой рано или поздно забудут раскрыть кортеж.
    """
    scope = scope_of(grants, "decision.read")
    if scope is None:
        # Прав нет: заведомо ложное условие. Запрос остаётся корректным,
        # выдача пустой — это честнее, чем не выполнять запрос вовсе.
        return [Decision.id.is_(None)]

    mine = or_(Decision.author_id == user.id, Decision.responsible_id == user.id)
    same_org = Decision.organization_id == user.organization_id

    if scope == Scope.ORGANIZATION:
        return [same_org]
    if scope == Scope.DEPARTMENT:
        if not visible_departments:
            return [Decision.id.is_(None)]
        in_department = select(User.id).where(User.department_id.in_(visible_departments))
        return [
            same_org,
            or_(
                mine,
                Decision.author_id.in_(in_department),
                Decision.responsible_id.in_(in_department),
            ),
        ]
    return [same_org, mine]


async def registry(
    session: AsyncSession,
    *,
    user: User,
    grants: dict[str, Grant],
    only_open: bool = False,
    meeting_id: int | None = None,
    limit: int = 20,
) -> list[Decision]:
    """Реестр решений, видимых этому человеку."""
    visible = await visible_department_ids(session, user)
    query = select(Decision).where(*visible_filter(user, grants, visible))
    if only_open:
        query = query.where(Decision.status == DecisionStatus.OPEN)
    if meeting_id is not None:
        query = query.where(Decision.meeting_id == meeting_id)
    return list(
        (
            await session.execute(query.order_by(Decision.created_at.desc()).limit(limit))
        ).scalars().all()
    )


async def meeting_outcome(session: AsyncSession, meeting: Meeting) -> tuple[int, int]:
    """Сколько решений и поручений породила встреча.

    Основание показателя «встречи без результата»: считает система, не человек
    и не модель. Ноль и ноль — это встреча, которая ничего не изменила.
    """
    decisions = await session.scalar(
        select(func.count(Decision.id)).where(Decision.meeting_id == meeting.id)
    )
    tasks = await session.scalar(
        select(func.count(Task.id)).where(Task.meeting_id == meeting.id)
    )
    return int(decisions or 0), int(tasks or 0)
