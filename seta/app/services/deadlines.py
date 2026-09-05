"""Сроки, просрочки и эскалация.

Пороги утверждены в решении Р-10. Главное в них - эскалация **фильтрует**,
а не пересылает наверх всё подряд:

    за 48 часов   → исполнителю
    за 24 часа    → исполнителю
    в день срока  → исполнителю
    срок прошёл   → статус «Просрочено», исполнителю
    +1 день       → начальнику отдела
    +3 дня        → дополнительно ассистенту
    руководителю  → никогда поштучно, только в утренней сводке

Единственное исключение - поручение с флагом «на личном контроле»:
о его просрочке автор узнаёт сразу.

Три механизма делают проход дешёвым и надёжным:

1. **Ступень эскалации хранится в поручении.** Пока ступень не выросла, проход
   не пишет в базу ничего. Раньше он каждую минуту пытался вставить уведомление
   заново: конфликт по event_key его отбрасывал, но мёртвая строка оставалась
   и в таблице, и в индексе - база пухла на ровном месте.
2. **Ключ события содержит номер продления.** Иначе после продления срока
   повторная просрочка не уведомила бы никого: ключ уже занят прошлым разом.
3. **Порог - это «меньше или равно», а не узкое окно.** Если обработчик лежал
   два часа, напоминание не теряется: оно уйдёт следующим проходом, а от
   повторов защищает тот же event_key.
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dates import humanize_due
from app.core.text import esc
from app.core.timeutil import utcnow
from app.models.enums import (
    NotificationPriority,
    Priority,
    RoleCode,
    TaskEventKind,
    TaskStatus,
    UserStatus,
)
from app.models.org import Department
from app.models.rbac import Role, UserRole
from app.models.task import Task
from app.models.user import User
from app.services.notifications import enqueue
from app.services.tasks import add_event, mark_overdue

log = logging.getLogger("seta.deadlines")

# Статусы, в которых поручение ещё ждёт исполнителя.
PENDING_STATUSES = (
    TaskStatus.NEW,
    TaskStatus.ACKNOWLEDGED,
    TaskStatus.IN_PROGRESS,
    TaskStatus.BLOCKED,
    TaskStatus.OVERDUE,
)

# Ступени эскалации, хранятся в tasks.escalation_level.
LEVEL_NONE = 0
LEVEL_OVERDUE = 1      # сообщено исполнителю
LEVEL_DEPT_HEAD = 2    # подключён начальник отдела
LEVEL_ASSISTANT = 3    # подключён ассистент

# Насколько вперёд смотрит проход. Совпадает с частичным индексом ix_tasks_due_watch:
# стоимость прохода зависит от числа близких сроков, а не от размера архива.
LOOKAHEAD = timedelta(hours=48, minutes=10)


async def process(
    session: AsyncSession,
    now: datetime | None = None,
    organization_id: int | None = None,
) -> dict[str, int]:
    """Один проход по срокам. Возвращает счётчики для журнала работы.

    organization_id ограничивает проход одной организацией. В бою не задаётся
    (организация одна), но проверочным скриптам он обязателен: без него прогон
    теста переведёт боевые поручения в «Просрочено» и разошлёт живым людям
    настоящие уведомления.
    """
    now = now or utcnow()
    stats = {"reminded": 0, "overdue": 0, "escalated": 0}

    query = select(Task).where(
        Task.status.in_(PENDING_STATUSES),
        Task.due_at.isnot(None),
        Task.due_at <= now + LOOKAHEAD,
    )
    if organization_id is not None:
        query = query.where(Task.organization_id == organization_id)

    tasks = list((await session.execute(query)).scalars().all())
    if not tasks:
        return stats

    people = await _load_people(session, tasks)

    for task in tasks:
        assignee = people.get(task.assignee_id)
        if assignee is None:
            continue

        left = task.due_at - now
        version = task.extensions_count

        if left.total_seconds() > 0:
            stats["reminded"] += await _remind(session, task, assignee, left, version)
            continue

        if task.status != TaskStatus.OVERDUE:
            await mark_overdue(session, task)
            stats["overdue"] += 1

        overdue_days = (-left).days
        desired = _desired_level(task, overdue_days)
        if desired <= task.escalation_level:
            # Ступень уже пройдена: ни записи, ни сообщения.
            continue

        if task.escalation_level < LEVEL_OVERDUE:
            stats["reminded"] += await _notify_overdue(session, task, assignee, version)
            if task.personal_control:
                stats["escalated"] += await _escalate(
                    session, task, task.on_behalf_of_id or task.creator_id, people,
                    key=f"pc:{version}",
                    header="🔴 <b>Просрочено поручение на вашем контроле</b>",
                    priority=NotificationPriority.CRITICAL,
                )

        if desired >= LEVEL_DEPT_HEAD > task.escalation_level:
            head_id = await _department_head(session, task, people)
            stats["escalated"] += await _escalate(
                session, task, head_id, people,
                key=f"esc1:{version}",
                header=f"🔴 <b>Просрочка в вашем отделе: {overdue_days} дн.</b>",
            )

        if desired >= LEVEL_ASSISTANT > task.escalation_level:
            assistant_id = await _assistant_of(session, task.organization_id)
            stats["escalated"] += await _escalate(
                session, task, assistant_id, people,
                key=f"esc3:{version}",
                header=f"🔴 <b>Требует вмешательства: просрочка {overdue_days} дн.</b>",
            )

        task.escalation_level = desired

    return stats


def _desired_level(task: Task, overdue_days: int) -> int:
    """До какой ступени поручение должно дойти при такой просрочке."""
    if overdue_days >= 3 or (overdue_days >= 1 and task.priority == Priority.CRITICAL):
        return LEVEL_ASSISTANT
    if overdue_days >= 1:
        return LEVEL_DEPT_HEAD
    return LEVEL_OVERDUE


async def _remind(
    session: AsyncSession, task: Task, assignee: User, left: timedelta, version: int
) -> int:
    """Напоминание о приближении срока.

    Берётся самый близкий из пройденных порогов, а не все сразу: поручение
    со сроком через десять часов не должно получить три напоминания подряд.
    """
    if left <= timedelta(hours=4):
        code, when_text = "today", "сегодня"
    elif left <= timedelta(hours=24):
        code, when_text = "24", "завтра"
    elif left <= timedelta(hours=48):
        code, when_text = "48", "через 2 дня"
    else:
        return 0

    created = await enqueue(
        session,
        user_id=assignee.id,
        organization_id=task.organization_id,
        event_key=f"task:{task.id}:due:{code}:{version}",
        kind="task.due_soon",
        priority=NotificationPriority.NORMAL,
        body=(
            f"⏰ <b>Срок {when_text}</b>\n\n{esc(task.title)}\n"
            f"📅 {humanize_due(task.due_at, assignee.timezone, assignee.locale)}"
        ),
        payload={"task_id": task.id},
        timezone_name=assignee.timezone,
    )
    return 1 if created else 0


async def _notify_overdue(
    session: AsyncSession, task: Task, assignee: User, version: int
) -> int:
    created = await enqueue(
        session,
        user_id=assignee.id,
        organization_id=task.organization_id,
        event_key=f"task:{task.id}:overdue:{version}",
        kind="task.overdue",
        priority=_priority_of(task),
        body=(
            f"🔴 <b>Срок истёк</b>\n\n{esc(task.title)}\n"
            f"⏰ Был: {humanize_due(task.due_at, assignee.timezone, assignee.locale)}\n\n"
            "Отчитайтесь о выполнении или попросите перенести срок."
        ),
        payload={"task_id": task.id},
        timezone_name=assignee.timezone,
    )
    return 1 if created else 0


async def _escalate(
    session: AsyncSession,
    task: Task,
    recipient_id: int | None,
    people: dict[int, User],
    *,
    key: str,
    header: str,
    priority: NotificationPriority = NotificationPriority.NORMAL,
) -> int:
    if recipient_id is None or recipient_id == task.assignee_id:
        return 0

    recipient = people.get(recipient_id) or await session.get(User, recipient_id)
    if recipient is None or recipient.status != UserStatus.ACTIVE:
        return 0

    assignee = people.get(task.assignee_id)
    created = await enqueue(
        session,
        user_id=recipient_id,
        organization_id=task.organization_id,
        event_key=f"task:{task.id}:{key}",
        kind="task.escalation",
        priority=priority,
        body=(
            f"{header}\n\n{esc(task.title)}\n"
            f"👤 Исполнитель: {esc(assignee.full_name) if assignee else '—'}\n"
            f"⏰ Срок был: {humanize_due(task.due_at, recipient.timezone, recipient.locale)}"
        ),
        payload={"task_id": task.id},
        timezone_name=recipient.timezone,
    )
    if created:
        await add_event(session, task, actor_id=None, kind=TaskEventKind.ESCALATED,
                        comment=f"эскалация: {key}")
        return 1
    return 0


def _priority_of(task: Task) -> NotificationPriority:
    if task.priority == Priority.CRITICAL or task.personal_control:
        return NotificationPriority.CRITICAL
    return NotificationPriority.NORMAL


async def _department_head(
    session: AsyncSession, task: Task, people: dict[int, User]
) -> int | None:
    assignee = people.get(task.assignee_id)
    department_id = task.department_id or (assignee.department_id if assignee else None)
    if department_id is None:
        return None

    department = await session.get(Department, department_id)
    if department is not None and department.head_user_id:
        return department.head_user_id

    head = (
        await session.execute(
            select(User)
            .join(UserRole, UserRole.user_id == User.id)
            .join(Role, Role.id == UserRole.role_id)
            .where(
                Role.code == RoleCode.DEPT_HEAD,
                User.department_id == department_id,
                User.status == UserStatus.ACTIVE,
            )
            .order_by(User.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    return head.id if head else None


async def _assistant_of(session: AsyncSession, organization_id: int) -> int | None:
    assistant = (
        await session.execute(
            select(User)
            .join(UserRole, UserRole.user_id == User.id)
            .join(Role, Role.id == UserRole.role_id)
            .where(
                Role.code == RoleCode.ASSISTANT,
                User.organization_id == organization_id,
                User.status == UserStatus.ACTIVE,
            )
            .order_by(User.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    return assistant.id if assistant else None


async def _load_people(session: AsyncSession, tasks: list[Task]) -> dict[int, User]:
    """Все участники разом: в цикле по поручениям обращений к базе быть не должно."""
    ids = set()
    for task in tasks:
        ids.update({task.assignee_id, task.creator_id, task.reviewer_id, task.on_behalf_of_id})
    ids.discard(None)
    if not ids:
        return {}
    rows = await session.execute(select(User).where(User.id.in_(ids)))
    return {user.id: user for user in rows.scalars().all()}
