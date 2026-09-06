"""Администрирование внутри Telegram.

Администратор ничего не заводит вручную: он подтверждает заявки одним касанием
и рассылает ссылки отделов. Содержание встреч и поручений ему не показывается -
роль управляет системой, а не читает переписку руководства.
"""
import re
from datetime import date, datetime, time

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.common import MenuButton, MENU_ADMIN, admin_menu_kb, approval_kb, approval_role_kb
from app.bot.utils import callback_int
from app.core.i18n import t
from app.core.text import esc
from app.core.timeutil import fmt_dt
from app.models.audit import AuditLog
from app.models.enums import RoleCode, UserStatus
from app.models.org import Department, Organization
from app.models.enums import AbsenceKind, QuotaPeriod
from app.models.schedule import Absence, Holiday
from app.models.rbac import Role, UserRole
from app.models.user import Invite, User
from app.services.audit import write_audit
from app.services import features as features_service
from app.services import orgadmin as settings_service
from app.services.rbac import Grant, has_permission, role_title
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
    session: AsyncSession, data: str | None, organization: Organization, locale: str
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


@router.message(MenuButton(MENU_ADMIN))
async def admin_menu(message: Message, session: AsyncSession, organization: Organization, grants: dict[str, Grant], locale: str) -> None:
    if not _require(grants, "admin.users"):
        await message.answer(t("admin.only", locale))
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
        f"<b>{t('admin.title', locale)}</b>\n\n"
        + t("admin.summary", locale, people=active,
            departments=departments, pending=pending),
        reply_markup=admin_menu_kb(locale),
    )


# ── Заявки ──────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "adm:pending")
async def show_pending(call: CallbackQuery, session: AsyncSession, organization: Organization, grants: dict[str, Grant], locale: str) -> None:
    if not _require(grants, "admin.users"):
        await call.answer(t("admin.no_rights", locale), show_alert=True)
        return

    applicants = await pending_users(session, organization.id)
    await call.answer()
    if not applicants:
        await call.message.answer(t("admin.pending.empty", locale))
        return

    for applicant in applicants[:20]:
        await call.message.answer(await _user_card(session, applicant, locale), reply_markup=approval_kb(applicant.id, locale))


async def _user_card(session: AsyncSession, applicant: User, locale: str) -> str:
    department = t("profile.department_none", locale)
    if applicant.department_id:
        dept = await session.get(Department, applicant.department_id)
        if dept:
            department = dept.name
    role_code = applicant.requested_role or RoleCode.EMPLOYEE
    return (
        f"👤 <b>{esc(applicant.full_name)}</b>\n"
        f"🏢 {esc(department)}\n"
        f"🔑 {t('start.requested_role', locale, role=role_title(role_code, locale))}\n"
        f"📱 {esc(applicant.phone) or t('profile.phone_none', locale)}\n"
        f"🕐 {t('admin.pending.at', locale)}: {fmt_dt(applicant.created_at)}"
    )


@router.callback_query(F.data.startswith("adm:approve:"))
async def approve(
    call: CallbackQuery, session: AsyncSession, organization: Organization,
    user: User, grants: dict[str, Grant], bot: Bot, locale: str,
) -> None:
    if not _require(grants, "admin.users"):
        await call.answer(t("admin.no_rights", locale), show_alert=True)
        return

    applicant = await _applicant_of(session, call.data, organization)
    if applicant is None:
        await call.answer(t("admin.request_not_found", locale), show_alert=True)
        return
    if applicant.status != UserStatus.PENDING:
        await call.answer(t("admin.request_decided", locale), show_alert=True)
        return

    role = RoleCode(applicant.requested_role or RoleCode.EMPLOYEE)
    await approve_user(session, user=applicant, role=role, approved_by=user.id)
    await call.answer(t("admin.accepted", locale))
    await call.message.edit_text("✅ " + t(
        "admin.access_opened", locale,
        name=esc(applicant.full_name), role=role_title(role, locale),
    ))
    # Письмо получает заявитель — значит, на его языке, а не на языке админа.
    await _notify_user(bot, applicant, t(
        "admin.access_granted_dm", applicant.locale,
        role=role_title(role, applicant.locale),
    ))


@router.callback_query(F.data.startswith("adm:reject:"))
async def reject(
    call: CallbackQuery, session: AsyncSession, organization: Organization,
    user: User, grants: dict[str, Grant], bot: Bot, locale: str,
) -> None:
    if not _require(grants, "admin.users"):
        await call.answer(t("admin.no_rights", locale), show_alert=True)
        return

    applicant = await _applicant_of(session, call.data, organization)
    if applicant is None:
        await call.answer(t("admin.request_not_found", locale), show_alert=True)
        return

    await reject_user(session, user=applicant, rejected_by=user.id)
    await call.answer(t("admin.rejected", locale))
    await call.message.edit_text(
        "❌ " + t("admin.request_rejected", locale, name=esc(applicant.full_name))
    )
    await _notify_user(bot, applicant, t("admin.request_rejected_dm", applicant.locale))


@router.callback_query(F.data.startswith("adm:role:"))
async def change_role(call: CallbackQuery, grants: dict[str, Grant], locale: str) -> None:
    if not _require(grants, "admin.roles"):
        await call.answer(t("admin.no_rights", locale), show_alert=True)
        return
    applicant_id = callback_int(call.data)
    if applicant_id is None:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=approval_role_kb(applicant_id, locale))


@router.callback_query(F.data.startswith("adm:card:"))
async def back_to_card(
    call: CallbackQuery, session: AsyncSession, locale: str
) -> None:
    applicant_id = callback_int(call.data)
    if applicant_id is None:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=approval_kb(applicant_id, locale))


@router.callback_query(F.data.startswith("adm:setrole:"))
async def set_role(
    call: CallbackQuery, session: AsyncSession, organization: Organization,
    user: User, grants: dict[str, Grant], bot: Bot, locale: str,
) -> None:
    if not _require(grants, "admin.roles"):
        await call.answer(t("admin.no_rights", locale), show_alert=True)
        return

    parts = call.data.split(":", 3)
    applicant_id = callback_int(call.data, 2)
    if len(parts) < 4 or applicant_id is None:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return

    applicant = await session.get(User, applicant_id)
    if applicant is None or applicant.organization_id != organization.id:
        await call.answer(t("admin.user_not_found", locale), show_alert=True)
        return

    try:
        role = RoleCode(parts[3])
    except ValueError:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    await approve_user(session, user=applicant, role=role, approved_by=user.id)
    await call.answer(t("admin.role_set", locale))
    await call.message.edit_text("✅ " + t(
        "admin.access_opened", locale,
        name=esc(applicant.full_name), role=role_title(role, locale),
    ))
    await _notify_user(bot, applicant, t(
        "admin.access_granted_dm", applicant.locale,
        role=role_title(role, applicant.locale),
    ))


# ── Сотрудники ──────────────────────────────────────────────────────────────
@router.callback_query(F.data == "adm:users")
async def list_users(call: CallbackQuery, session: AsyncSession, organization: Organization, grants: dict[str, Grant], locale: str) -> None:
    if not _require(grants, "admin.users"):
        await call.answer(t("admin.no_rights", locale), show_alert=True)
        return

    rows = await session.execute(
        select(User).where(User.organization_id == organization.id).order_by(User.full_name).limit(40)
    )
    people = list(rows.scalars().all())
    await call.answer()
    if not people:
        await call.message.answer(t("admin.users.empty", locale))
        return

    role_rows = await session.execute(
        select(UserRole.user_id, Role.code).join(Role, Role.id == UserRole.role_id)
    )
    roles_by_user: dict[int, list[str]] = {}
    for user_id, code in role_rows.all():
        roles_by_user.setdefault(user_id, []).append(role_title(code, locale))

    marks = {
        UserStatus.ACTIVE: "🟢",
        UserStatus.PENDING: "🟡",
        UserStatus.SUSPENDED: "🔴",
        UserStatus.REJECTED: "⚫",
    }
    lines = [f"<b>{t('admin.users.title', locale)}</b>", ""]
    for person in people:
        titles = ", ".join(roles_by_user.get(person.id, [])) or t("role.none", locale)
        lines.append(f"{marks.get(UserStatus(person.status), '⚪')} {esc(person.full_name)} — {titles}")

    await call.message.answer("\n".join(lines))


# ── Отделы ──────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "adm:depts")
async def show_departments(call: CallbackQuery, session: AsyncSession, organization: Organization, grants: dict[str, Grant], locale: str) -> None:
    if not _require(grants, "admin.settings"):
        await call.answer(t("admin.no_rights", locale), show_alert=True)
        return

    departments = await list_departments(session, organization.id)
    await call.answer()
    text = f"<b>{t('admin.departments.title', locale)}</b>\n\n" + (
        "\n".join(f"• {esc(d.name)}" for d in departments) if departments else t("admin.departments.empty", locale)
    )
    await call.message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=t("admin.departments.add", locale), callback_data="adm:dept:new")]]
        ),
    )


@router.callback_query(F.data == "adm:dept:new")
async def new_department(call: CallbackQuery, state: FSMContext, grants: dict[str, Grant], locale: str) -> None:
    if not _require(grants, "admin.settings"):
        await call.answer(t("admin.no_rights", locale), show_alert=True)
        return
    await call.answer()
    await call.message.answer(t("admin.departments.ask_name", locale))
    await state.set_state(AdminForms.department_name)


@router.message(StateFilter(AdminForms.department_name), F.text)
async def save_department(
    message: Message, state: FSMContext, session: AsyncSession,
    organization: Organization, user: User, grants: dict[str, Grant], locale: str,
) -> None:
    if not _require(grants, "admin.settings"):
        await state.clear()
        await message.answer(t("admin.no_rights", locale))
        return

    name = message.text.strip()[:200]
    if len(name) < 2:
        await message.answer(t("admin.departments.name_short", locale))
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
    await message.answer(t("admin.departments.created", locale, name=esc(name)))


# ── Приглашения ─────────────────────────────────────────────────────────────
@router.callback_query(F.data == "adm:invites")
async def show_invites(call: CallbackQuery, session: AsyncSession, organization: Organization, grants: dict[str, Grant], locale: str) -> None:
    if not _require(grants, "admin.invites"):
        await call.answer(t("admin.no_rights", locale), show_alert=True)
        return

    departments = await list_departments(session, organization.id)
    await call.answer()

    rows = [
        [InlineKeyboardButton(
            text="🔗 " + t("admin.invites.employees", locale, name=d.name),
            callback_data=f"adm:inv:{d.id}",
        )]
        for d in departments
    ]
    rows.append([InlineKeyboardButton(text=t("admin.invites.no_department", locale), callback_data="adm:inv:0")])

    active = await session.execute(
        select(Invite)
        .where(Invite.organization_id == organization.id, Invite.revoked_at.is_(None))
        .order_by(desc(Invite.created_at))
        .limit(10)
    )
    invites = list(active.scalars().all())
    text = (
        f"<b>{t('admin.invites.title', locale)}</b>\n\n"
        f"{t('admin.invites.hint', locale)}\n"
    )
    if invites:
        unnamed = t("admin.invites.unnamed", locale)
        text += f"\n{t('admin.invites.recent', locale)}\n" + "\n".join(
            "• " + t("admin.invites.used", locale,
                     name=esc(i.label) or unnamed, count=i.used_count)
            for i in invites
        )

    await call.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("adm:inv:"))
async def make_invite(
    call: CallbackQuery, session: AsyncSession, organization: Organization,
    user: User, grants: dict[str, Grant], bot: Bot, locale: str,
) -> None:
    if not _require(grants, "admin.invites"):
        await call.answer(t("admin.no_rights", locale), show_alert=True)
        return

    department_id = callback_int(call.data) or None
    label = t("admin.invites.no_department_short", locale)
    if department_id:
        dept = await session.get(Department, department_id)
        label = dept.name if dept else label

    invite = await create_invite(
        session,
        organization_id=organization.id,
        created_by=user.id,
        role=RoleCode.EMPLOYEE,
        department_id=department_id,
        # Ярлык приглашения хранится в базе и попадает в журнал: он на языке
        # того, кто выпустил ссылку, и потом не переводится.
        label=t("admin.invites.employees", locale, name=label),
        multi_use=True,
        max_uses=200,
        ttl_hours=None,
    )
    me = await bot.get_me()
    link = invite_link(me.username, invite.token)

    await call.answer(t("admin.invites.ready", locale))
    await call.message.answer(
        t("admin.invites.link", locale, name=esc(label), link=link)
    )


# ── Журнал ──────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "adm:audit")
async def show_audit(call: CallbackQuery, session: AsyncSession, grants: dict[str, Grant], locale: str) -> None:
    if not _require(grants, "admin.audit"):
        await call.answer(t("admin.no_rights", locale), show_alert=True)
        return

    rows = await session.execute(select(AuditLog).order_by(desc(AuditLog.created_at)).limit(15))
    entries = list(rows.scalars().all())
    await call.answer()
    if not entries:
        await call.message.answer(t("admin.audit.empty", locale))
        return

    names: dict[int, str] = {}
    actor_ids = {e.actor_id for e in entries if e.actor_id}
    if actor_ids:
        people = await session.execute(select(User.id, User.full_name).where(User.id.in_(actor_ids)))
        names = dict(people.all())

    lines = [f"<b>{t('admin.audit.title', locale)}</b>", ""]
    for entry in entries:
        who = names.get(entry.actor_id or 0, t("admin.audit.system", locale))
        lines.append(f"{fmt_dt(entry.created_at)} · {esc(who)} · <code>{esc(entry.action)}</code>")

    await call.message.answer("\n".join(lines))


async def _notify_user(bot: Bot, person: User, text: str, locale: str) -> None:
    """Бот только из диспетчера: см. пояснение в start.notify_admins."""
    try:
        await bot.send_message(person.telegram_user_id, text)
    except Exception:
        return


# ── Настройки организации ───────────────────────────────────────────────────
# До этой фазы рабочие часы, квоты, праздники и отпуска правил разработчик.
# Раздел «Администрирование» архитектуры обещал управление без программирования;
# здесь это обещание и выполняется.


class SettingsForms(StatesGroup):
    hours_value = State()
    quota_value = State()
    holiday_value = State()
    absence_value = State()


def _back_kb(locale: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t("admin.back", locale), callback_data="adm:home")]]
    )


@router.callback_query(F.data == "adm:home")
async def admin_home(call: CallbackQuery, grants: dict[str, Grant], locale: str) -> None:
    if not _require(grants, "admin.users"):
        await call.answer(t("admin.no_rights", locale), show_alert=True)
        return
    await call.message.answer(f"<b>{t('admin.title', locale)}</b>",
                              reply_markup=admin_menu_kb(locale))
    await call.answer()


@router.callback_query(F.data == "adm:features")
async def show_features(
    call: CallbackQuery, session: AsyncSession, organization: Organization,
    grants: dict[str, Grant], locale: str,
) -> None:
    if not _require(grants, "admin.settings"):
        await call.answer(t("admin.no_rights", locale), show_alert=True)
        return

    switches = await features_service.switches(session, organization.id, locale)
    rows = [
        [InlineKeyboardButton(
            text=f"{'🟢' if item.enabled else '⚪️'} {item.title}",
            callback_data=f"adm:feat:{item.code}",
        )]
        for item in switches
    ]
    await call.message.answer(
        f"<b>{t('admin.features.title', locale)}</b>\n\n"
        f"{t('admin.features.hint', locale)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows + _back_kb(locale).inline_keyboard),
    )
    await call.answer()


@router.callback_query(F.data.startswith("adm:feat:"))
async def toggle_feature(
    call: CallbackQuery, session: AsyncSession, organization: Organization,
    user: User, grants: dict[str, Grant], locale: str,
) -> None:
    if not _require(grants, "admin.settings"):
        await call.answer(t("admin.no_rights", locale), show_alert=True)
        return
    code = (call.data or "").split(":")[-1]
    state = await features_service.load(session, organization.id)
    if code not in state:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return

    problem = await features_service.switch(
        session, organization_id=organization.id, code=code,
        enabled=not state[code], actor=user,
    )
    if problem:
        await call.answer(problem, show_alert=True)
        return
    await call.answer(t("admin.features.done", locale))
    await show_features(call, session, organization, grants)


@router.callback_query(F.data == "adm:hours")
async def show_hours(
    call: CallbackQuery, session: AsyncSession, organization: Organization,
    grants: dict[str, Grant], locale: str,
) -> None:
    if not _require(grants, "admin.settings"):
        await call.answer(t("admin.no_rights", locale), show_alert=True)
        return

    people = await _executives_and_heads(session, organization.id)
    if not people:
        await call.answer(t("admin.hours.nobody", locale), show_alert=True)
        return
    await call.message.answer(
        f"<b>{t('admin.hours', locale)}</b>\n\n{t('admin.hours.whose', locale)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=person.full_name, callback_data=f"adm:hrs:{person.id}")]
            for person in people[:8]
        ] + _back_kb(locale).inline_keyboard),
    )
    await call.answer()


@router.callback_query(F.data.startswith("adm:hrs:"))
async def hours_of_person(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    organization: Organization, grants: dict[str, Grant], locale: str,
) -> None:
    if not _require(grants, "admin.settings"):
        await call.answer(t("admin.no_rights", locale), show_alert=True)
        return
    subject = await _applicant_of(session, call.data, organization)
    if subject is None:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return

    rows = await settings_service.hours_of(session, subject)
    title = t("admin.hours.title", locale, name=esc(subject.full_name))
    lines = [f"<b>{title}</b>", ""]
    for row in rows:
        day = t(settings_service.WEEKDAY_KEYS[row.weekday], locale)
        if not row.is_working:
            lines.append(f"{day}: {t('admin.hours.dayoff', locale)}")
            continue
        lunch = (
            ", " + t("admin.hours.lunch", locale,
                     start=f"{row.lunch_start:%H:%M}", end=f"{row.lunch_end:%H:%M}")
            if row.lunch_start and row.lunch_end
            else ""
        )
        lines.append(
            f"{day}: {row.start_time:%H:%M}–{row.end_time:%H:%M}{lunch}"
        )
    if rows:
        lines += [
            "",
            t("admin.hours.buffer", locale, minutes=rows[0].buffer_minutes),
            t("admin.hours.in_a_row", locale, count=rows[0].max_consecutive_meetings),
        ]
    lines += [
        "",
        t("admin.hours.howto", locale),
        t("admin.hours.example_day", locale),
        t("admin.hours.example_lunch", locale),
        t("admin.hours.example_buffer", locale),
        t("admin.hours.example_row", locale),
    ]
    await state.clear()
    await state.update_data(subject_id=subject.id)
    await state.set_state(SettingsForms.hours_value)
    await call.message.answer("\n".join(lines), reply_markup=_back_kb(locale))
    await call.answer()


@router.message(SettingsForms.hours_value, F.text)
async def hours_apply(
    message: Message, state: FSMContext, session: AsyncSession,
    user: User, grants: dict[str, Grant], locale: str,
) -> None:
    data = await state.get_data()
    subject = await session.get(User, data.get("subject_id", 0))
    if subject is None:
        await state.clear()
        await message.answer(t("error.stale_button", locale))
        return

    parsed = _parse_hours_command(message.text or "")
    if parsed is None:
        await message.answer(t("admin.hours.unclear", locale))
        return

    result = await settings_service.set_hours(
        session, actor=user, grants=grants, subject=subject, **parsed
    )
    if not result.ok:
        await message.answer(result.reason or t("admin.failed", locale))
        return
    await state.clear()
    await message.answer(
        t("admin.hours.changed", locale, name=esc(subject.full_name))
        + "\n" + t("admin.hours.applies", locale),
        reply_markup=_back_kb(locale),
    )


def _parse_hours_command(text: str, locale: str) -> dict | None:
    """Разбирает одну строку настройки расписания.

    Одна строка вместо мастера из пяти шагов: администратор правит буфер
    и уходит, а не проходит анкету ради одного числа.
    """
    raw = (text or "").strip().lower().replace("—", "-").replace("–", "-")
    span = re.fullmatch(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", raw)
    if span:
        return {
            "start": time(int(span.group(1)), int(span.group(2))),
            "end": time(int(span.group(3)), int(span.group(4))),
        }
    # Разбор принимает оба языка независимо от настройки: администратор пишет
    # то слово, что первым пришло в голову, и «tushlik» должно работать так же,
    # как «обед». Понимать и говорить — разные вещи.
    lunch = re.fullmatch(
        r"(?:обед|tushlik|тушлик)\s+(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", raw
    )
    if lunch:
        return {
            "lunch_start": time(int(lunch.group(1)), int(lunch.group(2))),
            "lunch_end": time(int(lunch.group(3)), int(lunch.group(4))),
        }
    buffer_match = re.fullmatch(r"(?:буфер|bufer|буфер)\s+(\d{1,3})", raw)
    if buffer_match:
        return {"buffer_minutes": int(buffer_match.group(1))}
    row_match = re.fullmatch(r"(?:подряд|ketma-ket|кетма-кет)\s+(\d{1,2})", raw)
    if row_match:
        return {"max_consecutive": int(row_match.group(1))}
    return None


@router.callback_query(F.data == "adm:holidays")
async def show_holidays(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    organization: Organization, grants: dict[str, Grant], locale: str,
) -> None:
    if not _require(grants, "admin.settings"):
        await call.answer(t("admin.no_rights", locale), show_alert=True)
        return

    items = await settings_service.holidays_of(
        session, organization_id=organization.id, since=date.today()
    )
    lines = [f"<b>{t('admin.holidays.title', locale)}</b>", ""]
    rows: list[list[InlineKeyboardButton]] = []
    if items:
        for item in items:
            mark = t("admin.holidays.working", locale) if item.is_working_day else t("admin.holidays.dayoff", locale)
            lines.append(f"{item.day:%d.%m.%Y} — {esc(item.title)} ({mark})")
            rows.append([InlineKeyboardButton(
                text=f"🗑 {item.day:%d.%m} {item.title[:20]}",
                callback_data=f"adm:hol_del:{item.id}",
            )])
    else:
        lines.append(t("admin.holidays.empty", locale))
    lines += [
        "",
        t("admin.holidays.examples_title", locale),
        t("admin.holidays.example_off", locale),
        t("admin.holidays.example_work", locale),
    ]
    await state.clear()
    await state.set_state(SettingsForms.holiday_value)
    await call.message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=rows[:6] + _back_kb(locale).inline_keyboard
        ),
    )
    await call.answer()


@router.message(SettingsForms.holiday_value, F.text)
async def holiday_apply(
    message: Message, state: FSMContext, session: AsyncSession,
    user: User, grants: dict[str, Grant], locale: str,
) -> None:
    raw = (message.text or "").strip()
    working = raw.startswith("+")
    raw = raw.lstrip("+").strip()
    parts = raw.split(None, 1)
    if len(parts) != 2:
        await message.answer(t("admin.holidays.need_both", locale))
        return
    try:
        day = datetime.strptime(parts[0], "%d.%m.%Y").date()
    except ValueError:
        await message.answer(t("admin.holidays.bad_date", locale))
        return

    result = await settings_service.set_holiday(
        session, actor=user, grants=grants, day=day,
        title=parts[1], is_working_day=working,
    )
    if not result.ok:
        await message.answer(result.reason or t("admin.failed", locale))
        return
    await state.clear()
    kind = t(
        "admin.holidays.as_working" if working else "admin.holidays.as_dayoff", locale
    )
    await message.answer(
        "📆 " + t("admin.holidays.declared", locale,
                 date=f"{day:%d.%m.%Y}", kind=kind)
        + f" — {esc(parts[1])}",
        reply_markup=_back_kb(locale),
    )


@router.callback_query(F.data.startswith("adm:hol_del:"))
async def holiday_delete(
    call: CallbackQuery, session: AsyncSession, user: User, grants: dict[str, Grant], locale: str
) -> None:
    holiday = await session.get(Holiday, callback_int(call.data) or 0)
    if holiday is None:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    problem = await settings_service.drop_holiday(
        session, actor=user, grants=grants, holiday=holiday
    )
    if problem:
        await call.answer(problem, show_alert=True)
        return
    await call.answer(t("common.deleted_short", locale))
    await call.message.answer(t("admin.holidays.removed", locale,
                                date=f"{holiday.day:%d.%m.%Y}"))


async def _executives_and_heads(session: AsyncSession, organization_id: int, locale: str) -> list[User]:
    """Те, чьё расписание вообще имеет смысл настраивать."""
    return list(
        (
            await session.execute(
                select(User)
                .join(UserRole, UserRole.user_id == User.id)
                .join(Role, Role.id == UserRole.role_id)
                .where(
                    User.organization_id == organization_id,
                    User.status == UserStatus.ACTIVE,
                    Role.code.in_([RoleCode.EXECUTIVE, RoleCode.DEPT_HEAD, RoleCode.ASSISTANT]),
                )
                .order_by(User.full_name)
                .distinct()
                .limit(8)
            )
        ).scalars().all()
    )


@router.callback_query(F.data == "adm:quotas")
async def show_quotas(
    call: CallbackQuery, session: AsyncSession, organization: Organization,
    grants: dict[str, Grant], locale: str,
) -> None:
    if not _require(grants, "admin.settings"):
        await call.answer(t("admin.no_rights", locale), show_alert=True)
        return

    chiefs = await _executives_and_heads(session, organization.id)
    if not chiefs:
        await call.answer(t("admin.quotas.nobody", locale), show_alert=True)
        return

    lines = [f"<b>{t('admin.quotas.title', locale)}</b>", ""]
    for chief in chiefs:
        quotas = await settings_service.quotas_of(session, chief)
        if not quotas:
            continue
        lines.append(f"<b>{esc(chief.full_name)}</b>")
        for quota in quotas:
            subject = (
                await session.get(User, quota.subject_user_id)
                if quota.subject_user_id
                else await session.get(Department, quota.subject_department_id)
            )
            name = getattr(subject, "full_name", None) or getattr(subject, "name", "—")
            period = t("admin.quotas.per_week", locale) if quota.period == QuotaPeriod.WEEK else t("admin.quotas.per_month", locale)
            lines.append("• " + t("admin.quotas.line", locale,
                                  name=esc(name), minutes=quota.minutes, period=period))
    if len(lines) == 2:
        lines.append(t("admin.quotas.none", locale))
    lines += ["", t("admin.quotas.whose", locale)]

    await call.message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=chief.full_name, callback_data=f"adm:qown:{chief.id}")]
            for chief in chiefs
        ] + _back_kb(locale).inline_keyboard),
    )
    await call.answer()


@router.callback_query(F.data.startswith("adm:qown:"))
async def quota_owner(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    organization: Organization, grants: dict[str, Grant], locale: str,
) -> None:
    if not _require(grants, "admin.settings"):
        await call.answer(t("admin.no_rights", locale), show_alert=True)
        return
    owner = await _applicant_of(session, call.data, organization)
    if owner is None:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return

    departments = await list_departments(session, organization.id)
    if not departments:
        await call.answer(t("admin.quotas.need_departments", locale), show_alert=True)
        return
    await state.clear()
    await state.update_data(owner_id=owner.id)
    await call.message.answer(
        t("admin.quotas.for_whom", locale, name=esc(owner.full_name)),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=item.name, callback_data=f"adm:qdep:{item.id}")]
            for item in departments[:8]
        ] + _back_kb(locale).inline_keyboard),
    )
    await call.answer()


@router.callback_query(F.data.startswith("adm:qdep:"))
async def quota_department(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    organization: Organization, grants: dict[str, Grant], locale: str,
) -> None:
    department = await session.get(Department, callback_int(call.data) or 0)
    if department is None or department.organization_id != organization.id:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    await state.update_data(department_id=department.id)
    await state.set_state(SettingsForms.quota_value)
    await call.message.answer(
        t("admin.quotas.ask_minutes", locale, department=esc(department.name)),
        reply_markup=_back_kb(locale),
    )
    await call.answer()


@router.message(SettingsForms.quota_value, F.text)
async def quota_apply(
    message: Message, state: FSMContext, session: AsyncSession,
    user: User, grants: dict[str, Grant], locale: str,
) -> None:
    data = await state.get_data()
    owner = await session.get(User, data.get("owner_id", 0))
    department = await session.get(Department, data.get("department_id", 0))
    if owner is None or department is None:
        await state.clear()
        await message.answer(t("error.stale_button", locale))
        return

    parts = (message.text or "").strip().lower().split()
    if not parts or not parts[0].isdigit():
        await message.answer(t("admin.quotas.need_number", locale))
        return
    monthly = len(parts) > 1 and parts[1].startswith(("мес", "oy", "ой"))
    period = QuotaPeriod.MONTH if monthly else QuotaPeriod.WEEK

    result = await settings_service.set_quota(
        session, actor=user, grants=grants, owner=owner,
        minutes=int(parts[0]), period=period, subject_department=department,
    )
    if not result.ok:
        await message.answer(result.reason or t("admin.failed", locale))
        return
    await state.clear()
    when = t("admin.quotas.per_week", locale) if period == QuotaPeriod.WEEK else t("admin.quotas.per_month", locale)
    await message.answer(
        t("admin.quotas.done", locale, department=esc(department.name),
          minutes=int(parts[0]), period=when)
        + f" · {esc(owner.full_name)}",
        reply_markup=_back_kb(locale),
    )


@router.callback_query(F.data == "adm:absences")
async def show_absences(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    organization: Organization, grants: dict[str, Grant], locale: str,
) -> None:
    if not _require(grants, "admin.settings"):
        await call.answer(t("admin.no_rights", locale), show_alert=True)
        return

    items = await settings_service.absences_of(
        session, organization_id=organization.id, since=date.today()
    )
    lines = [f"<b>{t('admin.absences.title', locale)}</b>", ""]
    rows: list[list[InlineKeyboardButton]] = []
    for item in items:
        person = await session.get(User, item.user_id)
        kind = t(settings_service.ABSENCE_KEYS.get(item.kind, "absence.other"), locale)
        lines.append(
            f"{esc(person.full_name) if person else '—'}: {kind} "
            f"{item.start_date:%d.%m}–{item.end_date:%d.%m}"
        )
        rows.append([InlineKeyboardButton(
            text=f"🗑 {(person.full_name if person else '')[:18]} {item.start_date:%d.%m}",
            callback_data=f"adm:abs_del:{item.id}",
        )])
    if not items:
        lines.append(t("admin.absences.empty", locale))
    lines += [
        "",
        t("admin.absences.pick", locale),
    ]

    people = (
        await session.execute(
            select(User)
            .where(User.organization_id == organization.id, User.status == UserStatus.ACTIVE)
            .order_by(User.full_name)
            .limit(8)
        )
    ).scalars().all()
    rows += [
        [InlineKeyboardButton(text=f"➕ {person.full_name}", callback_data=f"adm:abs:{person.id}")]
        for person in people
    ]
    await state.clear()
    await call.message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=rows[:10] + _back_kb(locale).inline_keyboard
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith("adm:abs:"))
async def absence_person(
    call: CallbackQuery, state: FSMContext, session: AsyncSession,
    organization: Organization, grants: dict[str, Grant], locale: str,
) -> None:
    if not _require(grants, "admin.settings"):
        await call.answer(t("admin.no_rights", locale), show_alert=True)
        return
    subject = await _applicant_of(session, call.data, organization)
    if subject is None:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    await state.clear()
    await state.update_data(subject_id=subject.id)
    await state.set_state(SettingsForms.absence_value)
    await call.message.answer(
        t("admin.absences.form", locale, name=esc(subject.full_name)),
        reply_markup=_back_kb(locale),
    )
    await call.answer()


@router.message(SettingsForms.absence_value, F.text)
async def absence_apply(
    message: Message, state: FSMContext, session: AsyncSession,
    user: User, grants: dict[str, Grant], locale: str,
) -> None:
    data = await state.get_data()
    subject = await session.get(User, data.get("subject_id", 0))
    if subject is None:
        await state.clear()
        await message.answer(t("error.stale_button", locale))
        return

    parts = (message.text or "").strip().lower().split()
    if len(parts) < 2:
        await message.answer(t("admin.absences.need_dates", locale))
        return
    try:
        start = datetime.strptime(parts[0], "%d.%m.%Y").date()
        end = datetime.strptime(parts[1], "%d.%m.%Y").date()
    except ValueError:
        await message.answer(t("admin.absences.bad_date", locale))
        return

    word = parts[2] if len(parts) > 2 else ""
    kind = AbsenceKind.VACATION
    if word.startswith(("команд", "xizmat", "safar", "хизмат", "сафар")):
        kind = AbsenceKind.TRIP
    elif word.startswith(("больн", "kasal", "касал")):
        kind = AbsenceKind.SICK

    result = await settings_service.set_absence(
        session, actor=user, grants=grants, subject=subject,
        kind=kind, start_date=start, end_date=end,
    )
    if not result.ok:
        await message.answer(result.reason or t("admin.failed", locale))
        return
    await state.clear()
    await message.answer(
        t("admin.absences.added", locale,
          name=esc(subject.full_name),
          kind=t(settings_service.ABSENCE_KEYS[kind], locale),
          from_date=f"{start:%d.%m}", to_date=f"{end:%d.%m}")
        + "\n" + t("admin.absences.applies", locale),
        reply_markup=_back_kb(locale),
    )


@router.callback_query(F.data.startswith("adm:abs_del:"))
async def absence_delete(
    call: CallbackQuery, session: AsyncSession, user: User, grants: dict[str, Grant], locale: str
) -> None:
    absence = await session.get(Absence, callback_int(call.data) or 0)
    if absence is None:
        await call.answer(t("error.stale_button", locale), show_alert=True)
        return
    problem = await settings_service.drop_absence(
        session, actor=user, grants=grants, absence=absence
    )
    if problem:
        await call.answer(problem, show_alert=True)
        return
    await call.answer(t("common.deleted_short", locale))
    await call.message.answer(t("admin.absences.removed", locale))
