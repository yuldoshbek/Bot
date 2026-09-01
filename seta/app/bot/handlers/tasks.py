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

from app.bot.keyboards.common import BTN_CONTROL, BTN_MY_TASKS, BTN_NEW_TASK, main_menu
from app.bot.utils import STALE_BUTTON, callback_int
from app.core.dates import humanize_due, parse_due
from app.core.text import cut, esc
from app.core.timeutil import fmt_dt
from app.models.enums import Priority, RoleCode, TaskStatus, UserStatus
from app.models.org import Organization
from app.models.task import Task, TaskComment, TaskEvent
from app.models.user import User
from app.services import tasks as service
from app.services.rbac import Grant, has_permission
from app.services.tasks import PRIORITY_LABELS, STATUS_LABELS, TaskError

router = Router(name="tasks")

BUCKETS = {
    "active": "Активные",
    "today": "На сегодня",
    "overdue": "Просроченные",
    "review": "На проверке",
    "created": "Мои поручения другим",
    "done": "Выполненные",
}


class NewTask(StatesGroup):
    assignee = State()
    title = State()
    due = State()
    priority = State()


class TaskInput(StatesGroup):
    comment = State()
    rework = State()
    extension_date = State()
    extension_reason = State()


# ─────────────────────────  СОЗДАНИЕ  ─────────────────────────
@router.message(F.text == BTN_NEW_TASK)
async def new_task(
    message: Message, state: FSMContext, session: AsyncSession,
    organization: Organization, user: User, grants: dict[str, Grant],
) -> None:
    if not has_permission(grants, "task.create"):
        await message.answer("Создавать поручения может руководитель, ассистент или начальник отдела.")
        return

    people = await _colleagues(session, organization.id, exclude_id=user.id)
    if not people:
        await message.answer(
            "В системе пока нет других сотрудников.\n"
            "Заведите отделы и разошлите ссылки-приглашения в разделе «Администрирование»."
        )
        return

    await state.clear()
    await message.answer(
        "<b>Новое поручение</b>\n\nКому поручаем?",
        reply_markup=_people_kb(people),
    )
    await state.set_state(NewTask.assignee)


@router.message(NewTask.assignee, F.text)
async def new_task_search(message: Message, session: AsyncSession, organization: Organization) -> None:
    found = await service.find_assignee(session, organization.id, message.text)
    if not found:
        await message.answer("Никого не нашёл. Напишите часть фамилии или выберите из списка.")
        return
    await message.answer("Кого имели в виду?", reply_markup=_people_kb(found))


@router.callback_query(NewTask.assignee, F.data.startswith("nt:who:"))
async def new_task_assignee(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    assignee_id = callback_int(call.data)
    assignee = await session.get(User, assignee_id) if assignee_id else None
    if assignee is None:
        await call.answer("Сотрудник не найден.", show_alert=True)
        return

    await state.update_data(assignee_id=assignee.id, assignee_name=assignee.full_name)
    await call.answer()
    await call.message.edit_text(f"<b>Новое поручение</b>\n\n👤 Кому: {assignee.full_name}")
    await call.message.answer("Что нужно сделать? Напишите одной строкой.")
    await state.set_state(NewTask.title)


@router.message(NewTask.title, F.text)
async def new_task_title(message: Message, state: FSMContext) -> None:
    title = message.text.strip()
    if len(title) < 3:
        await message.answer("Слишком коротко. Опишите задачу понятнее.")
        return

    await state.update_data(title=title)
    await message.answer(
        "К какому сроку?\n\n"
        "Можно написать словами: <i>завтра</i>, <i>до пятницы</i>, <i>через 3 дня</i>, "
        "<i>05.09</i>, <i>5 сентября 15:00</i>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Без срока", callback_data="nt:due:none")]]
        ),
    )
    await state.set_state(NewTask.due)


@router.message(NewTask.due, F.text)
async def new_task_due(message: Message, state: FSMContext, user: User) -> None:
    due_at = parse_due(message.text, user.timezone)
    if due_at is None:
        await message.answer(
            "Не понял срок. Напишите, например: <i>завтра</i>, <i>до пятницы</i>, <i>05.09</i>."
        )
        return

    await state.update_data(due_at=due_at.isoformat())
    await message.answer(
        f"⏰ Срок: <b>{humanize_due(due_at, user.timezone)}</b>\n\nКакой приоритет?",
        reply_markup=_priority_kb(),
    )
    await state.set_state(NewTask.priority)


@router.callback_query(NewTask.due, F.data == "nt:due:none")
async def new_task_no_due(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(due_at=None)
    await call.answer()
    await call.message.edit_text("⏰ Срок: не задан")
    await call.message.answer("Какой приоритет?", reply_markup=_priority_kb())
    await state.set_state(NewTask.priority)


@router.callback_query(NewTask.priority, F.data.startswith("nt:prio:"))
async def new_task_priority(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    user: User, roles: set[RoleCode],
) -> None:
    from datetime import datetime

    priority = Priority(call.data.rsplit(":", 1)[1])
    data = await state.get_data()
    assignee = await session.get(User, data["assignee_id"])
    if assignee is None:
        await call.answer("Исполнитель не найден.", show_alert=True)
        await state.clear()
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
    await call.answer("Поручение создано")

    review_line = (
        "\n🔎 Требует проверки после выполнения" if task.requires_review else ""
    )
    await call.message.edit_text(
        f"✅ <b>Поручение создано</b>\n\n"
        f"📋 {esc(cut(task.title, 200))}\n"
        f"👤 {esc(assignee.full_name)}\n"
        f"⏰ {humanize_due(task.due_at, user.timezone) if task.due_at else 'без срока'}\n"
        f"🔺 {PRIORITY_LABELS[priority]}{review_line}\n\n"
        f"Исполнителю отправлено уведомление.",
        reply_markup=_task_kb_minimal(task.id),
    )


# ─────────────────────────  СПИСКИ  ─────────────────────────
@router.message(F.text == BTN_MY_TASKS)
async def my_tasks(message: Message, session: AsyncSession, user: User) -> None:
    await _show_bucket(message, session, user, "active")


@router.callback_query(F.data.startswith("tl:"))
async def switch_bucket(call: CallbackQuery, session: AsyncSession, user: User) -> None:
    bucket = call.data.rsplit(":", 1)[1]
    await call.answer()
    await _show_bucket(call.message, session, user, bucket, edit=True)


async def _show_bucket(
    message: Message, session: AsyncSession, user: User, bucket: str, edit: bool = False
) -> None:
    items = await service.my_tasks(session, user, bucket=bucket)
    title = BUCKETS.get(bucket, "Поручения")

    if not items:
        text = f"<b>{title}</b>\n\nПусто."
    else:
        lines = [f"<b>{title}: {len(items)}</b>", ""]
        for task in items:
            due = f" · {humanize_due(task.due_at, user.timezone)}" if task.due_at else ""
            lines.append(f"{STATUS_LABELS[TaskStatus(task.status)]}{due}\n📋 {esc(cut(task.title, 120))}")
        text = "\n\n".join(lines)

    keyboard = _buckets_kb(bucket, items)
    if edit:
        await message.edit_text(text, reply_markup=keyboard)
    else:
        await message.answer(text, reply_markup=keyboard)


@router.message(F.text == BTN_CONTROL)
async def control(
    message: Message, session: AsyncSession, user: User, grants: dict[str, Grant]
) -> None:
    if not has_permission(grants, "task.read"):
        await message.answer("Раздел недоступен.")
        return

    scope_all = grants["task.read"].scope == "ORGANIZATION"
    counters = await service.control_counters(session, user, scope_all)

    await message.answer(
        "<b>📊 Контроль исполнения</b>\n\n"
        f"В работе: <b>{counters['active']}</b>\n"
        f"🔴 Просрочено: <b>{counters['overdue']}</b>\n"
        f"🟠 На проверке: <b>{counters['review']}</b>\n"
        f"🔴 Критичных: <b>{counters['critical']}</b>\n"
        f"🟢 Выполнено: <b>{counters['done']}</b>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🔴 Просроченные", callback_data="tl:overdue"),
                    InlineKeyboardButton(text="🟠 На проверке", callback_data="tl:review"),
                ],
                [InlineKeyboardButton(text="Мои поручения другим", callback_data="tl:created")],
            ]
        ),
    )


# ─────────────────────────  КАРТОЧКА  ─────────────────────────
@router.callback_query(F.data.startswith("t:open:"))
async def open_task(
    call: CallbackQuery, session: AsyncSession, user: User, grants: dict[str, Grant]
) -> None:
    task_id = callback_int(call.data)
    task = await session.get(Task, task_id) if task_id else None
    if task is None:
        await call.answer("Поручение не найдено.", show_alert=True)
        return

    access = await service.access_for(session, task, user, grants)
    if not access.can_view:
        await call.answer("Это поручение вам недоступно.", show_alert=True)
        return

    await call.answer()
    await call.message.answer(
        await _render_task(session, task, user), reply_markup=_task_kb(task, access)
    )


@router.callback_query(F.data.startswith("t:"))
async def task_action(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    user: User, grants: dict[str, Grant],
) -> None:
    parts = call.data.split(":")
    if len(parts) < 3:
        await call.answer(STALE_BUTTON, show_alert=True)
        return

    action = parts[1]
    if action == "open":
        return

    task_id = callback_int(call.data, 2)
    if task_id is None:
        await call.answer(STALE_BUTTON, show_alert=True)
        return

    task = await session.get(Task, task_id)
    if task is None:
        await call.answer("Поручение не найдено.", show_alert=True)
        return

    access = await service.access_for(session, task, user, grants)
    if not access.can_view:
        await call.answer("Это поручение вам недоступно.", show_alert=True)
        return

    try:
        if action == "accept" and access.can_accept:
            await service.accept(session, task, user)
            await call.answer("Принято в работу")
        elif action == "start" and access.can_start:
            await service.start(session, task, user)
            await call.answer("Отмечено: в работе")
        elif action == "submit" and access.can_submit:
            result = await service.submit(session, task, user)
            await call.answer(
                "Отправлено на проверку" if result == TaskStatus.REVIEW else "Поручение закрыто"
            )
        elif action == "approve" and access.can_review:
            await service.approve(session, task, user)
            await call.answer("Работа принята")
        elif action == "reject" and access.can_review:
            await state.update_data(task_id=task.id)
            await state.set_state(TaskInput.rework)
            await call.answer()
            await call.message.answer("Что именно нужно доработать? Напишите комментарий.")
            return
        elif action == "cancel" and access.can_cancel:
            await service.cancel(session, task, user)
            await call.answer("Поручение отменено")
        elif action == "comment":
            await state.update_data(task_id=task.id)
            await state.set_state(TaskInput.comment)
            await call.answer()
            await call.message.answer("Напишите комментарий или пришлите файл.")
            return
        elif action == "ext" and access.can_request_extension:
            await state.update_data(task_id=task.id)
            await state.set_state(TaskInput.extension_date)
            await call.answer()
            await call.message.answer(
                "На какой срок перенести?\nНапример: <i>до пятницы</i>, <i>+3 дня</i>, <i>10.09</i>"
            )
            return
        elif action in ("extok", "extno") and access.can_decide_extension:
            extension = await service.pending_extension(session, task.id)
            if extension is None:
                await call.answer("Запрос уже рассмотрен.", show_alert=True)
                return
            await service.decide_extension(
                session, extension, task, user, approved=(action == "extok")
            )
            await call.answer("Срок продлён" if action == "extok" else "Отклонено")
        else:
            await call.answer("Это действие сейчас недоступно.", show_alert=True)
            return
    except TaskError as error:
        await call.answer(str(error), show_alert=True)
        return

    access = await service.access_for(session, task, user, grants)
    await call.message.edit_text(
        await _render_task(session, task, user), reply_markup=_task_kb(task, access)
    )


# ─────────────────────────  ВВОД ТЕКСТА  ─────────────────────────
@router.message(TaskInput.rework, F.text)
async def input_rework(
    message: Message, state: FSMContext, session: AsyncSession,
    user: User, grants: dict[str, Grant],
) -> None:
    data = await state.get_data()
    task = await session.get(Task, data["task_id"])
    await state.clear()
    if task is None:
        return

    try:
        await service.return_for_rework(session, task, user, message.text)
    except TaskError as error:
        await message.answer(str(error))
        return

    access = await service.access_for(session, task, user, grants)
    await message.answer("🟠 Возвращено на доработку. Исполнитель уведомлён.")
    await message.answer(await _render_task(session, task, user), reply_markup=_task_kb(task, access))


@router.message(TaskInput.comment)
async def input_comment(
    message: Message, state: FSMContext, session: AsyncSession, user: User
) -> None:
    data = await state.get_data()
    task = await session.get(Task, data["task_id"])
    await state.clear()
    if task is None:
        return

    file_id = file_name = None
    if message.document:
        file_id, file_name = message.document.file_id, message.document.file_name
    elif message.photo:
        file_id, file_name = message.photo[-1].file_id, "фото"

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

    await message.answer("💬 Комментарий добавлен.")


@router.message(TaskInput.extension_date, F.text)
async def input_extension_date(message: Message, state: FSMContext, user: User) -> None:
    new_due = parse_due(message.text, user.timezone)
    if new_due is None:
        await message.answer("Не понял дату. Например: <i>до пятницы</i> или <i>10.09</i>.")
        return

    await state.update_data(new_due=new_due.isoformat())
    await state.set_state(TaskInput.extension_reason)
    await message.answer(
        f"Новый срок: <b>{humanize_due(new_due, user.timezone)}</b>\n\n"
        "Почему нужен перенос? Причина уйдёт автору поручения."
    )


@router.message(TaskInput.extension_reason, F.text)
async def input_extension_reason(
    message: Message, state: FSMContext, session: AsyncSession, user: User
) -> None:
    from datetime import datetime

    data = await state.get_data()
    task = await session.get(Task, data["task_id"])
    await state.clear()
    if task is None:
        return

    try:
        await service.request_extension(
            session, task, user, datetime.fromisoformat(data["new_due"]), message.text
        )
    except TaskError as error:
        await message.answer(str(error))
        return

    await message.answer("⏰ Запрос отправлен автору поручения. Ответ придёт сюда же.")


# ─────────────────────────  ОФОРМЛЕНИЕ  ─────────────────────────
async def _render_task(session: AsyncSession, task: Task, viewer: User) -> str:
    creator = await session.get(User, task.creator_id)
    assignee = await session.get(User, task.assignee_id)

    author = esc(creator.full_name) if creator else "—"
    if task.on_behalf_of_id:
        principal = await session.get(User, task.on_behalf_of_id)
        if principal:
            author = f"{author} по поручению: {esc(principal.full_name)}"

    lines = [
        f"📋 <b>{esc(cut(task.title, 300))}</b>",
        "",
        f"Статус: {STATUS_LABELS[TaskStatus(task.status)]}",
        f"👤 Исполнитель: {esc(assignee.full_name) if assignee else '—'}",
        f"✍️ Автор: {author}",
    ]
    if task.due_at:
        lines.append(f"⏰ Срок: {humanize_due(task.due_at, viewer.timezone)}")
    lines.append(f"🔺 Приоритет: {PRIORITY_LABELS[Priority(task.priority)]}")
    if task.description:
        lines += ["", esc(cut(task.description, 800))]
    if task.requires_review:
        reviewer = await session.get(User, task.reviewer_id) if task.reviewer_id else None
        lines.append(f"🔎 Проверяет: {esc(reviewer.full_name) if reviewer else '—'}")
    if task.personal_control:
        lines.append("⭐ На личном контроле руководителя")
    if task.rework_count:
        lines.append(f"↩️ Возвратов на доработку: {task.rework_count}")
    if task.extensions_count:
        lines.append(f"⏳ Срок продлевали: {task.extensions_count}")

    extension = await service.pending_extension(session, task.id)
    if extension is not None:
        lines += [
            "",
            f"⏰ <b>Просят перенести срок</b> на {humanize_due(extension.new_due_at, viewer.timezone)}",
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
        lines += ["", "<b>Последние комментарии</b>"]
        for comment in reversed(list(comments)):
            author_user = await session.get(User, comment.author_id)
            name = esc(author_user.full_name) if author_user else "—"
            body = esc(cut(comment.text or f"📎 {comment.file_name or 'файл'}", 200))
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
        lines.append(f"\n🕐 Последнее изменение: {fmt_dt(last.created_at, viewer.timezone)}")

    return "\n".join(lines)


def _task_kb(task: Task, access: service.TaskAccess) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    tid = task.id

    if access.can_accept:
        rows.append([InlineKeyboardButton(text="✅ Принять", callback_data=f"t:accept:{tid}")])
    if access.can_start:
        rows.append([InlineKeyboardButton(text="▶️ Взять в работу", callback_data=f"t:start:{tid}")])
    if access.can_submit:
        rows.append([InlineKeyboardButton(text="📤 Выполнено", callback_data=f"t:submit:{tid}")])
    if access.can_review:
        rows.append([
            InlineKeyboardButton(text="✅ Принять работу", callback_data=f"t:approve:{tid}"),
            InlineKeyboardButton(text="↩️ На доработку", callback_data=f"t:reject:{tid}"),
        ])
    if access.can_decide_extension:
        rows.append([
            InlineKeyboardButton(text="⏰ Продлить", callback_data=f"t:extok:{tid}"),
            InlineKeyboardButton(text="✖️ Отказать", callback_data=f"t:extno:{tid}"),
        ])
    if access.can_request_extension:
        rows.append([InlineKeyboardButton(text="⏰ Продлить срок", callback_data=f"t:ext:{tid}")])

    bottom = [InlineKeyboardButton(text="💬 Комментарий", callback_data=f"t:comment:{tid}")]
    if access.can_cancel:
        bottom.append(InlineKeyboardButton(text="⚫ Отменить", callback_data=f"t:cancel:{tid}"))
    rows.append(bottom)

    return InlineKeyboardMarkup(inline_keyboard=rows)


def _task_kb_minimal(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Открыть", callback_data=f"t:open:{task_id}")]]
    )


def _buckets_kb(current: str, items: list[Task]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for task in items[:8]:
        title = task.title if len(task.title) <= 32 else task.title[:31] + "…"
        rows.append([InlineKeyboardButton(text=f"📋 {title}", callback_data=f"t:open:{task.id}")])

    filters = [code for code in BUCKETS if code != current]
    for index in range(0, len(filters), 2):
        rows.append([
            InlineKeyboardButton(text=BUCKETS[code], callback_data=f"tl:{code}")
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


def _priority_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Обычный", callback_data="nt:prio:NORMAL"),
                InlineKeyboardButton(text="🔴 Высокий", callback_data="nt:prio:HIGH"),
            ],
            [InlineKeyboardButton(text="🔴 Критичный", callback_data="nt:prio:CRITICAL")],
        ]
    )


async def _colleagues(
    session: AsyncSession, organization_id: int, exclude_id: int
) -> list[User]:
    rows = await session.execute(
        select(User)
        .where(
            User.organization_id == organization_id,
            User.status == UserStatus.ACTIVE,
            User.id != exclude_id,
        )
        .order_by(User.full_name)
        .limit(10)
    )
    return list(rows.scalars().all())


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
