"""Вход и регистрация.

Сотрудник ничего не заполняет вручную дважды: имя, отдел, роль, телефон -
и он в системе. Роль по ссылке отдела применяется сразу, выбранная
самостоятельно уходит администратору на подтверждение.
"""
from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.utils import callback_int
from app.bot.keyboards.common import (
    approval_kb,
    department_choice_kb,
    main_menu,
    request_contact_kb,
    role_choice_kb,
)
from app.core.i18n import t
from app.core.text import esc
from app.models.enums import RoleCode, UserStatus
from app.models.org import Organization
from app.models.rbac import Role, UserRole
from app.models.user import User
from app.services.availability import get_view
from app.services.rbac import ELEVATED_ROLES, role_title, role_titles, user_role_codes
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
async def cmd_id(message: Message, locale: str) -> None:
    await message.answer(
        t("start.your_id", locale, id=message.from_user.id)
        + "\n\n"
        + t("start.id_hint", locale)
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
    features: dict[str, bool],
    locale: str,
) -> None:
    if user is not None and user.status == UserStatus.ACTIVE:
        await state.clear()
        await message.answer(
            await greeting(session, user, roles, locale),
            reply_markup=main_menu(roles, features, locale),
        )
        return

    if user is not None and user.status == UserStatus.PENDING:
        await message.answer(t("start.already_pending", locale))
        return

    if user is not None and user.status in (UserStatus.REJECTED, UserStatus.SUSPENDED):
        await message.answer(t("start.closed", locale))
        return

    await state.clear()

    # Ссылка-приглашение: t.me/bot?start=inv_TOKEN
    invite = None
    payload = (command.args or "") if command else ""
    if payload.startswith("inv_"):
        invite = await resolve_invite(session, payload[4:])
        if invite is None:
            await message.answer(t("start.invite_bad", locale))
            return

    await state.update_data(
        invite_id=invite.id if invite else None,
        invite_role=invite.role if invite else None,
        invite_department_id=invite.department_id if invite else None,
    )

    hello = t("start.hello", locale)
    if invite is not None and invite.label:
        hello += "\n" + t("start.invite_label", locale, label=esc(invite.label))

    await message.answer(
        f"{hello}\n\n" + t("start.ask_name", locale),
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(Reg.name)


@router.message(Reg.name, F.text)
async def reg_name(
    message: Message, state: FSMContext, session: AsyncSession,
    organization: Organization, locale: str,
) -> None:
    full_name = message.text.strip()
    if len(full_name) < 3:
        await message.answer(t("start.name_too_short", locale))
        return

    await state.update_data(full_name=full_name)
    data = await state.get_data()

    if data.get("invite_department_id"):
        await ask_role_or_contact(message, state, locale)
        return

    departments = await list_departments(session, organization.id)
    if not departments:
        await state.update_data(department_id=None)
        await ask_role_or_contact(message, state, locale)
        return

    await message.answer(
        t("start.ask_department", locale),
        reply_markup=department_choice_kb([(d.id, d.name) for d in departments], locale),
    )
    await state.set_state(Reg.department)


@router.callback_query(Reg.department, F.data.startswith("reg:dept:"))
async def reg_department(call: CallbackQuery, state: FSMContext, locale: str) -> None:
    dept_id = callback_int(call.data)
    if dept_id is None:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    await state.update_data(department_id=dept_id or None)
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer()
    await ask_role_or_contact(call.message, state, locale)


async def ask_role_or_contact(message: Message, state: FSMContext, locale: str) -> None:
    data = await state.get_data()
    if data.get("invite_role"):
        await ask_contact(message, state, locale)
        return
    await message.answer(t("start.ask_role", locale), reply_markup=role_choice_kb(locale))
    await state.set_state(Reg.role)


@router.callback_query(Reg.role, F.data.startswith("reg:role:"))
async def reg_role(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    organization: Organization, locale: str,
) -> None:
    try:
        role = RoleCode(call.data.rsplit(":", 1)[1])
    except ValueError:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    await state.update_data(requested_role=role)
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer()

    # Про подтверждение говорим, только если подтверждать действительно есть кому:
    # в пустой системе первый вошедший получает доступ сразу.
    if role in ELEVATED_ROLES and await has_any_admin(session, organization.id):
        await call.message.answer(
            t("start.role_needs_approval", locale, role=role_title(role, locale))
        )
    await ask_contact(call.message, state, locale)


async def ask_contact(message: Message, state: FSMContext, locale: str) -> None:
    await message.answer(
        t("start.ask_contact", locale),
        reply_markup=request_contact_kb(locale),
    )
    await state.set_state(Reg.contact)


@router.message(Reg.contact, F.contact)
async def reg_contact(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    organization: Organization,
    features: dict[str, bool],
    locale: str,
    bot: Bot,
) -> None:
    if message.contact.user_id != message.from_user.id:
        await message.answer(t("start.own_number", locale))
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
        text = t("start.done", locale, name=esc(user.full_name))
        if RoleCode.ADMIN in roles:
            text += "\n\n" + t("start.first_admin", locale)
        await message.answer(text, reply_markup=main_menu(roles, features, locale))
        return

    await message.answer(
        t("start.request_sent", locale),
        reply_markup=ReplyKeyboardRemove(),
    )
    await notify_admins(bot, session, user)


@router.message(Reg.contact)
async def reg_contact_fallback(message: Message, locale: str) -> None:
    await message.answer(t("start.press_contact", locale))


async def notify_admins(bot: Bot, session: AsyncSession, applicant: User) -> None:
    """Одна карточка администратору: принять, изменить роль или отклонить.

    Бот приходит из диспетчера, а не берётся из app.bot.loader. Глобальный
    экземпляр держит боевой токен, и проверочный прогон с подменённой сетью
    всё равно отправлял бы настоящие сообщения живым людям - однажды так
    и случилось.
    """
    rows = await session.execute(
        select(User)
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(
            Role.code == RoleCode.ADMIN,
            User.status == UserStatus.ACTIVE,
            # Заявка уходит администраторам своей организации, а не всем подряд.
            User.organization_id == applicant.organization_id,
        )
    )
    admins = list(rows.scalars().unique().all())

    department = None
    if applicant.department_id:
        from app.models.org import Department

        dept = (
            await session.execute(select(Department).where(Department.id == applicant.department_id))
        ).scalar_one_or_none()
        if dept:
            department = dept.name

    # Текст собирается внутри цикла, а не один раз до него: у каждого
    # администратора свой язык. Одно сообщение на всех показало бы половине
    # из них чужой язык — и это единственное сообщение, которое они видят
    # до того, как вообще войдут в систему.
    for admin in admins:
        text = "\n\n".join([
            t("start.new_application", admin.locale),
            "\n".join([
                f"👤 {esc(applicant.full_name)}",
                f"🏢 {esc(department) if department else t('profile.department_none', admin.locale)}",
                "🔑 " + t("start.requested_role", admin.locale,
                          role=role_title(applicant.requested_role, admin.locale)),
                f"📱 {esc(applicant.phone) or t('profile.phone_none', admin.locale)}",
            ]),
        ])
        try:
            await bot.send_message(
                admin.telegram_user_id, text,
                reply_markup=approval_kb(applicant.id, admin.locale),
            )
        except Exception:  # администратор мог заблокировать бота
            continue


async def greeting(
    session: AsyncSession, user: User, roles: set[RoleCode], locale: str | None = None
) -> str:
    lines = [
        t("start.welcome_back", locale, name=esc(user.full_name)),
        t("start.role_line", locale, roles=role_titles(roles, locale)),
    ]

    if RoleCode.EXECUTIVE in roles or RoleCode.ASSISTANT in roles:
        view = await get_view(session, user.id)
        lines.append(t("start.availability_line", locale, state=view.render(user.timezone)))

    return "\n".join(lines)
