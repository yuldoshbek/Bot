"""Права доступа.

Три уровня контроля работают одновременно:
  роль   - кто ты,
  право  - какое действие тебе разрешено,
  область - над какими записями.

Наличие права task.read само по себе не открывает ни одного поручения:
объектная проверка (can_access_object) дополнительно спрашивает, являетесь ли
вы автором, исполнителем, проверяющим или начальником исполнителя.
"""
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import utcnow
from app.models.enums import RoleCode, Scope
from app.models.org import Department
from app.models.rbac import Delegation, Permission, Role, RolePermission, UserRole
from app.models.user import User

# ── Каталог прав ────────────────────────────────────────────────────────────
PERMISSIONS: dict[str, str] = {
    "meeting.read": "Просмотр встреч",
    "meeting.create": "Создание встреч",
    "meeting.approve": "Подтверждение заявок на встречу",
    "meeting.reschedule": "Перенос встреч",
    "meeting.cancel": "Отмена встреч",
    "meeting.attendance": "Отметка явки",
    "meeting.rate": "Оценка встречи",
    "calendar.read_free": "Просмотр свободных окон",
    "calendar.read_busy": "Просмотр занятости без деталей",
    "calendar.read_full": "Полный просмотр календаря",
    "calendar.manage": "Управление рабочим временем и блокировками",
    "availability.set": "Управление своим индикатором доступности",
    "availability.set_for_other": "Переключение доступности руководителя",
    "task.read": "Просмотр поручений",
    "task.create": "Создание поручений",
    "task.assign": "Назначение исполнителя",
    "task.review": "Проверка выполнения",
    "task.close": "Закрытие поручения",
    "task.extend": "Одобрение продления срока",
    "file.read": "Просмотр документов",
    "file.upload": "Загрузка документов",
    "analytics.read": "Просмотр аналитики",
    "analytics.read_org": "Аналитика по всей организации",
    "admin.users": "Управление пользователями",
    "admin.roles": "Управление ролями",
    "admin.settings": "Настройки системы",
    "admin.audit": "Просмотр журнала аудита",
    "admin.invites": "Управление приглашениями",
}

# ── Матрица ролей: право -> область видимости ───────────────────────────────
ROLE_MATRIX: dict[RoleCode, dict[str, Scope]] = {
    RoleCode.EXECUTIVE: {
        "meeting.read": Scope.ORGANIZATION,
        "meeting.create": Scope.ORGANIZATION,
        "meeting.approve": Scope.ORGANIZATION,
        "meeting.reschedule": Scope.ORGANIZATION,
        "meeting.cancel": Scope.ORGANIZATION,
        "meeting.attendance": Scope.ORGANIZATION,
        "meeting.rate": Scope.ORGANIZATION,
        "calendar.read_full": Scope.ORGANIZATION,
        "calendar.manage": Scope.SELF,
        "availability.set": Scope.SELF,
        "task.read": Scope.ORGANIZATION,
        "task.create": Scope.ORGANIZATION,
        "task.assign": Scope.ORGANIZATION,
        "task.review": Scope.ORGANIZATION,
        "task.close": Scope.ORGANIZATION,
        "task.extend": Scope.ORGANIZATION,
        "file.read": Scope.ORGANIZATION,
        "file.upload": Scope.ORGANIZATION,
        "analytics.read_org": Scope.ORGANIZATION,
    },
    RoleCode.ASSISTANT: {
        "meeting.read": Scope.ORGANIZATION,
        "meeting.create": Scope.ORGANIZATION,
        "meeting.approve": Scope.ORGANIZATION,
        "meeting.reschedule": Scope.ORGANIZATION,
        "meeting.cancel": Scope.ORGANIZATION,
        "meeting.attendance": Scope.ORGANIZATION,
        "calendar.read_full": Scope.ORGANIZATION,
        "calendar.manage": Scope.ORGANIZATION,
        "availability.set": Scope.SELF,
        "availability.set_for_other": Scope.ORGANIZATION,
        "task.read": Scope.ORGANIZATION,
        "task.create": Scope.ORGANIZATION,
        "task.assign": Scope.ORGANIZATION,
        "task.review": Scope.ORGANIZATION,
        "task.extend": Scope.ORGANIZATION,
        "file.read": Scope.ORGANIZATION,
        "file.upload": Scope.ORGANIZATION,
        "analytics.read_org": Scope.ORGANIZATION,
    },
    RoleCode.DEPT_HEAD: {
        "meeting.read": Scope.DEPARTMENT,
        "meeting.create": Scope.DEPARTMENT,
        "calendar.read_busy": Scope.ORGANIZATION,
        "calendar.read_free": Scope.ORGANIZATION,
        "calendar.manage": Scope.SELF,
        "availability.set": Scope.SELF,
        "task.read": Scope.DEPARTMENT,
        "task.create": Scope.DEPARTMENT,
        "task.assign": Scope.DEPARTMENT,
        "task.review": Scope.DEPARTMENT,
        "task.extend": Scope.DEPARTMENT,
        "file.read": Scope.DEPARTMENT,
        "file.upload": Scope.DEPARTMENT,
        "analytics.read": Scope.DEPARTMENT,
    },
    RoleCode.EMPLOYEE: {
        "meeting.read": Scope.SELF,
        "meeting.create": Scope.SELF,
        "calendar.read_free": Scope.ORGANIZATION,
        "calendar.manage": Scope.SELF,
        "availability.set": Scope.SELF,
        "task.read": Scope.SELF,
        "task.create": Scope.SELF,
        "file.read": Scope.SELF,
        "file.upload": Scope.SELF,
    },
    # Администратор управляет системой, но не видит содержание встреч и поручений.
    RoleCode.ADMIN: {
        "admin.users": Scope.ORGANIZATION,
        "admin.roles": Scope.ORGANIZATION,
        "admin.settings": Scope.ORGANIZATION,
        "admin.audit": Scope.ORGANIZATION,
        "admin.invites": Scope.ORGANIZATION,
        "calendar.manage": Scope.ORGANIZATION,
        "availability.set": Scope.SELF,
    },
    RoleCode.AUDITOR: {
        "admin.audit": Scope.ORGANIZATION,
        "analytics.read_org": Scope.ORGANIZATION,
    },
}

ROLE_TITLES: dict[RoleCode, str] = {
    RoleCode.EXECUTIVE: "Руководитель",
    RoleCode.ASSISTANT: "Ассистент",
    RoleCode.DEPT_HEAD: "Начальник отдела",
    RoleCode.EMPLOYEE: "Сотрудник",
    RoleCode.ADMIN: "Администратор",
    RoleCode.AUDITOR: "Аудитор",
}

# Роли, которые нельзя получить самостоятельной регистрацией.
ELEVATED_ROLES: set[RoleCode] = {
    RoleCode.EXECUTIVE,
    RoleCode.ASSISTANT,
    RoleCode.DEPT_HEAD,
    RoleCode.ADMIN,
    RoleCode.AUDITOR,
}


@dataclass(slots=True)
class Grant:
    """Что именно разрешено пользователю: право, область и источник."""

    permission: str
    scope: Scope
    via_delegation_from: int | None = None


async def load_grants(session: AsyncSession, user: User) -> dict[str, Grant]:
    """Собирает права пользователя: собственные роли плюс действующие делегирования."""
    now = utcnow()
    grants: dict[str, Grant] = {}

    rows = await session.execute(
        select(Permission.code, RolePermission.scope)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(Role, Role.id == RolePermission.role_id)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(
            UserRole.user_id == user.id,
            (UserRole.valid_from.is_(None)) | (UserRole.valid_from <= now),
            (UserRole.valid_to.is_(None)) | (UserRole.valid_to >= now),
        )
    )
    for code, scope in rows.all():
        current = grants.get(code)
        if current is None or _scope_rank(scope) > _scope_rank(current.scope):
            grants[code] = Grant(permission=code, scope=Scope(scope))

    delegations = await session.execute(
        select(Delegation).where(
            Delegation.to_user_id == user.id,
            Delegation.revoked_at.is_(None),
            Delegation.valid_from <= now,
            Delegation.valid_to >= now,
        )
    )
    for delegation in delegations.scalars().all():
        for code in delegation.permissions or []:
            grants[code] = Grant(
                permission=code,
                scope=Scope.ORGANIZATION,
                via_delegation_from=delegation.from_user_id,
            )

    return grants


def _scope_rank(scope: str) -> int:
    order = {
        Scope.SELF: 1,
        Scope.SUBORDINATES: 2,
        Scope.DEPARTMENT: 3,
        Scope.ORGANIZATION: 4,
    }
    return order.get(Scope(scope), 0)


def has_permission(grants: dict[str, Grant], permission: str) -> bool:
    return permission in grants


def scope_of(grants: dict[str, Grant], permission: str) -> Scope | None:
    grant = grants.get(permission)
    return grant.scope if grant else None


async def can_access_object(
    session: AsyncSession,
    user: User,
    grants: dict[str, Grant],
    permission: str,
    *,
    owner_id: int | None = None,
    related_user_ids: set[int] | None = None,
    department_id: int | None = None,
) -> bool:
    """Объектная проверка: право есть, но открыт ли доступ именно к этой записи."""
    scope = scope_of(grants, permission)
    if scope is None:
        return False
    if scope == Scope.ORGANIZATION:
        return True

    participants = set(related_user_ids or set())
    if owner_id is not None:
        participants.add(owner_id)

    if scope == Scope.SELF:
        return user.id in participants

    if scope == Scope.DEPARTMENT:
        if department_id is not None and department_id == user.department_id:
            return True
        if participants:
            rows = await session.execute(
                select(User.id).where(
                    User.id.in_(participants), User.department_id == user.department_id
                )
            )
            return bool(rows.first())
        return False

    if scope == Scope.SUBORDINATES:
        if not participants:
            return False
        rows = await session.execute(
            select(User.id).where(User.id.in_(participants), User.manager_id == user.id)
        )
        return bool(rows.first())

    return False


async def visible_department_ids(session: AsyncSession, user: User) -> set[int]:
    """Свой отдел и все вложенные в него."""
    if user.department_id is None:
        return set()
    result = {user.department_id}
    frontier = {user.department_id}
    while frontier:
        rows = await session.execute(
            select(Department.id).where(Department.parent_id.in_(frontier))
        )
        children = {row[0] for row in rows.all()} - result
        if not children:
            break
        result |= children
        frontier = children
    return result


async def user_role_codes(session: AsyncSession, user: User) -> set[RoleCode]:
    rows = await session.execute(
        select(Role.code).join(UserRole, UserRole.role_id == Role.id).where(UserRole.user_id == user.id)
    )
    return {RoleCode(code) for (code,) in rows.all()}


async def has_role(session: AsyncSession, user: User, role: RoleCode) -> bool:
    return role in await user_role_codes(session, user)
