"""Сроки, просрочки и эскалация.

Пороги утверждены в решении Р-10. Главное в них - эскалация **фильтрует**,
а не пересылает наверх всё подряд:

    за 48 часов   → исполнителю
    за 24 часа    → исполнителю
    срок прошёл   → статус «Просрочено», исполнителю
    +1 день       → исполнителю и начальнику отдела
    +3 дня        → дополнительно ассистенту
    руководителю  → никогда поштучно, только в утренней сводке

Единственное исключение - поручение с флагом «на личном контроле»:
о его просрочке автор узнаёт сразу.

Все уведомления ставятся с устойчивым event_key, поэтому повторный запуск
проверки (а она работает каждую минуту) не создаёт вторых сообщений.
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
from app.services.tasks import add_event

log = logging.getLogger("seta.deadlines")

# Статусы, в которых поручение ещё ждёт исполнителя.
PENDING_STATUSES = (
    TaskStatus.NEW,
    TaskStatus.ACKNOWLEDGED,
    TaskStatus.IN_PROGRESS,
    TaskStatus.BLOCKED,
    TaskStatus.OVERDUE,
)


async def process(session: AsyncSession, now: datetime | None = None) -> dict[str, int]:
    """Один проход по срокам. Возвращает счётчики для журнала работы."""
    now = now or utcnow()
    stats = {"reminded": 0, "overdue": 0, "escalated": 0}

    rows = await session.execute(
        select(Task).where(
            Task.status.in_(PENDING_STATUSES),
            Task.due_at.isnot(None),
        )
    )
    tasks = list(rows.scalars().all())
    if not tasks:
        return stats

    people = await _load_people(session, tasks)

    for task in tasks:
        assignee = people.get(task.assignee_id)
        if assignee is None:
            continue
        left = task.due_at - now

        if timedelta(hours=47) <= left <= timedelta(hours=48, minutes=5):
            stats["reminded"] += await _remind(session, task, assignee, "48", "через 2 дня")
        elif timedelta(hours=23) <= left <= timedelta(hours=24, minutes=5):
            stats["reminded"] += await _remind(session, task, assignee, "24", "завтра")
        elif timedelta(hours=3) <= left <= timedelta(hours=4, minutes=5):
            stats["reminded"] += await _remind(session, task, assignee, "today", "сегодня")

        if left.total_seconds() > 0:
            continue

        # Срок прошёл.
        if task.status != TaskStatus.OVERDUE:
            before = task.status
            task.status = TaskStatus.OVERDUE
            await add_event(
                session, task, actor_id=None, kind=TaskEventKind.OVERDUE,
                from_status=before, to_status=task.status,
            )
            stats["overdue"] += 1

        overdue_days = max(0, (-left).days)

        if await enqueue(
            session,
            user_id=assignee.id,
            event_key=f"task:{task.id}:overdue",
            kind="task.overdue",
            priority=_priority_of(task),
            body=(
                f"🔴 <b>Срок истёк</b>\n\n{esc(task.title)}\n"
                f"⏰ Был: {humanize_due(task.due_at, assignee.timezone)}\n\n"
                "Отчитайтесь о выполнении или попросите перенести срок."
            ),
            payload={"task_id": task.id},
            timezone_name=assignee.timezone,
        ):
            stats["reminded"] += 1

        # Поручение на личном контроле: автор узнаёт сразу, не дожидаясь сводки.
        if task.personal_control:
            stats["escalated"] += await _escalate(
                session, task, task.on_behalf_of_id or task.creator_id, people,
                key="pc",
                header="🔴 <b>Просрочено поручение на вашем контроле</b>",
                priority=NotificationPriority.CRITICAL,
            )

        if overdue_days >= 1:
            head_id = await _department_head(session, task, people)
            stats["escalated"] += await _escalate(
                session, task, head_id, people,
                key="esc1",
                header=f"🔴 <b>Просрочка в вашем отделе: {overdue_days} дн.</b>",
            )

        if overdue_days >= 3 or (
            overdue_days >= 1 and task.priority == Priority.CRITICAL
        ):
            assistant_id = await _assistant_of(session, task.organization_id)
            stats["escalated"] += await _escalate(
                session, task, assistant_id, people,
                key="esc3",
                header=f"🔴 <b>Требует вмешательства: просрочка {overdue_days} дн.</b>",
            )

    return stats


async def _remind(
    session: AsyncSession, task: Task, assignee: User, code: str, when_text: str
) -> int:
    created = await enqueue(
        session,
        user_id=assignee.id,
        event_key=f"task:{task.id}:due:{code}",
        kind="task.due_soon",
        priority=NotificationPriority.NORMAL,
        body=(
            f"⏰ <b>Срок {when_text}</b>\n\n{esc(task.title)}\n"
            f"📅 {humanize_due(task.due_at, assignee.timezone)}"
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
        event_key=f"task:{task.id}:{key}",
        kind="task.escalation",
        priority=priority,
        body=(
            f"{header}\n\n{esc(task.title)}\n"
            f"👤 Исполнитель: {esc(assignee.full_name) if assignee else '—'}\n"
            f"⏰ Срок был: {humanize_due(task.due_at, recipient.timezone)}"
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
            .limit(1)
        )
    ).scalar_one_or_none()
    return assistant.id if assistant else None


async def _load_people(session: AsyncSession, tasks: list[Task]) -> dict[int, User]:
    ids = set()
    for task in tasks:
        ids.update({task.assignee_id, task.creator_id, task.reviewer_id, task.on_behalf_of_id})
    ids.discard(None)
    if not ids:
        return {}
    rows = await session.execute(select(User).where(User.id.in_(ids)))
    return {user.id: user for user in rows.scalars().all()}
