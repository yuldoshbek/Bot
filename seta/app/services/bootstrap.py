"""Первичное наполнение: организация, роли, права, первый администратор.

Выполняется при каждом старте и безопасно к повторам: ничего не дублирует.
"""
from datetime import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.timeutil import parse_hhmm, utcnow
from app.models.enums import RoleCode, UserStatus
from app.models.org import Organization
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.schedule import WorkingHours
from app.models.user import User
from app.services.rbac import ELEVATED_ROLES, PERMISSIONS, ROLE_MATRIX, ROLE_TITLES


async def ensure_organization(session: AsyncSession) -> Organization:
    org = (await session.execute(select(Organization).limit(1))).scalar_one_or_none()
    if org is None:
        org = Organization(name=settings.org_name, timezone=settings.default_timezone)
        session.add(org)
        await session.flush()
    return org


async def ensure_permissions(session: AsyncSession) -> dict[str, Permission]:
    existing = {
        p.code: p for p in (await session.execute(select(Permission))).scalars().all()
    }
    for code, description in PERMISSIONS.items():
        if code not in existing:
            permission = Permission(code=code, description=description)
            session.add(permission)
            existing[code] = permission
    await session.flush()
    return existing


async def ensure_roles(session: AsyncSession) -> dict[RoleCode, Role]:
    existing = {
        RoleCode(r.code): r for r in (await session.execute(select(Role))).scalars().all()
    }
    for code, title in ROLE_TITLES.items():
        role = existing.get(code)
        if role is None:
            role = Role(code=code, title_ru=title, is_elevated=code in ELEVATED_ROLES)
            session.add(role)
            existing[code] = role
        else:
            role.title_ru = title
            role.is_elevated = code in ELEVATED_ROLES
    await session.flush()
    return existing


async def ensure_role_permissions(session: AsyncSession) -> None:
    permissions = await ensure_permissions(session)
    roles = await ensure_roles(session)

    current = {
        (rp.role_id, rp.permission_id): rp
        for rp in (await session.execute(select(RolePermission))).scalars().all()
    }
    for role_code, mapping in ROLE_MATRIX.items():
        role = roles[role_code]
        for permission_code, scope in mapping.items():
            permission = permissions[permission_code]
            key = (role.id, permission.id)
            link = current.get(key)
            if link is None:
                session.add(
                    RolePermission(role_id=role.id, permission_id=permission.id, scope=scope)
                )
            elif link.scope != scope:
                link.scope = scope
    await session.flush()


async def ensure_default_working_hours(session: AsyncSession, user: User) -> None:
    """Рабочая неделя по умолчанию: пн-пт 09:00-19:00, обед 13:00-14:00."""
    rows = await session.execute(select(WorkingHours).where(WorkingHours.user_id == user.id))
    if rows.first() is not None:
        return

    start: time = parse_hhmm(settings.work_start)
    end: time = parse_hhmm(settings.work_end)
    lunch_start: time = parse_hhmm(settings.lunch_start)
    lunch_end: time = parse_hhmm(settings.lunch_end)
    late_end: time = parse_hhmm(settings.late_end)

    for weekday in range(7):
        session.add(
            WorkingHours(
                user_id=user.id,
                weekday=weekday,
                is_working=weekday < 5,
                start_time=start,
                end_time=end,
                lunch_start=lunch_start,
                lunch_end=lunch_end,
                allow_late=False,
                late_end_time=late_end,
                buffer_minutes=settings.buffer_minutes,
            )
        )
    await session.flush()


async def grant_role(
    session: AsyncSession, user: User, role_code: RoleCode, granted_by: int | None = None
) -> None:
    role = (
        await session.execute(select(Role).where(Role.code == role_code))
    ).scalar_one_or_none()
    if role is None:
        roles = await ensure_roles(session)
        role = roles[role_code]

    existing = (
        await session.execute(
            select(UserRole).where(UserRole.user_id == user.id, UserRole.role_id == role.id)
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(UserRole(user_id=user.id, role_id=role.id, granted_by=granted_by))
        await session.flush()


async def revoke_all_roles(session: AsyncSession, user: User) -> None:
    rows = await session.execute(select(UserRole).where(UserRole.user_id == user.id))
    for link in rows.scalars().all():
        await session.delete(link)
    await session.flush()


async def bootstrap(session: AsyncSession) -> Organization:
    org = await ensure_organization(session)
    await ensure_role_permissions(session)

    # Первый администратор: указан в переменной окружения, роль выдаётся один раз.
    admin_tg_id = settings.bootstrap_admin_telegram_id
    if admin_tg_id:
        user = (
            await session.execute(
                select(User).where(User.telegram_user_id == admin_tg_id)
            )
        ).scalar_one_or_none()
        if user is None:
            user = User(
                organization_id=org.id,
                telegram_user_id=admin_tg_id,
                full_name="Администратор",
                status=UserStatus.ACTIVE,
                timezone=settings.default_timezone,
                locale=settings.default_locale,
                last_seen_at=utcnow(),
            )
            session.add(user)
            await session.flush()
            await ensure_default_working_hours(session, user)
        if user.status != UserStatus.ACTIVE:
            user.status = UserStatus.ACTIVE
        await grant_role(session, user, RoleCode.ADMIN)

    return org
