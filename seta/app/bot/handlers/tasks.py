"""Поручения в боте.

Создание — четыре шага и подтверждение. Карточка показывает только те кнопки,
которые этому человеку сейчас доступны: исполнитель не увидит «Принять работу»,
автор не увидит «Отчитаться».
"""
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.common import MenuButton, MENU_CONTROL, MENU_MY_TASKS, MENU_NEW_TASK, main_menu
from app.bot.utils import callback_int
from app.core.dates import humanize_due, parse_due
from app.core.i18n import t
from app.core.text import cut, esc
from app.core.timeutil import fmt_dt
from app.models.enums import Priority, RoleCode, TaskStatus, UserStatus
from app.models.org import Organization
from app.models.task import Task, TaskComment, TaskEvent, TaskTemplate
from app.models.user import User
from app.services import tasks as service
from app.services import templates
from app.services import features as feature_service
from app.services.rbac import Grant, can_access_object, has_permission, visible_department_ids
from app.services.tasks import TaskError, priority_title, status_title

router = Router(name="tasks")

# Ключи, а не надписи: подпись зависит от языка, набор — нет.
BUCKETS = ("active", "today", "overdue", "review", "created", "done")
BUCKET_KEYS = {name: f"task.list.{name}" for name in BUCKETS}


class NewTask(StatesGroup):
    assignee = State()
    title = State()
    due = State()
    priority = State()


class UseTemplate(StatesGroup):
    assignee = State()


class TaskInput(StatesGroup):
    comment = State()
    rework = State()
    extension_date = State()
    extension_reason = State()


# ─────────────────────────  СОЗДАНИЕ  ─────────────────────────
@router.message(MenuButton(MENU_NEW_TASK))
async def new_task(
    message: Message, state: FSMContext, session: AsyncSession,
    organization: Organization, user: User, grants: dict[str, Grant],
    features: dict[str, bool], locale: str,
) -> None:
    if not has_permission(grants, "task.create"):
        await message.answer(t("task.new.no_rights", locale))
        return

    people = await _allowed_assignees(session, user, grants)
    if not people:
        await message.answer(t("task.new.nobody", locale))
        return

    await state.clear()
    ready = (
        await templates.catalogue(session, organization_id=user.organization_id)
        if feature_service.is_on(features, "templates")
        else []
    )
    if ready:
        await message.answer(
            f"<b>{t('template.title', locale)}</b>\n\n{t('template.subtitle', locale)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"📑 {cut(item.title, 40)}", callback_data=f"tt:use:{item.id}"
                )]
                for item in ready[:templates.QUICK_BUTTONS]
            ] + [[InlineKeyboardButton(
                text=t("template.all", locale), callback_data="tt:list"
            )]]),
        )
    await message.answer(
        f"<b>{t('task.new.title', locale)}</b>\n\n{t('task.new.ask_assignee', locale)}",
        reply_markup=_people_kb(people),
    )
    await state.set_state(NewTask.assignee)


@router.message(NewTask.assignee, F.text)
async def new_task_search(
    message: Message, session: AsyncSession, organization: Organization,
    user: User, grants: dict[str, Grant], locale: str,
) -> None:
    found = await service.find_assignee(session, organization.id, message.text)
    # Поиск тоже подчиняется области права, иначе он обходит список кандидатов.
    allowed = []
    for person in found:
        if await _may_assign_to(session, user, grants, person):
            allowed.append(person)
    if not allowed:
        await message.answer(t("task.new.nobody_found", locale))
        return
    await message.answer(t("task.new.who_meant", locale), reply_markup=_people_kb(allowed))


@router.callback_query(NewTask.assignee, F.data.startswith("nt:who:"))
async def new_task_assignee(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    user: User, grants: dict[str, Grant], locale: str,
) -> None:
    assignee_id = callback_int(call.data)
    assignee = await session.get(User, assignee_id) if assignee_id else None
    if assignee is None:
        await call.answer(t("task.new.assignee_not_found", locale), show_alert=True)
        return

    # Идентификатор пришёл от клиента: проверяем заново, а не доверяем кнопке.
    if not await _may_assign_to(session, user, grants, assignee):
        await call.answer(t("task.new.cannot_assign", locale), show_alert=True)
        return

    await state.update_data(assignee_id=assignee.id, assignee_name=assignee.full_name)
    await call.answer()
    await call.message.edit_text(
        f"<b>{t('task.new.title', locale)}</b>\n\n"
        f"👤 {t('task.new.to', locale)}: {esc(assignee.full_name)}"
    )
    await call.message.answer(t("task.new.ask_title", locale))
    await state.set_state(NewTask.title)


@router.message(NewTask.title, F.text)
async def new_task_title(message: Message, state: FSMContext, locale: str) -> None:
    title = message.text.strip()
    if len(title) < 3:
        await message.answer(t("task.new.too_short", locale))
        return

    await state.update_data(title=title)
    await message.answer(
        t("task.new.ask_due", locale),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=t("task.new.no_due", locale), callback_data="nt:due:none")
        ]]),
    )
    await state.set_state(NewTask.due)


@router.message(NewTask.due, F.text)
async def new_task_due(
    message: Message, state: FSMContext, user: User, locale: str
) -> None:
    due_at = parse_due(message.text, user.timezone)
    if due_at is None:
        await message.answer(t("task.new.bad_due", locale))
        return

    await state.update_data(due_at=due_at.isoformat())
    await message.answer(
        t("task.new.due_set", locale, due=humanize_due(due_at, user.timezone, locale))
        + "\n\n" + t("task.new.ask_priority", locale),
        reply_markup=_priority_kb(locale),
    )
    await state.set_state(NewTask.priority)


@router.callback_query(NewTask.due, F.data == "nt:due:none")
async def new_task_no_due(call: CallbackQuery, state: FSMContext, locale: str) -> None:
    await state.update_data(due_at=None)
    await call.answer()
    await call.message.edit_text(t("task.new.due_none", locale))
    await call.message.answer(t("task.new.ask_priority", locale),
                              reply_markup=_priority_kb(locale))
    await state.set_state(NewTask.priority)


@router.callback_query(NewTask.priority, F.data.startswith("nt:prio:"))
async def new_task_priority(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    user: User, roles: set[RoleCode], grants: dict[str, Grant], locale: str,
) -> None:
    from datetime import datetime

    try:
        priority = Priority(call.data.rsplit(":", 1)[1])
    except ValueError:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return

    data = await state.get_data()
    assignee = await session.get(User, data.get("assignee_id", 0))
    if assignee is None:
        await call.answer(t("task.new.executor_not_found", locale), show_alert=True)
        await state.clear()
        return

    if not await _may_assign_to(session, user, grants, assignee):
        await state.clear()
        await call.answer(t("task.new.cannot_assign", locale), show_alert=True)
        return

    due_at = datetime.fromisoformat(data["due_at"]) if data.get("due_at") else None

    # Ассистент действует от имени руководителя: в карточке видно обоих.
    on_behalf_of_id = None
    if RoleCode.ASSISTANT in roles and RoleCode.EXECUTIVE not in roles:
        executive = await _executive_of(session, user.organization_id)
        on_behalf_of_id = executive.id if executive else None

    try:
        task = await service.create_task(
            session,
            creator=user,
            assignee=assignee,
            title=data["title"],
            due_at=due_at,
            priority=priority,
            on_behalf_of_id=on_behalf_of_id,
        )
    except TaskError as error:
        await call.answer(str(error), show_alert=True)
        return

    await state.clear()
    await call.answer(t("task.new.created", locale))

    review_line = "\n" + t("task.new.needs_review", locale) if task.requires_review else ""
    when = (
        humanize_due(task.due_at, user.timezone, locale) if task.due_at
        else t("task.field.no_due", locale)
    )
    await call.message.edit_text(
        f"{t('task.new.created_title', locale)}\n\n"
        f"📋 {esc(cut(task.title, 200))}\n"
        f"👤 {esc(assignee.full_name)}\n"
        f"⏰ {when}\n"
        f"🔺 {priority_title(priority, locale)}{review_line}\n\n"
        f"{t('task.new.notified', locale)}",
        reply_markup=_task_kb_minimal(task.id, locale),
    )


# ─────────────────────────  СПИСКИ  ─────────────────────────
@router.message(MenuButton(MENU_MY_TASKS))
async def my_tasks(
    message: Message, session: AsyncSession, user: User, locale: str
) -> None:
    await _show_bucket(message, session, user, "active", locale)


@router.callback_query(F.data.startswith("tl:"))
async def switch_bucket(
    call: CallbackQuery, session: AsyncSession, user: User, locale: str
) -> None:
    bucket = call.data.rsplit(":", 1)[1]
    if bucket not in BUCKETS:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    await call.answer()
    await _show_bucket(call.message, session, user, bucket, locale, edit=True)


async def _show_bucket(
    message: Message, session: AsyncSession, user: User, bucket: str,
    locale: str, edit: bool = False,
) -> None:
    items = await service.my_tasks(session, user, bucket=bucket)
    title = t(BUCKET_KEYS.get(bucket, "task.list.title"), locale)

    if not items:
        text = f"<b>{title}</b>\n\n{t('task.list.empty', locale)}"
    else:
        lines = [f"<b>{title}: {len(items)}</b>", ""]
        for task in items:
            due = f" · {humanize_due(task.due_at, user.timezone, locale)}" if task.due_at else ""
            lines.append(
                f"{status_title(task.status, locale)}{due}\n📋 {esc(cut(task.title, 120))}"
            )
        text = "\n\n".join(lines)

    keyboard = _buckets_kb(bucket, items, locale)
    if edit:
        await message.edit_text(text, reply_markup=keyboard)
    else:
        await message.answer(text, reply_markup=keyboard)


@router.message(MenuButton(MENU_CONTROL))
async def control(
    message: Message, session: AsyncSession, user: User,
    grants: dict[str, Grant], locale: str,
) -> None:
    if not has_permission(grants, "task.read"):
        await message.answer(t("error.section_closed", locale))
        return

    scope_all = grants["task.read"].scope == "ORGANIZATION"
    department_ids = None
    if not scope_all and grants["task.read"].scope == "DEPARTMENT":
        department_ids = await visible_department_ids(session, user)
    counters = await service.control_counters(session, user, scope_all, department_ids)

    await message.answer(
        f"{t('task.control.title', locale)}\n\n"
        f"{t('task.control.working', locale)}: <b>{counters['active']}</b>\n"
        f"🔴 {t('task.control.overdue', locale)}: <b>{counters['overdue']}</b>\n"
        f"🟠 {t('task.control.review', locale)}: <b>{counters['review']}</b>\n"
        f"🔴 {t('task.control.critical', locale)}: <b>{counters['critical']}</b>\n"
        f"🟢 {t('task.control.done', locale)}: <b>{counters['done']}</b>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🔴 " + t("task.list.overdue", locale),
                                         callback_data="tl:overdue"),
                    InlineKeyboardButton(text="🟠 " + t("task.list.review", locale),
                                         callback_data="tl:review"),
                ],
                [InlineKeyboardButton(text=t("task.list.created", locale),
                                      callback_data="tl:created")],
            ]
        ),
    )


# ─────────────────────────  КАРТОЧКА  ─────────────────────────
@router.callback_query(F.data.startswith("t:open:"))
async def open_task(
    call: CallbackQuery, session: AsyncSession, user: User,
    grants: dict[str, Grant], locale: str,
) -> None:
    task_id = callback_int(call.data)
    task = await session.get(Task, task_id) if task_id else None
    if task is None:
        await call.answer(t("task.card.not_found", locale), show_alert=True)
        return

    access = await service.access_for(session, task, user, grants)
    if not access.can_view:
        await call.answer(t("task.card.no_access", locale), show_alert=True)
        return

    await call.answer()
    await call.message.answer(
        await _render_task(session, task, user, locale),
        reply_markup=_task_kb(task, access, locale),
    )


@router.callback_query(F.data.startswith("t:"))
async def task_action(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    user: User, grants: dict[str, Grant], locale: str,
) -> None:
    parts = call.data.split(":")
    if len(parts) < 3:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return

    action = parts[1]
    if action == "open":
        return

    task_id = callback_int(call.data, 2)
    if task_id is None:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return

    # Строка блокируется на время перехода: без этого десять одновременных
    # нажатий «Принять работу» прошли бы проверку статуса каждое и записали
    # десять событий в историю.
    task = await session.get(Task, task_id, with_for_update=True)
    if task is None:
        await call.answer(t("task.card.not_found", locale), show_alert=True)
        return

    access = await service.access_for(session, task, user, grants)
    if not access.can_view:
        await call.answer(t("task.card.no_access", locale), show_alert=True)
        return

    try:
        if action == "accept" and access.can_accept:
            await service.accept(session, task, user)
            await call.answer(t("task.act.accepted", locale))
        elif action == "start" and access.can_start:
            await service.start(session, task, user)
            await call.answer(t("task.act.started", locale))
        elif action == "submit" and access.can_submit:
            result = await service.submit(session, task, user)
            await call.answer(t(
                "task.act.submitted" if result == TaskStatus.REVIEW else "task.act.closed",
                locale,
            ))
        elif action == "approve" and access.can_review:
            await service.approve(session, task, user)
            await call.answer(t("task.act.approved", locale))
        elif action == "reject" and access.can_review:
            await state.update_data(task_id=task.id)
            await state.set_state(TaskInput.rework)
            await call.answer()
            await call.message.answer(t("task.act.ask_rework", locale))
            return
        elif action == "cancel" and access.can_cancel:
            await service.cancel(session, task, user)
            await call.answer(t("task.act.cancelled", locale))
        elif action == "comment":
            await state.update_data(task_id=task.id)
            await state.set_state(TaskInput.comment)
            await call.answer()
            await call.message.answer(t("task.act.ask_comment", locale))
            return
        elif action == "ext" and access.can_request_extension:
            await state.update_data(task_id=task.id)
            await state.set_state(TaskInput.extension_date)
            await call.answer()
            await call.message.answer(t("task.act.ask_new_due", locale))
            return
        elif action in ("extok", "extno") and access.can_decide_extension:
            extension = await service.pending_extension(session, task.id)
            if extension is None:
                await call.answer(t("task.act.already_decided", locale), show_alert=True)
                return
            await service.decide_extension(
                session, extension, task, user, approved=(action == "extok")
            )
            await call.answer(t(
                "task.act.extended" if action == "extok" else "task.act.declined", locale
            ))
        else:
            await call.answer(t("task.act.unavailable", locale), show_alert=True)
            return
    except TaskError as error:
        await call.answer(str(error), show_alert=True)
        return

    access = await service.access_for(session, task, user, grants)
    await call.message.edit_text(
        await _render_task(session, task, user, locale),
        reply_markup=_task_kb(task, access, locale),
    )


# ─────────────────────────  ВВОД ТЕКСТА  ─────────────────────────
@router.message(TaskInput.rework, F.text)
async def input_rework(
    message: Message, state: FSMContext, session: AsyncSession,
    user: User, grants: dict[str, Grant], locale: str,
) -> None:
    data = await state.get_data()
    task = await session.get(Task, data.get("task_id", 0), with_for_update=True)
    await state.clear()
    if task is None:
        return

    # Между нажатием кнопки и отправкой текста может пройти сколько угодно
    # времени: право могли отозвать, поручение отменить, проверяющего сменить.
    access = await service.access_for(session, task, user, grants)
    if not access.can_review:
        await message.answer(t("task.review.cannot", locale))
        return

    try:
        await service.return_for_rework(session, task, user, message.text)
    except TaskError as error:
        await message.answer(str(error))
        return

    access = await service.access_for(session, task, user, grants)
    await message.answer(t("task.review.returned", locale))
    await message.answer(
        await _render_task(session, task, user, locale),
        reply_markup=_task_kb(task, access, locale),
    )


@router.message(TaskInput.comment)
async def input_comment(
    message: Message, state: FSMContext, session: AsyncSession,
    user: User, grants: dict[str, Grant], locale: str,
) -> None:
    data = await state.get_data()
    task = await session.get(Task, data.get("task_id", 0))
    await state.clear()
    if task is None:
        return

    access = await service.access_for(session, task, user, grants)
    if not access.can_comment:
        await message.answer(t("task.card.no_access", locale))
        return

    file_id = file_name = None
    if message.document:
        file_id, file_name = message.document.file_id, message.document.file_name
    elif message.photo:
        file_id, file_name = message.photo[-1].file_id, t("task.comment.photo", locale)

    try:
        await service.add_comment(
            session, task, user,
            text=message.text or message.caption,
            telegram_file_id=file_id,
            file_name=file_name,
        )
    except TaskError as error:
        await message.answer(str(error))
        return

    await message.answer(t("task.comment.added", locale))


@router.message(TaskInput.extension_date, F.text)
async def input_extension_date(
    message: Message, state: FSMContext, user: User, locale: str
) -> None:
    new_due = parse_due(message.text, user.timezone)
    if new_due is None:
        await message.answer(t("task.extend.bad_date", locale))
        return

    await state.update_data(new_due=new_due.isoformat())
    await state.set_state(TaskInput.extension_reason)
    await message.answer(
        t("task.extend.new_due", locale,
          due=humanize_due(new_due, user.timezone, locale))
        + "\n\n" + t("task.extend.ask_reason", locale)
    )


@router.message(TaskInput.extension_reason, F.text)
async def input_extension_reason(
    message: Message, state: FSMContext, session: AsyncSession,
    user: User, grants: dict[str, Grant], locale: str,
) -> None:
    from datetime import datetime

    data = await state.get_data()
    task = await session.get(Task, data.get("task_id", 0), with_for_update=True)
    await state.clear()
    if task is None:
        return

    access = await service.access_for(session, task, user, grants)
    if not access.can_request_extension:
        await message.answer(t("task.extend.cannot", locale))
        return

    try:
        await service.request_extension(
            session, task, user, datetime.fromisoformat(data["new_due"]), message.text
        )
    except TaskError as error:
        await message.answer(str(error))
        return

    await message.answer(t("task.extend.sent", locale))


# ─────────────────────────  ОФОРМЛЕНИЕ  ─────────────────────────
async def _render_task(
    session: AsyncSession, task: Task, viewer: User, locale: str
) -> str:
    """Карточка поручения.

    Язык здесь обязателен, а не со значением по умолчанию: забытый на одном
    вызове он не выдал бы ошибки, а молча показал бы карточку на узбекском
    человеку, выбравшему русский. Такое находится только глазами. Обязательный
    аргумент превращает ту же оплошность в TypeError, который ловит проверка.
    """
    creator = await session.get(User, task.creator_id)
    assignee = await session.get(User, task.assignee_id)

    author = esc(creator.full_name) if creator else "—"
    if task.on_behalf_of_id:
        principal = await session.get(User, task.on_behalf_of_id)
        if principal:
            author = f"{author}{t('task.card.by_task', locale)}{esc(principal.full_name)}"

    lines = [
        f"📋 <b>{esc(cut(task.title, 300))}</b>",
        "",
        f"{t('task.field.status', locale)}: {status_title(task.status, locale)}",
        f"👤 {t('task.field.assignee', locale)}: {esc(assignee.full_name) if assignee else '—'}",
        f"✍️ {t('task.field.author', locale)}: {author}",
    ]
    if task.due_at:
        lines.append(
            f"⏰ {t('task.field.due', locale)}: "
            f"{humanize_due(task.due_at, viewer.timezone, locale)}"
        )
    lines.append(
        f"🔺 {t('task.field.priority', locale)}: {priority_title(task.priority, locale)}"
    )
    if task.description:
        lines += ["", esc(cut(task.description, 800))]
    if task.requires_review:
        reviewer = await session.get(User, task.reviewer_id) if task.reviewer_id else None
        lines.append(
            f"{t('task.card.checker', locale)}: "
            f"{esc(reviewer.full_name) if reviewer else '—'}"
        )
    if task.personal_control:
        lines.append(t("task.card.personal_control", locale))
    if task.rework_count:
        lines.append(f"{t('task.card.reworks', locale)}: {task.rework_count}")
    if task.extensions_count:
        lines.append(f"{t('task.card.extensions', locale)}: {task.extensions_count}")

    extension = await service.pending_extension(session, task.id)
    if extension is not None:
        lines += [
            "",
            f"{t('task.card.extension_asked', locale)} "
            f"{humanize_due(extension.new_due_at, viewer.timezone, locale)}",
            f"💬 {esc(cut(extension.reason, 300))}",
        ]

    comments = (
        await session.execute(
            select(TaskComment)
            .where(TaskComment.task_id == task.id)
            .order_by(TaskComment.created_at.desc())
            .limit(3)
        )
    ).scalars().all()
    if comments:
        lines += ["", f"<b>{t('task.comments.title', locale)}</b>"]
        for comment in reversed(list(comments)):
            author_user = await session.get(User, comment.author_id)
            name = esc(author_user.full_name) if author_user else "—"
            fallback = f"📎 {comment.file_name or t('task.comment.file', locale)}"
            body = esc(cut(comment.text or fallback, 200))
            lines.append(f"• {name}: {body}")

    last = (
        await session.execute(
            select(TaskEvent)
            .where(TaskEvent.task_id == task.id)
            .order_by(TaskEvent.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last is not None:
        lines.append(
            f"\n🕐 {t('task.last_change', locale)}: "
            f"{fmt_dt(last.created_at, viewer.timezone)}"
        )

    return "\n".join(lines)


def _task_kb(
    task: Task, access: service.TaskAccess, locale: str
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    tid = task.id

    def button(key: str, action: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=t(key, locale), callback_data=f"t:{action}:{tid}")

    if access.can_accept:
        rows.append([button("task.action.accept", "accept")])
    if access.can_start:
        rows.append([button("task.action.start", "start")])
    if access.can_submit:
        rows.append([button("task.action.submit", "submit")])
    if access.can_review:
        rows.append([button("task.action.approve", "approve"),
                     button("task.action.reject", "reject")])
    if access.can_decide_extension:
        rows.append([button("task.action.extend_ok", "extok"),
                     button("task.action.extend_no", "extno")])
    if access.can_request_extension:
        rows.append([button("task.action.extend", "ext")])

    bottom = [button("task.action.comment", "comment")]
    if access.can_cancel:
        bottom.append(button("task.action.cancel", "cancel"))
    rows.append(bottom)
    # Шаблон заводится из готового поручения: формулировка, приоритет и срок
    # у него уже есть, и переспрашивать их мастером значит просить ввести
    # дважды то, что система знает.
    rows.append([InlineKeyboardButton(
        text=t("task.action.save_template", locale), callback_data=f"tt:save:{tid}"
    )])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def _task_kb_minimal(task_id: int, locale: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t("common.open", locale), callback_data=f"t:open:{task_id}")
    ]])


def _buckets_kb(current: str, items: list[Task], locale: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for task in items[:8]:
        title = task.title if len(task.title) <= 32 else task.title[:31] + "…"
        rows.append([InlineKeyboardButton(text=f"📋 {title}", callback_data=f"t:open:{task.id}")])

    filters = [code for code in BUCKETS if code != current]
    for index in range(0, len(filters), 2):
        rows.append([
            InlineKeyboardButton(text=t(BUCKET_KEYS[code], locale), callback_data=f"tl:{code}")
            for code in filters[index:index + 2]
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _people_kb(people: list[User]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=person.full_name, callback_data=f"nt:who:{person.id}")]
            for person in people[:10]
        ]
    )


def _priority_kb(locale: str) -> InlineKeyboardMarkup:
    def button(priority: Priority) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=priority_title(priority, locale),
            callback_data=f"nt:prio:{priority.value}",
        )

    return InlineKeyboardMarkup(inline_keyboard=[
        [button(Priority.NORMAL), button(Priority.HIGH)],
        [button(Priority.CRITICAL)],
    ])


async def _allowed_assignees(
    session: AsyncSession, user: User, grants: dict[str, Grant]
) -> list[User]:
    """Кому этот человек вправе поручать.

    Право task.create есть и у рядового сотрудника, но с областью «только свои».
    Без учёта области список кандидатов включал бы всю организацию, и сотрудник
    мог бы назначить поручение руководителю.
    """
    scope = grants["task.create"].scope
    query = select(User).where(
        User.organization_id == user.organization_id,
        User.status == UserStatus.ACTIVE,
    )

    if scope == "SELF":
        query = query.where(User.id == user.id)
    elif scope == "DEPARTMENT":
        visible = await visible_department_ids(session, user)
        if not visible:
            return []
        query = query.where(User.department_id.in_(visible))
    elif scope == "SUBORDINATES":
        query = query.where(User.manager_id == user.id)

    rows = await session.execute(query.order_by(User.full_name).limit(20))
    return list(rows.scalars().all())


async def _may_assign_to(
    session: AsyncSession, user: User, grants: dict[str, Grant], assignee: User
) -> bool:
    """Проверка конкретной записи. Правило живёт в службе: то же самое нужно
    при создании поручения из шаблона, и двух описаний быть не должно."""
    return await service.may_assign_to(
        session, actor=user, grants=grants, assignee=assignee
    )


async def _executive_of(session: AsyncSession, organization_id: int) -> User | None:
    from app.models.rbac import Role, UserRole

    return (
        await session.execute(
            select(User)
            .join(UserRole, UserRole.user_id == User.id)
            .join(Role, Role.id == UserRole.role_id)
            .where(
                Role.code == RoleCode.EXECUTIVE,
                User.organization_id == organization_id,
                User.status == UserStatus.ACTIVE,
            )
            .limit(1)
        )
    ).scalar_one_or_none()


# ─────────────────────────  ШАБЛОНЫ  ─────────────────────────
# Типовое поручение в одно нажатие — функция 09 первой волны, долг блока 2.
# Заводится из готового поручения, применяется с экрана создания.


@router.callback_query(F.data.startswith("tt:save:"))
async def template_save(
    call: CallbackQuery, session: AsyncSession, user: User,
    grants: dict[str, Grant], features: dict[str, bool], locale: str,
) -> None:
    if not feature_service.is_on(features, "templates"):
        await call.answer(t("feature.off", locale), show_alert=True)
        return
    task = await session.get(Task, callback_int(call.data) or 0)
    if task is None:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    # Право видеть поручение проверяется тем же способом, что и везде:
    # из карточки, которую человеку не открыли, шаблон не заведёшь.
    access = await service.access_for(session, task, user, grants)
    if not access.can_view:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return

    result = await templates.from_task(session, task=task, actor=user, grants=grants)
    if not result.ok:
        await call.answer(result.reason or t("template.failed", locale), show_alert=True)
        return
    if result.duplicate:
        await call.answer(t("template.exists", locale), show_alert=True)
        return
    await call.answer(t("common.saved", locale))
    await call.message.answer(
        f"{t('template.saved_full', locale)}\n\n{esc(cut(result.item.title, 80))}\n"
        f"{t('template.due_hint', locale, days=result.item.default_days)}\n\n"
        f"{t('template.saved_hint', locale)}"
    )


@router.callback_query(F.data == "tt:list")
async def template_list(
    call: CallbackQuery, session: AsyncSession, user: User,
    features: dict[str, bool], locale: str,
) -> None:
    if not feature_service.is_on(features, "templates"):
        await call.answer(t("feature.off", locale), show_alert=True)
        return
    items = await templates.catalogue(session, organization_id=user.organization_id)
    if not items:
        await call.answer(t("template.empty", locale), show_alert=True)
        return

    lines = [t("template.list_title", locale), ""]
    rows: list[list[InlineKeyboardButton]] = []
    for item in items:
        due = (
            t("template.after_days", locale, days=item.default_days)
            if item.default_days else t("task.field.no_due", locale)
        )
        lines.append(f"📑 {esc(cut(item.title, 70))} — {due}")
        rows.append([
            InlineKeyboardButton(
                text=f"▶️ {cut(item.title, 28)}", callback_data=f"tt:use:{item.id}"
            ),
            InlineKeyboardButton(text="🗑", callback_data=f"tt:drop:{item.id}"),
        ])
    await call.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows[:8]))
    await call.answer()


@router.callback_query(F.data.startswith("tt:use:"))
async def template_use(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    user: User, grants: dict[str, Grant], features: dict[str, bool], locale: str,
) -> None:
    if not feature_service.is_on(features, "templates"):
        await call.answer(t("feature.off", locale), show_alert=True)
        return
    template = await session.get(TaskTemplate, callback_int(call.data) or 0)
    if template is None or template.organization_id != user.organization_id:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return

    result = await templates.apply(session, template=template, actor=user, grants=grants)
    if not result.ok:
        # Чаще всего причина одна: в шаблоне нет исполнителя. Тогда спрашиваем,
        # а не отказываем — шаблон без адресата всё равно экономит формулировку.
        if template.default_assignee_id is None:
            people = await _allowed_assignees(session, user, grants)
            if people:
                await state.clear()
                await state.update_data(template_id=template.id)
                await call.message.answer(
                    f"📑 <b>{esc(cut(template.title, 70))}</b>\n\n"
                    f"{t('template.ask_assignee', locale)}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(
                            text=person.full_name, callback_data=f"tt:to:{person.id}"
                        )]
                        for person in people[:7]
                    ]),
                )
                await state.set_state(UseTemplate.assignee)
                await call.answer()
                return
        await call.answer(result.reason or t("template.failed", locale), show_alert=True)
        return

    if result.duplicate:
        await call.answer(t("template.duplicate_task", locale), show_alert=True)
        return
    await call.answer(t("common.done", locale))
    await _announce_template(call.message, session, result.item, user, locale)


@router.callback_query(UseTemplate.assignee, F.data.startswith("tt:to:"))
async def template_assignee(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    user: User, grants: dict[str, Grant], locale: str,
) -> None:
    data = await state.get_data()
    template = await session.get(TaskTemplate, data.get("template_id", 0))
    assignee = await session.get(User, callback_int(call.data) or 0)
    if template is None or assignee is None:
        await state.clear()
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return

    result = await templates.apply(
        session, template=template, actor=user, grants=grants, assignee=assignee
    )
    await state.clear()
    if not result.ok:
        await call.answer(result.reason or t("template.failed", locale), show_alert=True)
        return
    await call.answer(t(
        "template.duplicate_done" if result.duplicate else "common.done", locale
    ))
    if not result.duplicate:
        await _announce_template(call.message, session, result.item, user, locale)


@router.callback_query(F.data.startswith("tt:drop:"))
async def template_drop(
    call: CallbackQuery, session: AsyncSession, user: User,
    features: dict[str, bool], locale: str,
) -> None:
    if not feature_service.is_on(features, "templates"):
        await call.answer(t("feature.off", locale), show_alert=True)
        return
    template = await session.get(TaskTemplate, callback_int(call.data) or 0)
    if template is None or template.organization_id != user.organization_id:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    title = template.title
    problem = await templates.remove(session, template=template, actor=user)
    if problem:
        await call.answer(problem, show_alert=True)
        return
    await call.answer(t("common.deleted", locale))
    await call.message.answer(t("template.deleted", locale, title=esc(cut(title, 60))))


async def _announce_template(
    message: Message, session: AsyncSession, task: Task, user: User, locale: str,
) -> None:
    """Сообщение об успехе. Поручение из шаблона — обычное поручение."""
    assignee = await session.get(User, task.assignee_id)
    when = (
        fmt_dt(task.due_at, user.timezone) if task.due_at
        else t("task.field.no_due", locale)
    )
    await message.answer(
        f"{t('task.new.created_title', locale)}\n\n"
        f"{esc(cut(task.title, 80))}\n"
        f"👤 {esc(assignee.full_name) if assignee else '—'}\n"
        f"🕐 {t('task.field.due', locale)}: {when}",
        reply_markup=_task_kb_minimal(task.id, locale),
    )
