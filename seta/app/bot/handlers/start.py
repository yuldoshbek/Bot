"""Вход и регистрация.

Сотрудник ничего не заполняет вручную дважды: имя, отдел, роль, телефон -
и он в системе. Роль по ссылке отдела применяется сразу, выбранная
самостоятельно уходит администратору на подтверждение.
"""
from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.common import (
    approval_kb,
    department_choice_kb,
    main_menu,
    request_contact_kb,
    role_choice_kb,
)
from app.models.enums import RoleCode, UserStatus
from app.models.org import Organization
from app.models.rbac import Role, UserRole
from app.models.user import User
from app.services.availability import get_view
from app.services.rbac import ELEVATED_ROLES, ROLE_TITLES, user_role_codes
from app.services.registration import (
    RegistrationError,
    has_any_admin,
    list_departments,
    resolve_invite,
    set_phone,
    start_registration,
)

router = Router(name="start")


class Reg(StatesGroup):
    name = State()
    department = State()
    role = State()
    contact = State()


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(
        f"Ваш Telegram ID: <code>{message.from_user.id}</code>\n\n"
        "Он нужен, чтобы назначить первого администратора системы."
    )


@router.message(CommandStart(deep_link=True))
@router.message(CommandStart())
async def cmd_start(
    message: Message,
    command: CommandObject | None,
    state: FSMContext,
    session: AsyncSession,
    organization: Organization,
    user: User | None,
    roles: set[RoleCode],
) -> None:
    if user is not None and user.status == UserStatus.ACTIVE:
        await state.clear()
        await message.answer(await greeting(session, user, roles), reply_markup=main_menu(roles))
        return

    if user is not None and user.status == UserStatus.PENDING:
        await message.answer(
            "Ваша заявка уже отправлена администратору.\n"
            "Как только её подтвердят, бот пришлёт уведомление."
        )
        return

    if user is not None and user.status in (UserStatus.REJECTED, UserStatus.SUSPENDED):
        await message.answer("Доступ к системе закрыт. Обратитесь к администратору.")
        return

    await state.clear()

    # Ссылка-приглашение: t.me/bot?start=inv_TOKEN
    invite = None
    payload = (command.args or "") if command else ""
    if payload.startswith("inv_"):
        invite = await resolve_invite(session, payload[4:])
        if invite is None:
            await message.answer(
                "Ссылка-приглашение недействительна или уже использована.\n"
                "Попросите администратора прислать новую."
            )
            return

    await state.update_data(
        invite_id=invite.id if invite else None,
        invite_role=invite.role if invite else None,
        invite_department_id=invite.department_id if invite else None,
    )

    hello = "Здравствуйте! Это корпоративный помощник по встречам и поручениям."
    if invite is not None and invite.label:
        hello += f"\nПриглашение: <b>{invite.label}</b>"

    await message.answer(f"{hello}\n\nКак вас зовут? Напишите фамилию и имя.", reply_markup=ReplyKeyboardRemove())
    await state.set_state(Reg.name)


@router.message(Reg.name, F.text)
async def reg_name(
    message: Message, state: FSMContext, session: AsyncSession, organization: Organization
) -> None:
    full_name = message.text.strip()
    if len(full_name) < 3:
        await message.answer("Слишком короткое имя. Напишите фамилию и имя целиком.")
        return

    await state.update_data(full_name=full_name)
    data = await state.get_data()

    if data.get("invite_department_id"):
        await ask_role_or_contact(message, state)
        return

    departments = await list_departments(session, organization.id)
    if not departments:
        await state.update_data(department_id=None)
        await ask_role_or_contact(message, state)
        return

    await message.answer(
        "В каком подразделении вы работаете?",
        reply_markup=department_choice_kb([(d.id, d.name) for d in departments]),
    )
    await state.set_state(Reg.department)


@router.callback_query(Reg.department, F.data.startswith("reg:dept:"))
async def reg_department(call: CallbackQuery, state: FSMContext) -> None:
    dept_id = int(call.data.rsplit(":", 1)[1])
    await state.update_data(department_id=dept_id or None)
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer()
    await ask_role_or_contact(call.message, state)


async def ask_role_or_contact(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if data.get("invite_role"):
        await ask_contact(message, state)
        return
    await message.answer("Какая у вас роль в системе?", reply_markup=role_choice_kb())
    await state.set_state(Reg.role)


@router.callback_query(Reg.role, F.data.startswith("reg:role:"))
async def reg_role(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, organization: Organization
) -> None:
    role = RoleCode(call.data.rsplit(":", 1)[1])
    await state.update_data(requested_role=role)
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer()

    # Про подтверждение говорим, только если подтверждать действительно есть кому:
    # в пустой системе первый вошедший получает доступ сразу.
    if role in ELEVATED_ROLES and await has_any_admin(session, organization.id):
        await call.message.answer(
            f"Роль «{ROLE_TITLES[role]}» подтверждает администратор — это займёт немного времени."
        )
    await ask_contact(call.message, state)


async def ask_contact(message: Message, state: FSMContext) -> None:
    await message.answer(
        "Последний шаг — подтвердите номер телефона кнопкой ниже.",
        reply_markup=request_contact_kb(),
    )
    await state.set_state(Reg.contact)


@router.message(Reg.contact, F.contact)
async def reg_contact(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    organization: Organization,
) -> None:
    if message.contact.user_id != message.from_user.id:
        await message.answer("Пожалуйста, отправьте свой собственный номер — кнопкой ниже.")
        return

    data = await state.get_data()
    invite = None
    if data.get("invite_id"):
        from app.models.user import Invite  # локальный импорт: нужен только здесь

        invite = (
            await session.execute(select(Invite).where(Invite.id == data["invite_id"]))
        ).scalar_one_or_none()

    requested_role = RoleCode(data.get("requested_role") or RoleCode.EMPLOYEE)

    try:
        user = await start_registration(
            session,
            organization=organization,
            telegram_user_id=message.from_user.id,
            telegram_username=message.from_user.username,
            full_name=data["full_name"],
            department_id=data.get("department_id"),
            requested_role=requested_role,
            invite=invite,
        )
    except RegistrationError as error:
        await state.clear()
        await message.answer(str(error), reply_markup=ReplyKeyboardRemove())
        return

    await set_phone(session, user=user, phone=message.contact.phone_number)
    await state.clear()

    if user.status == UserStatus.ACTIVE:
        roles = await user_role_codes(session, user)
        text = f"Готово, {user.full_name}. Вы в системе."
        if RoleCode.ADMIN in roles:
            text += (
                "\n\nВы первый в системе, поэтому вам выдана роль "
                "<b>администратора</b> — подтверждать заявки было бы некому.\n\n"
                "С чего начать:\n"
                "1. «🛠 Администрирование» → «Отделы» — заведите подразделения\n"
                "2. «Ссылки-приглашения» — отправьте ссылку в чат отдела\n"
                "3. Руководителю дайте зарегистрироваться и подтвердите его заявку"
            )
        await message.answer(text, reply_markup=main_menu(roles))
        return

    await message.answer(
        "Заявка отправлена администратору.\n"
        "Как только её подтвердят, бот пришлёт уведомление — повторно писать не нужно.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await notify_admins(session, user)


@router.message(Reg.contact)
async def reg_contact_fallback(message: Message) -> None:
    await message.answer("Нажмите кнопку «📱 Подтвердить номер» — вводить номер вручную не нужно.")


async def notify_admins(session: AsyncSession, applicant: User) -> None:
    """Одна карточка администратору: принять, изменить роль или отклонить."""
    from app.bot.loader import bot

    rows = await session.execute(
        select(User)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(Role.code == RoleCode.ADMIN, User.status == UserStatus.ACTIVE)
    )
    admins = list(rows.scalars().unique().all())

    role_title = ROLE_TITLES.get(RoleCode(applicant.requested_role), applicant.requested_role)
    department = "не указано"
    if applicant.department_id:
        from app.models.org import Department

        dept = (
            await session.execute(select(Department).where(Department.id == applicant.department_id))
        ).scalar_one_or_none()
        if dept:
            department = dept.name

    text = (
        "📥 <b>Новая заявка на регистрацию</b>\n\n"
        f"👤 {applicant.full_name}\n"
        f"🏢 {department}\n"
        f"🔑 Запрошенная роль: <b>{role_title}</b>\n"
        f"📱 {applicant.phone or 'номер не подтверждён'}"
    )
    for admin in admins:
        try:
            await bot.send_message(
                admin.telegram_user_id, text, reply_markup=approval_kb(applicant.id)
            )
        except Exception:  # администратор мог заблокировать бота
            continue


async def greeting(session: AsyncSession, user: User, roles: set[RoleCode]) -> str:
    titles = ", ".join(ROLE_TITLES[r] for r in sorted(roles, key=lambda r: r.value)) or "Сотрудник"
    lines = [f"С возвращением, <b>{user.full_name}</b>.", f"Роль: {titles}"]

    if RoleCode.EXECUTIVE in roles or RoleCode.ASSISTANT in roles:
        view = await get_view(session, user.id)
        lines.append(f"Доступность: {view.render(user.timezone)}")

    return "\n".join(lines)
