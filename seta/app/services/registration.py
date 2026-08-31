"""Регистрация сотрудников.

Три пути, один результат - подтверждённая связь Telegram и корпоративной личности:
  1. ссылка отдела      - многоразовая, роль задана заранее, подтверждение не нужно;
  2. свободная заявка   - человек выбирает роль сам, администратор подтверждает;
  3. личное приглашение - одноразовая ссылка на конкретного человека.

Правило без исключений: роль, выбранная пользователем самому себе, - это заявка.
Права она не даёт до подтверждения администратором.
"""
import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.timeutil import utcnow
from app.models.enums import RoleCode, UserStatus
from app.models.org import Department, Organization
from app.models.user import Invite, User
from app.services.audit import write_audit
from app.services.bootstrap import ensure_default_working_hours, grant_role
from app.services.rbac import ELEVATED_ROLES

INVITE_TTL_HOURS = 72


class RegistrationError(Exception):
    """Ошибка регистрации с текстом, который можно показать пользователю."""


def new_token() -> str:
    return secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]


async def create_invite(
    session: AsyncSession,
    *,
    organization_id: int,
    created_by: int | None,
    role: RoleCode = RoleCode.EMPLOYEE,
    department_id: int | None = None,
    label: str | None = None,
    multi_use: bool = False,
    max_uses: int = 1,
    ttl_hours: int | None = INVITE_TTL_HOURS,
) -> Invite:
    invite = Invite(
        organization_id=organization_id,
        token=new_token(),
        department_id=department_id,
        role=role,
        label=label,
        is_multi_use=multi_use,
        max_uses=max_uses if multi_use else 1,
        expires_at=utcnow() + timedelta(hours=ttl_hours) if ttl_hours else None,
        created_by=created_by,
    )
    session.add(invite)
    await session.flush()
    await write_audit(
        session,
        actor_id=created_by,
        action="invite.create",
        entity_type="invite",
        entity_id=invite.id,
        after={"role": role, "department_id": department_id, "multi_use": multi_use},
    )
    return invite


def invite_link(bot_username: str, token: str) -> str:
    return f"https://t.me/{bot_username}?start=inv_{token}"


async def resolve_invite(session: AsyncSession, token: str) -> Invite | None:
    invite = (
        await session.execute(select(Invite).where(Invite.token == token))
    ).scalar_one_or_none()
    if invite is None or invite.revoked_at is not None:
        return None
    if invite.expires_at is not None and invite.expires_at < utcnow():
        return None
    if not invite.is_multi_use and invite.used_count >= 1:
        return None
    if invite.is_multi_use and invite.used_count >= invite.max_uses:
        return None
    return invite


async def get_user_by_telegram_id(session: AsyncSession, telegram_user_id: int) -> User | None:
    return (
        await session.execute(select(User).where(User.telegram_user_id == telegram_user_id))
    ).scalar_one_or_none()


async def start_registration(
    session: AsyncSession,
    *,
    organization: Organization,
    telegram_user_id: int,
    telegram_username: str | None,
    full_name: str,
    department_id: int | None,
    requested_role: RoleCode,
    invite: Invite | None = None,
) -> User:
    """Создаёт пользователя. По ссылке отдела активирует сразу, иначе ставит в очередь заявок."""
    existing = await get_user_by_telegram_id(session, telegram_user_id)
    if existing is not None:
        raise RegistrationError("Вы уже зарегистрированы в системе.")

    # Роль из приглашения имеет приоритет над тем, что человек выбрал сам.
    effective_role = RoleCode(invite.role) if invite else requested_role

    # Персональную одноразовую ссылку администратор создал под конкретного человека -
    # она выдаёт роль сразу. Многоразовую ссылку отдела могут переслать кому угодно,
    # поэтому повышенные роли через неё не выдаются никогда.
    auto_approve = invite is not None and (
        not invite.is_multi_use or effective_role not in ELEVATED_ROLES
    )

    user = User(
        organization_id=organization.id,
        telegram_user_id=telegram_user_id,
        telegram_username=telegram_username,
        full_name=full_name.strip(),
        department_id=invite.department_id if invite and invite.department_id else department_id,
        status=UserStatus.ACTIVE if auto_approve else UserStatus.PENDING,
        requested_role=effective_role,
        timezone=organization.timezone or settings.default_timezone,
        locale=settings.default_locale,
        last_seen_at=utcnow(),
    )
    session.add(user)
    await session.flush()
    await ensure_default_working_hours(session, user)

    if auto_approve:
        await grant_role(session, user, effective_role, granted_by=invite.created_by if invite else None)

    if invite is not None:
        invite.used_count += 1

    await write_audit(
        session,
        actor_id=user.id,
        action="user.register",
        entity_type="user",
        entity_id=user.id,
        after={
            "full_name": user.full_name,
            "requested_role": effective_role,
            "department_id": user.department_id,
            "auto_approved": auto_approve,
            "invite_token": invite.token if invite else None,
        },
    )
    return user


async def approve_user(
    session: AsyncSession,
    *,
    user: User,
    role: RoleCode,
    approved_by: int,
) -> None:
    before = {"status": user.status, "requested_role": user.requested_role}
    user.status = UserStatus.ACTIVE
    await grant_role(session, user, role, granted_by=approved_by)
    await write_audit(
        session,
        actor_id=approved_by,
        action="user.approve",
        entity_type="user",
        entity_id=user.id,
        before=before,
        after={"status": user.status, "role": role},
    )


async def reject_user(session: AsyncSession, *, user: User, rejected_by: int, reason: str | None = None) -> None:
    before = {"status": user.status}
    user.status = UserStatus.REJECTED
    await write_audit(
        session,
        actor_id=rejected_by,
        action="user.reject",
        entity_type="user",
        entity_id=user.id,
        before=before,
        after={"status": user.status},
        reason=reason,
    )


async def set_phone(session: AsyncSession, *, user: User, phone: str) -> None:
    before = {"phone": user.phone}
    user.phone = phone
    await write_audit(
        session,
        actor_id=user.id,
        action="user.phone_confirmed",
        entity_type="user",
        entity_id=user.id,
        before=before,
        after={"phone": phone},
    )


async def list_departments(session: AsyncSession, organization_id: int) -> list[Department]:
    rows = await session.execute(
        select(Department).where(Department.organization_id == organization_id).order_by(Department.name)
    )
    return list(rows.scalars().all())


async def pending_users(session: AsyncSession, organization_id: int) -> list[User]:
    rows = await session.execute(
        select(User)
        .where(User.organization_id == organization_id, User.status == UserStatus.PENDING)
        .order_by(User.created_at)
    )
    return list(rows.scalars().all())
