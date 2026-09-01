"""Поручения: жизненный цикл, проверка качества, продление сроков.

Переходы состояний собраны в одном месте намеренно. Обработчики бота и API
не меняют статус напрямую - иначе через месяц появятся два разных способа
закрыть поручение, и один из них забудет записать событие в историю.

    NEW → ACKNOWLEDGED → IN_PROGRESS → REVIEW → DONE
                            ↑            │
                            └────────────┘  возврат на доработку

Дополнительно: BLOCKED, OVERDUE, CANCELLED.
"""
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dates import humanize_due
from app.core.text import esc
from app.core.timeutil import to_local, to_utc, utcnow
from app.models.enums import (
    ExtensionStatus,
    NotificationPriority,
    Priority,
    RoleCode,
    TaskEventKind,
    TaskStatus,
    UserStatus,
)
from app.models.rbac import Role, UserRole
from app.models.task import Task, TaskComment, TaskEvent, TaskExtension
from app.models.user import User
from app.services.audit import write_audit
from app.services.notifications import enqueue
from app.services.rbac import Grant, has_permission, user_role_codes, visible_department_ids

# Статусы, в которых поручение считается живым.
ACTIVE_STATUSES = (
    TaskStatus.NEW,
    TaskStatus.ACKNOWLEDGED,
    TaskStatus.IN_PROGRESS,
    TaskStatus.REVIEW,
    TaskStatus.BLOCKED,
    TaskStatus.OVERDUE,
)

STATUS_LABELS: dict[TaskStatus, str] = {
    TaskStatus.NEW: "🔵 Новое",
    TaskStatus.ACKNOWLEDGED: "🔵 Принято",
    TaskStatus.IN_PROGRESS: "🟡 В работе",
    TaskStatus.REVIEW: "🟠 На проверке",
    TaskStatus.DONE: "🟢 Выполнено",
    TaskStatus.BLOCKED: "🟠 Заблокировано",
    TaskStatus.OVERDUE: "🔴 Просрочено",
    TaskStatus.CANCELLED: "⚫ Отменено",
}

PRIORITY_LABELS: dict[Priority, str] = {
    Priority.LOW: "Низкий",
    Priority.NORMAL: "Обычный",
    Priority.HIGH: "🔴 Высокий",
    Priority.CRITICAL: "🔴 Критичный",
}


class TaskError(Exception):
    """Ошибка с текстом, который можно показать пользователю."""


@dataclass(slots=True)
class TaskAccess:
    can_view: bool = False
    can_accept: bool = False
    can_start: bool = False
    can_submit: bool = False
    can_review: bool = False
    can_cancel: bool = False
    can_comment: bool = False
    can_request_extension: bool = False
    can_decide_extension: bool = False


# ── Создание ────────────────────────────────────────────────────────────────
async def resolve_reviewer(
    session: AsyncSession, creator: User, requires_review: bool
) -> int | None:
    """Проверяющий по умолчанию - автор поручения.

    Но если автор руководитель, проверка уходит ассистенту: иначе руководитель
    утонет в кнопках «Принять» и начнёт жать их не глядя.
    """
    if not requires_review:
        return None

    # Роли берём через общую функцию: она учитывает срок действия так же,
    # как load_grants. Отдельный запрос без этого фильтра означал бы, что
    # истёкшая роль руководителя продолжает перенаправлять проверку ассистенту.
    creator_roles = await user_role_codes(session, creator)
    if RoleCode.EXECUTIVE not in creator_roles:
        return creator.id

    assistant = (
        await session.execute(
            select(User)
            .join(UserRole, UserRole.user_id == User.id)
            .join(Role, Role.id == UserRole.role_id)
            .where(
                Role.code == RoleCode.ASSISTANT,
                User.organization_id == creator.organization_id,
                # Приостановленный сотрудник не может быть проверяющим,
                # иначе поручение зависнет на проверке навсегда.
                User.status == UserStatus.ACTIVE,
            )
            .order_by(User.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    return assistant.id if assistant else creator.id


def default_requires_review(priority: Priority) -> bool:
    """Для важного проверка включается сама, для рутины остаётся выключенной."""
    return priority in (Priority.HIGH, Priority.CRITICAL)


async def create_task(
    session: AsyncSession,
    *,
    creator: User,
    assignee: User,
    title: str,
    description: str | None = None,
    due_at: datetime | None = None,
    priority: Priority = Priority.NORMAL,
    requires_review: bool | None = None,
    personal_control: bool = False,
    on_behalf_of_id: int | None = None,
    meeting_id: int | None = None,
) -> Task:
    if not title.strip():
        raise TaskError("У поручения должно быть название.")

    needs_review = (
        default_requires_review(priority) if requires_review is None else requires_review
    )

    task = Task(
        organization_id=creator.organization_id,
        title=title.strip()[:300],
        description=(description or "").strip() or None,
        creator_id=creator.id,
        assignee_id=assignee.id,
        reviewer_id=await resolve_reviewer(session, creator, needs_review),
        on_behalf_of_id=on_behalf_of_id,
        department_id=assignee.department_id,
        due_at=due_at,
        priority=priority,
        status=TaskStatus.NEW,
        requires_review=needs_review,
        personal_control=personal_control,
        meeting_id=meeting_id,
    )
    session.add(task)
    await session.flush()

    await add_event(session, task, actor_id=creator.id, kind=TaskEventKind.CREATED,
                    to_status=TaskStatus.NEW)
    await write_audit(
        session,
        actor_id=creator.id,
        on_behalf_of_id=on_behalf_of_id,
        action="task.create",
        entity_type="task",
        entity_id=task.id,
        after={
            "title": task.title,
            "assignee_id": assignee.id,
            "due_at": due_at.isoformat() if due_at else None,
            "priority": priority,
            "requires_review": needs_review,
        },
    )

    author = esc(creator.full_name)
    if on_behalf_of_id:
        principal = await session.get(User, on_behalf_of_id)
        if principal:
            author = f"{esc(creator.full_name)} по поручению: {esc(principal.full_name)}"

    due_line = f"\n⏰ Срок: {humanize_due(due_at, assignee.timezone)}" if due_at else ""
    await enqueue(
        session,
        user_id=assignee.id,
        organization_id=task.organization_id,
        event_key=f"task:{task.id}:assigned",
        kind="task.assigned",
        priority=(
            NotificationPriority.CRITICAL
            if priority == Priority.CRITICAL
            else NotificationPriority.NORMAL
        ),
        body=(
            f"📋 <b>Новое поручение</b>\n\n"
            f"{esc(task.title)}{due_line}\n"
            f"🔺 Приоритет: {PRIORITY_LABELS[Priority(priority)]}\n"
            f"👤 От: {author}"
        ),
        payload={"task_id": task.id},
        timezone_name=assignee.timezone,
    )
    return task


async def add_event(
    session: AsyncSession,
    task: Task,
    *,
    actor_id: int | None,
    kind: TaskEventKind,
    from_status: str | None = None,
    to_status: str | None = None,
    comment: str | None = None,
) -> None:
    session.add(
        TaskEvent(
            task_id=task.id,
            actor_id=actor_id,
            kind=kind,
            from_status=from_status,
            to_status=to_status,
            comment=comment,
            created_at=utcnow(),
        )
    )
    await session.flush()


# ── Доступ ──────────────────────────────────────────────────────────────────
async def access_for(
    session: AsyncSession, task: Task, user: User, grants: dict[str, Grant]
) -> TaskAccess:
    """Что этот человек может сделать с этим поручением.

    Права роли проверяются вместе с отношением к записи: наличие task.read
    само по себе не открывает чужое поручение.
    """
    access = TaskAccess()
    if not has_permission(grants, "task.read"):
        return access

    is_assignee = task.assignee_id == user.id
    is_creator = task.creator_id == user.id
    is_reviewer = task.reviewer_id == user.id
    is_principal = task.on_behalf_of_id == user.id

    scope_all = grants["task.read"].scope == "ORGANIZATION"
    in_department = False
    if not scope_all and grants["task.read"].scope == "DEPARTMENT":
        in_department = task.department_id in await visible_department_ids(session, user)

    access.can_view = (
        is_assignee or is_creator or is_reviewer or is_principal or scope_all or in_department
    )
    if not access.can_view:
        return access

    status = TaskStatus(task.status)
    access.can_comment = True
    access.can_accept = is_assignee and status == TaskStatus.NEW
    access.can_start = is_assignee and status == TaskStatus.ACKNOWLEDGED
    access.can_submit = is_assignee and status in (
        TaskStatus.ACKNOWLEDGED, TaskStatus.IN_PROGRESS, TaskStatus.OVERDUE, TaskStatus.BLOCKED
    )
    access.can_review = (
        status == TaskStatus.REVIEW
        and (is_reviewer or is_creator or is_principal)
        and has_permission(grants, "task.review")
    )
    access.can_cancel = (is_creator or is_principal) and status not in (
        TaskStatus.DONE, TaskStatus.CANCELLED
    )
    access.can_request_extension = (
        is_assignee and task.due_at is not None
        and status in (TaskStatus.NEW, TaskStatus.ACKNOWLEDGED, TaskStatus.IN_PROGRESS,
                       TaskStatus.OVERDUE, TaskStatus.BLOCKED)
    )
    access.can_decide_extension = (is_creator or is_principal) and has_permission(
        grants, "task.extend"
    )
    return access


# ── Переходы ────────────────────────────────────────────────────────────────
async def accept(session: AsyncSession, task: Task, actor: User) -> None:
    _require(task.status == TaskStatus.NEW, "Поручение уже принято.")
    before = task.status
    task.status = TaskStatus.ACKNOWLEDGED
    task.accepted_at = utcnow()
    await add_event(session, task, actor_id=actor.id, kind=TaskEventKind.ACCEPTED,
                    from_status=before, to_status=task.status)
    await _notify(
        session, task.creator_id,
        f"task:{task.id}:accepted", "task.accepted", NotificationPriority.LOW,
        f"✅ {esc(actor.full_name)} принял поручение: {esc(task.title)}",
        task.id,
    )


async def start(session: AsyncSession, task: Task, actor: User) -> None:
    _require(
        task.status in (TaskStatus.NEW, TaskStatus.ACKNOWLEDGED, TaskStatus.BLOCKED),
        "Поручение уже в работе.",
    )
    before = task.status
    task.status = TaskStatus.IN_PROGRESS
    task.started_at = task.started_at or utcnow()
    await add_event(session, task, actor_id=actor.id, kind=TaskEventKind.STARTED,
                    from_status=before, to_status=task.status)


async def submit(
    session: AsyncSession, task: Task, actor: User, comment: str | None = None
) -> TaskStatus:
    """Исполнитель отчитался. Дальше - проверка или сразу закрытие."""
    # Множество статусов совпадает с TaskAccess.can_submit: правило перехода
    # обязано жить в самом переходе, иначе API блока 5 пойдёт мимо проверки.
    _require(
        task.status in (
            TaskStatus.ACKNOWLEDGED, TaskStatus.IN_PROGRESS,
            TaskStatus.OVERDUE, TaskStatus.BLOCKED,
        ),
        "Отчитаться по этому поручению сейчас нельзя.",
    )
    before = task.status
    task.submitted_at = utcnow()

    if task.requires_review and task.reviewer_id:
        task.status = TaskStatus.REVIEW
        await add_event(session, task, actor_id=actor.id, kind=TaskEventKind.SUBMITTED,
                        from_status=before, to_status=task.status, comment=comment)
        await _notify(
            session, task.reviewer_id,
            f"task:{task.id}:review:{task.rework_count}", "task.review_required",
            NotificationPriority.NORMAL,
            f"🟠 <b>Требуется проверка</b>\n\n{esc(task.title)}\n"
            f"👤 Исполнитель: {esc(actor.full_name)}"
            + (f"\n💬 {esc(comment)}" if comment else ""),
            task.id,
        )
        return TaskStatus.REVIEW

    task.status = TaskStatus.DONE
    task.completed_at = utcnow()
    await add_event(session, task, actor_id=actor.id, kind=TaskEventKind.COMPLETED,
                    from_status=before, to_status=task.status, comment=comment)
    await _notify(
        session, task.creator_id,
        f"task:{task.id}:done", "task.done", NotificationPriority.NORMAL,
        f"🟢 <b>Поручение выполнено</b>\n\n{esc(task.title)}\n👤 {esc(actor.full_name)}"
        + (f"\n💬 {esc(comment)}" if comment else ""),
        task.id,
    )
    return TaskStatus.DONE


async def approve(
    session: AsyncSession, task: Task, actor: User, comment: str | None = None
) -> None:
    _require(task.status == TaskStatus.REVIEW, "Поручение не находится на проверке.")
    before = task.status
    task.status = TaskStatus.DONE
    task.completed_at = utcnow()
    await add_event(session, task, actor_id=actor.id, kind=TaskEventKind.APPROVED,
                    from_status=before, to_status=task.status, comment=comment)
    await write_audit(
        session, actor_id=actor.id, action="task.approve", entity_type="task",
        entity_id=task.id, before={"status": before}, after={"status": task.status},
    )
    await _notify(
        session, task.assignee_id,
        f"task:{task.id}:approved:{task.rework_count}", "task.approved",
        NotificationPriority.NORMAL,
        f"🟢 <b>Работа принята</b>\n\n{esc(task.title)}" + (f"\n💬 {esc(comment)}" if comment else ""),
        task.id,
    )


async def return_for_rework(
    session: AsyncSession, task: Task, actor: User, comment: str
) -> None:
    """Возврат на доработку. Комментарий обязателен: «переделай» без причины бесполезно."""
    _require(task.status == TaskStatus.REVIEW, "Поручение не находится на проверке.")
    if not comment.strip():
        raise TaskError("Напишите, что именно нужно доработать.")

    before = task.status
    task.status = TaskStatus.IN_PROGRESS
    task.rework_count += 1
    task.submitted_at = None
    await add_event(session, task, actor_id=actor.id, kind=TaskEventKind.RETURNED,
                    from_status=before, to_status=task.status, comment=comment)
    await _notify(
        session, task.assignee_id,
        f"task:{task.id}:returned:{task.rework_count}", "task.returned",
        NotificationPriority.NORMAL,
        f"🟠 <b>Возвращено на доработку</b>\n\n{esc(task.title)}\n💬 {esc(comment)}",
        task.id,
    )


async def mark_overdue(session: AsyncSession, task: Task) -> None:
    """Срок прошёл. Переход здесь, рядом с остальными: иначе появится второе
    место, где меняется статус, и оно забудет про историю и журнал."""
    if task.status == TaskStatus.OVERDUE:
        return
    before = task.status
    task.status = TaskStatus.OVERDUE
    await add_event(
        session, task, actor_id=None, kind=TaskEventKind.OVERDUE,
        from_status=before, to_status=task.status,
    )
    await write_audit(
        session, actor_id=None, action="task.overdue", entity_type="task",
        entity_id=task.id, before={"status": before}, after={"status": task.status},
        source="scheduler",
    )


async def cancel(session: AsyncSession, task: Task, actor: User, reason: str | None = None) -> None:
    _require(
        task.status not in (TaskStatus.DONE, TaskStatus.CANCELLED),
        "Поручение уже закрыто.",
    )
    before = task.status
    task.status = TaskStatus.CANCELLED
    task.cancelled_at = utcnow()
    await add_event(session, task, actor_id=actor.id, kind=TaskEventKind.CANCELLED,
                    from_status=before, to_status=task.status, comment=reason)
    await write_audit(
        session, actor_id=actor.id, action="task.cancel", entity_type="task",
        entity_id=task.id, before={"status": before}, after={"status": task.status}, reason=reason,
    )
    await _notify(
        session, task.assignee_id,
        f"task:{task.id}:cancelled", "task.cancelled", NotificationPriority.NORMAL,
        f"⚫ <b>Поручение отменено</b>\n\n{esc(task.title)}" + (f"\n💬 {esc(reason)}" if reason else ""),
        task.id,
    )


# ── Продление срока ─────────────────────────────────────────────────────────
async def request_extension(
    session: AsyncSession, task: Task, actor: User, new_due_at: datetime, reason: str
) -> TaskExtension:
    if not reason.strip():
        raise TaskError("Укажите причину переноса срока.")
    if task.due_at and new_due_at <= task.due_at:
        raise TaskError("Новый срок должен быть позже текущего.")

    # Один открытый запрос на поручение. Иначе десять нажатий кнопки дают автору
    # десять одинаковых карточек, и непонятно, какую из них он решает.
    if await pending_extension(session, task.id) is not None:
        raise TaskError(
            "Запрос на перенос уже отправлен — ждём решения автора поручения."
        )

    extension = TaskExtension(
        task_id=task.id,
        requested_by=actor.id,
        old_due_at=task.due_at,
        new_due_at=new_due_at,
        reason=reason.strip(),
        status=ExtensionStatus.NEW,
        created_at=utcnow(),
    )
    session.add(extension)
    await session.flush()

    decider_id = task.on_behalf_of_id or task.creator_id
    await _notify(
        session, decider_id,
        f"task:{task.id}:ext:{extension.id}", "task.extension_requested",
        NotificationPriority.NORMAL,
        f"⏰ <b>Просят перенести срок</b>\n\n{esc(task.title)}\n"
        f"👤 {esc(actor.full_name)}\n"
        f"Было: {humanize_due(task.due_at) if task.due_at else 'без срока'}\n"
        f"Станет: {humanize_due(new_due_at)}\n💬 {esc(reason.strip())}",
        task.id,
    )
    return extension


async def decide_extension(
    session: AsyncSession,
    extension: TaskExtension,
    task: Task,
    actor: User,
    *,
    approved: bool,
    comment: str | None = None,
) -> None:
    _require(extension.status == ExtensionStatus.NEW, "Запрос уже рассмотрен.")

    extension.status = ExtensionStatus.APPROVED if approved else ExtensionStatus.DECLINED
    extension.decided_by = actor.id
    extension.decided_at = utcnow()
    extension.decision_comment = comment

    if approved:
        before_due = task.due_at
        task.due_at = extension.new_due_at
        task.extensions_count += 1
        # Новый срок начинает цикл контроля заново: иначе поднятая ступень
        # эскалации молчала бы о повторной просрочке, и продление превращалось бы
        # в способ навсегда избавиться от напоминаний.
        task.escalation_level = 0
        if task.status == TaskStatus.OVERDUE:
            task.status = TaskStatus.IN_PROGRESS
        await add_event(
            session, task, actor_id=actor.id, kind=TaskEventKind.DUE_CHANGED,
            comment=extension.reason,
        )
        await write_audit(
            session, actor_id=actor.id, action="task.extend", entity_type="task",
            entity_id=task.id,
            before={"due_at": before_due.isoformat() if before_due else None},
            after={"due_at": task.due_at.isoformat()},
            reason=extension.reason,
        )

    verdict = "продлён" if approved else "оставлен прежним"
    await _notify(
        session, extension.requested_by,
        f"task:{task.id}:extdone:{extension.id}", "task.extension_decided",
        NotificationPriority.NORMAL,
        f"⏰ <b>Срок {verdict}</b>\n\n{esc(task.title)}\n"
        f"Срок: {humanize_due(task.due_at) if task.due_at else 'без срока'}"
        + (f"\n💬 {esc(comment)}" if comment else ""),
        task.id,
    )


async def pending_extension(session: AsyncSession, task_id: int) -> TaskExtension | None:
    return (
        await session.execute(
            select(TaskExtension)
            .where(TaskExtension.task_id == task_id, TaskExtension.status == ExtensionStatus.NEW)
            .order_by(TaskExtension.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


# ── Комментарии ─────────────────────────────────────────────────────────────
async def add_comment(
    session: AsyncSession,
    task: Task,
    author: User,
    *,
    text: str | None = None,
    telegram_file_id: str | None = None,
    file_name: str | None = None,
) -> TaskComment:
    if not text and not telegram_file_id:
        raise TaskError("Пустой комментарий добавить нельзя.")

    comment = TaskComment(
        task_id=task.id,
        author_id=author.id,
        text=text,
        telegram_file_id=telegram_file_id,
        file_name=file_name,
        created_at=utcnow(),
    )
    session.add(comment)
    await session.flush()
    await add_event(session, task, actor_id=author.id, kind=TaskEventKind.COMMENTED,
                    comment=(text or file_name or "файл")[:500])

    # Сообщаем другой стороне: автору - если пишет исполнитель, и наоборот.
    recipients = {task.creator_id, task.assignee_id, task.reviewer_id} - {author.id, None}
    for recipient in recipients:
        await _notify(
            session, recipient,
            f"task:{task.id}:comment:{comment.id}", "task.comment",
            NotificationPriority.LOW,
            f"💬 <b>{esc(author.full_name)}</b> в поручении «{esc(task.title)}»\n"
            + esc(text or f"📎 {file_name or 'файл'}"),
            task.id,
        )
    return comment


# ── Выборки ─────────────────────────────────────────────────────────────────
async def my_tasks(
    session: AsyncSession, user: User, *, bucket: str = "active", limit: int = 30
) -> list[Task]:
    """Поручения человека в разрезе, который ему нужен прямо сейчас."""
    query = select(Task).where(Task.assignee_id == user.id)

    if bucket == "active":
        query = query.where(Task.status.in_(ACTIVE_STATUSES))
    elif bucket == "today":
        # Граница дня считается в часовом поясе сотрудника, а не сервера:
        # «на сегодня» в Ташкенте и в UTC - разные множества задач.
        end_of_day = to_utc(
            to_local(utcnow(), user.timezone).replace(
                hour=23, minute=59, second=59, microsecond=0, tzinfo=None
            ),
            user.timezone,
        )
        query = query.where(
            Task.status.in_(ACTIVE_STATUSES),
            Task.due_at.isnot(None),
            Task.due_at <= end_of_day,
        )
    elif bucket == "overdue":
        query = query.where(Task.status == TaskStatus.OVERDUE)
    elif bucket == "review":
        query = select(Task).where(
            or_(Task.reviewer_id == user.id, Task.creator_id == user.id),
            Task.status == TaskStatus.REVIEW,
        )
    elif bucket == "created":
        query = select(Task).where(
            or_(Task.creator_id == user.id, Task.on_behalf_of_id == user.id),
            Task.status.in_(ACTIVE_STATUSES),
        )
    elif bucket == "done":
        query = query.where(Task.status == TaskStatus.DONE)

    rows = await session.execute(
        query.order_by(Task.due_at.is_(None), Task.due_at, Task.id.desc()).limit(limit)
    )
    return list(rows.scalars().all())


async def control_counters(
    session: AsyncSession,
    user: User,
    scope_all: bool,
    department_ids: set[int] | None = None,
) -> dict[str, int]:
    """Сводка для раздела «Контроль»."""
    base = select(func.count(Task.id)).where(Task.organization_id == user.organization_id)
    if not scope_all:
        mine = or_(
            Task.creator_id == user.id,
            Task.on_behalf_of_id == user.id,
            Task.reviewer_id == user.id,
        )
        # Начальник отдела отвечает за своё подразделение целиком, а не только
        # за то, что создал сам: без этой ветки «Контроль» показывал ему
        # неполные цифры - ровно по той роли, ради которой раздел и нужен.
        if department_ids:
            mine = or_(mine, Task.department_id.in_(department_ids))
        base = base.where(mine)

    async def count(*conditions) -> int:
        return await session.scalar(base.where(*conditions)) or 0

    return {
        "active": await count(Task.status.in_(ACTIVE_STATUSES)),
        "overdue": await count(Task.status == TaskStatus.OVERDUE),
        "review": await count(Task.status == TaskStatus.REVIEW),
        "done": await count(Task.status == TaskStatus.DONE),
        "critical": await count(
            Task.status.in_(ACTIVE_STATUSES), Task.priority == Priority.CRITICAL
        ),
    }


async def find_assignee(session: AsyncSession, organization_id: int, query: str) -> list[User]:
    """Поиск исполнителя по части имени."""
    # Экранируем шаблонные символы: иначе поиск по «%» вернёт всех подряд.
    escaped = query.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    rows = await session.execute(
        select(User)
        .where(
            User.organization_id == organization_id,
            User.status == "ACTIVE",
            func.lower(User.full_name).like(pattern, escape="\\"),
        )
        .order_by(User.full_name)
        .limit(10)
    )
    return list(rows.scalars().all())


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise TaskError(message)


async def _notify(
    session: AsyncSession,
    user_id: int | None,
    event_key: str,
    kind: str,
    priority: NotificationPriority,
    body: str,
    task_id: int,
) -> None:
    if user_id is None:
        return
    recipient = await session.get(User, user_id)
    await enqueue(
        session,
        user_id=user_id,
        organization_id=recipient.organization_id if recipient else None,
        event_key=event_key,
        kind=kind,
        priority=priority,
        body=body,
        payload={"task_id": task_id},
        timezone_name=recipient.timezone if recipient else None,
    )
