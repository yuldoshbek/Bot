"""Администрирование внутри Telegram.

Администратор ничего не заводит вручную: он подтверждает заявки одним касанием
и рассылает ссылки отделов. Содержание встреч и поручений ему не показывается -
роль управляет системой, а не читает переписку руководства.
"""
from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.common import BTN_ADMIN, admin_menu_kb, approval_kb, approval_role_kb
from app.bot.utils import STALE_BUTTON, callback_int
from app.core.text import esc
from app.core.timeutil import fmt_dt
from app.models.audit import AuditLog
from app.models.enums import RoleCode, UserStatus
from app.models.org import Department, Organization
from app.models.rbac import Role, UserRole
from app.models.user import Invite, User
from app.services.audit import write_audit
from app.services.rbac import ROLE_TITLES, Grant, has_permission
from app.services.registration import (
    approve_user,
    create_invite,
    invite_link,
    list_departments,
    pending_users,
    reject_user,
)

router = Router(name="admin")


class AdminForms(StatesGroup):
    department_name = State()
    invite_label = State()


def _require(grants: dict[str, Grant], permission: str) -> bool:
    return has_permission(grants, permission)


async def _applicant_of(
    session: AsyncSession, data: str | None, organization: Organization
) -> User | None:
    """Сотрудник из данных кнопки — только из своей организации.

    Идентификатор приходит от клиента: без сверки организации подставленный
    номер позволил бы администратору действовать в чужой организации.
    """
    applicant_id = callback_int(data)
    if applicant_id is None:
        return None
    applicant = await session.get(User, applicant_id)
    if applicant is None or applicant.organization_id != organization.id:
        return None
    return applicant


@router.message(F.text == BTN_ADMIN)
async def admin_menu(message: Message, session: AsyncSession, organization: Organization, grants: dict[str, Grant]) -> None:
    if not _require(grants, "admin.users"):
        await message.answer("Раздел доступен администратору.")
        return

    pending = await session.scalar(
        select(func.count(User.id)).where(
            User.organization_id == organization.id, User.status == UserStatus.PENDING
        )
    )
    active = await session.scalar(
        select(func.count(User.id)).where(
            User.organization_id == organization.id, User.status == UserStatus.ACTIVE
        )
    )
    departments = await session.scalar(
        select(func.count(Department.id)).where(Department.organization_id == organization.id)
    )

    await message.answer(
        "<b>Администрирование</b>\n\n"
        f"Сотрудников в системе: <b>{active}</b>\n"
        f"Заявок на подтверждение: <b>{pending}</b>\n"
        f"Отделов: <b>{departments}</b>",
        reply_markup=admin_menu_kb(),
    )


# ── Заявки ──────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "adm:pending")
async def show_pending(call: CallbackQuery, session: AsyncSession, organization: Organization, grants: dict[str, Grant]) -> None:
    if not _require(grants, "admin.users"):
        await call.answer("Недостаточно прав.", show_alert=True)
        return

    applicants = await pending_users(session, organization.id)
    await call.answer()
    if not applicants:
        await call.message.answer("Заявок нет — все подтверждены.")
        return

    for applicant in applicants[:20]:
        await call.message.answer(await _user_card(session, applicant), reply_markup=approval_kb(applicant.id))


async def _user_card(session: AsyncSession, applicant: User) -> str:
    department = "не указано"
    if applicant.department_id:
        dept = await session.get(Department, applicant.department_id)
        if dept:
            department = dept.name
    role_code = applicant.requested_role or RoleCode.EMPLOYEE
    role_title = ROLE_TITLES.get(RoleCode(role_code), role_code)
    return (
        f"👤 <b>{esc(applicant.full_name)}</b>\n"
        f"🏢 {esc(department)}\n"
        f"🔑 Запрошенная роль: <b>{role_title}</b>\n"
        f"📱 {esc(applicant.phone) or 'номер не подтверждён'}\n"
        f"🕐 Заявка: {fmt_dt(applicant.created_at)}"
    )


@router.callback_query(F.data.startswith("adm:approve:"))
async def approve(
    call: CallbackQuery, session: AsyncSession, organization: Organization,
    user: User, grants: dict[str, Grant], bot: Bot,
) -> None:
    if not _require(grants, "admin.users"):
        await call.answer("Недостаточно прав.", show_alert=True)
        return

    applicant = await _applicant_of(session, call.data, organization)
    if applicant is None:
        await call.answer("Заявка не найдена.", show_alert=True)
        return
    if applicant.status != UserStatus.PENDING:
        await call.answer("Заявка уже рассмотрена.", show_alert=True)
        return

    role = RoleCode(applicant.requested_role or RoleCode.EMPLOYEE)
    await approve_user(session, user=applicant, role=role, approved_by=user.id)
    await call.answer("Принято")
    await call.message.edit_text(
        f"✅ <b>{esc(applicant.full_name)}</b> — доступ открыт\nРоль: {ROLE_TITLES[role]}"
    )
    await _notify_user(bot, applicant, f"Ваш доступ подтверждён. Роль: {ROLE_TITLES[role]}.\nНажмите /start.")


@router.callback_query(F.data.startswith("adm:reject:"))
async def reject(
    call: CallbackQuery, session: AsyncSession, organization: Organization,
    user: User, grants: dict[str, Grant], bot: Bot,
) -> None:
    if not _require(grants, "admin.users"):
        await call.answer("Недостаточно прав.", show_alert=True)
        return

    applicant = await _applicant_of(session, call.data, organization)
    if applicant is None:
        await call.answer("Заявка не найдена.", show_alert=True)
        return

    await reject_user(session, user=applicant, rejected_by=user.id)
    await call.answer("Отклонено")
    await call.message.edit_text(f"❌ <b>{esc(applicant.full_name)}</b> — заявка отклонена")
    await _notify_user(bot, applicant, "Заявка на доступ отклонена. Уточните детали у администратора.")


@router.callback_query(F.data.startswith("adm:role:"))
async def change_role(call: CallbackQuery, grants: dict[str, Grant]) -> None:
    if not _require(grants, "admin.roles"):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    applicant_id = callback_int(call.data)
    if applicant_id is None:
        await call.answer(STALE_BUTTON, show_alert=True)
        return
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=approval_role_kb(applicant_id))


@router.callback_query(F.data.startswith("adm:card:"))
async def back_to_card(call: CallbackQuery, session: AsyncSession) -> None:
    applicant_id = callback_int(call.data)
    if applicant_id is None:
        await call.answer(STALE_BUTTON, show_alert=True)
        return
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=approval_kb(applicant_id))


@router.callback_query(F.data.startswith("adm:setrole:"))
async def set_role(
    call: CallbackQuery, session: AsyncSession, organization: Organization,
    user: User, grants: dict[str, Grant], bot: Bot,
) -> None:
    if not _require(grants, "admin.roles"):
        await call.answer("Недостаточно прав.", show_alert=True)
        return

    parts = call.data.split(":", 3)
    applicant_id = callback_int(call.data, 2)
    if len(parts) < 4 or applicant_id is None:
        await call.answer(STALE_BUTTON, show_alert=True)
        return

    applicant = await session.get(User, applicant_id)
    if applicant is None or applicant.organization_id != organization.id:
        await call.answer("Сотрудник не найден.", show_alert=True)
        return

    try:
        role = RoleCode(parts[3])
    except ValueError:
        await call.answer(STALE_BUTTON, show_alert=True)
        return
    await approve_user(session, user=applicant, role=role, approved_by=user.id)
    await call.answer("Роль назначена")
    await call.message.edit_text(
        f"✅ <b>{esc(applicant.full_name)}</b> — доступ открыт\nРоль: {ROLE_TITLES[role]}"
    )
    await _notify_user(bot, applicant, f"Ваш доступ подтверждён. Роль: {ROLE_TITLES[role]}.\nНажмите /start.")


# ── Сотрудники ──────────────────────────────────────────────────────────────
@router.callback_query(F.data == "adm:users")
async def list_users(call: CallbackQuery, session: AsyncSession, organization: Organization, grants: dict[str, Grant]) -> None:
    if not _require(grants, "admin.users"):
        await call.answer("Недостаточно прав.", show_alert=True)
        return

    rows = await session.execute(
        select(User).where(User.organization_id == organization.id).order_by(User.full_name).limit(40)
    )
    people = list(rows.scalars().all())
    await call.answer()
    if not people:
        await call.message.answer("Сотрудников пока нет.")
        return

    role_rows = await session.execute(
        select(UserRole.user_id, Role.code).join(Role, Role.id == UserRole.role_id)
    )
    roles_by_user: dict[int, list[str]] = {}
    for user_id, code in role_rows.all():
        roles_by_user.setdefault(user_id, []).append(ROLE_TITLES.get(RoleCode(code), code))

    marks = {
        UserStatus.ACTIVE: "🟢",
        UserStatus.PENDING: "🟡",
        UserStatus.SUSPENDED: "🔴",
        UserStatus.REJECTED: "⚫",
    }
    lines = ["<b>Сотрудники</b>", ""]
    for person in people:
        titles = ", ".join(roles_by_user.get(person.id, [])) or "без роли"
        lines.append(f"{marks.get(UserStatus(person.status), '⚪')} {esc(person.full_name)} — {titles}")

    await call.message.answer("\n".join(lines))


# ── Отделы ──────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "adm:depts")
async def show_departments(call: CallbackQuery, session: AsyncSession, organization: Organization, grants: dict[str, Grant]) -> None:
    if not _require(grants, "admin.settings"):
        await call.answer("Недостаточно прав.", show_alert=True)
        return

    departments = await list_departments(session, organization.id)
    await call.answer()
    text = "<b>Отделы</b>\n\n" + (
        "\n".join(f"• {esc(d.name)}" for d in departments) if departments else "Пока не заведено ни одного."
    )
    await call.message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="➕ Добавить отдел", callback_data="adm:dept:new")]]
        ),
    )


@router.callback_query(F.data == "adm:dept:new")
async def new_department(call: CallbackQuery, state: FSMContext, grants: dict[str, Grant]) -> None:
    if not _require(grants, "admin.settings"):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    await call.answer()
    await call.message.answer("Название отдела?")
    await state.set_state(AdminForms.department_name)


@router.message(StateFilter(AdminForms.department_name), F.text)
async def save_department(
    message: Message, state: FSMContext, session: AsyncSession,
    organization: Organization, user: User, grants: dict[str, Grant],
) -> None:
    if not _require(grants, "admin.settings"):
        await state.clear()
        await message.answer("Недостаточно прав.")
        return

    name = message.text.strip()[:200]
    if len(name) < 2:
        await message.answer("Слишком короткое название. Напишите полностью.")
        return
    department = Department(organization_id=organization.id, name=name)
    session.add(department)
    await session.flush()
    await write_audit(
        session,
        actor_id=user.id,
        action="department.create",
        entity_type="department",
        entity_id=department.id,
        after={"name": name},
    )
    await state.clear()
    await message.answer(f"Отдел «{esc(name)}» создан.\nТеперь можно выпустить для него ссылку-приглашение.")


# ── Приглашения ─────────────────────────────────────────────────────────────
@router.callback_query(F.data == "adm:invites")
async def show_invites(call: CallbackQuery, session: AsyncSession, organization: Organization, grants: dict[str, Grant]) -> None:
    if not _require(grants, "admin.invites"):
        await call.answer("Недостаточно прав.", show_alert=True)
        return

    departments = await list_departments(session, organization.id)
    await call.answer()

    rows = [
        [InlineKeyboardButton(text=f"🔗 {d.name} — сотрудники", callback_data=f"adm:inv:{d.id}")]
        for d in departments
    ]
    rows.append([InlineKeyboardButton(text="🔗 Без отдела — сотрудники", callback_data="adm:inv:0")])

    active = await session.execute(
        select(Invite)
        .where(Invite.organization_id == organization.id, Invite.revoked_at.is_(None))
        .order_by(desc(Invite.created_at))
        .limit(10)
    )
    invites = list(active.scalars().all())
    text = "<b>Ссылки-приглашения</b>\n\nВыберите отдел — бот выдаст готовую ссылку.\n"
    if invites:
        text += "\nПоследние выпущенные:\n" + "\n".join(
            f"• {esc(i.label) or 'без названия'} — использовано {i.used_count}" for i in invites
        )

    await call.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("adm:inv:"))
async def make_invite(
    call: CallbackQuery, session: AsyncSession, organization: Organization,
    user: User, grants: dict[str, Grant], bot: Bot,
) -> None:
    if not _require(grants, "admin.invites"):
        await call.answer("Недостаточно прав.", show_alert=True)
        return

    department_id = callback_int(call.data) or None
    label = "Без отдела"
    if department_id:
        dept = await session.get(Department, department_id)
        label = dept.name if dept else label

    invite = await create_invite(
        session,
        organization_id=organization.id,
        created_by=user.id,
        role=RoleCode.EMPLOYEE,
        department_id=department_id,
        label=f"{label} — сотрудники",
        multi_use=True,
        max_uses=200,
        ttl_hours=None,
    )
    me = await bot.get_me()
    link = invite_link(me.username, invite.token)

    await call.answer("Ссылка готова")
    await call.message.answer(
        f"<b>Ссылка для отдела «{esc(label)}»</b>\n\n"
        f"<code>{link}</code>\n\n"
        "Отправьте её в чат отдела. Все, кто перейдёт, получат роль «Сотрудник» "
        "автоматически — подтверждать вручную не нужно."
    )


# ── Журнал ──────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "adm:audit")
async def show_audit(call: CallbackQuery, session: AsyncSession, grants: dict[str, Grant]) -> None:
    if not _require(grants, "admin.audit"):
        await call.answer("Недостаточно прав.", show_alert=True)
        return

    rows = await session.execute(select(AuditLog).order_by(desc(AuditLog.created_at)).limit(15))
    entries = list(rows.scalars().all())
    await call.answer()
    if not entries:
        await call.message.answer("Журнал пока пуст.")
        return

    names: dict[int, str] = {}
    actor_ids = {e.actor_id for e in entries if e.actor_id}
    if actor_ids:
        people = await session.execute(select(User.id, User.full_name).where(User.id.in_(actor_ids)))
        names = dict(people.all())

    lines = ["<b>Последние действия</b>", ""]
    for entry in entries:
        who = names.get(entry.actor_id or 0, "система")
        lines.append(f"{fmt_dt(entry.created_at)} · {esc(who)} · <code>{esc(entry.action)}</code>")

    await call.message.answer("\n".join(lines))


async def _notify_user(bot: Bot, person: User, text: str) -> None:
    """Бот только из диспетчера: см. пояснение в start.notify_admins."""
    try:
        await bot.send_message(person.telegram_user_id, text)
    except Exception:
        return
