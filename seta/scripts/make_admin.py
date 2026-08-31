"""Выдать человеку роль администратора.

Аварийный инструмент: нужен, если администратор потерял доступ или если
система осталась без единого администратора. Обычный путь — подтверждение
заявки в боте, а не этот скрипт.

    docker compose -f docker-compose.yml -f docker-compose.dev.yml \
        run --rm --no-deps migrate python scripts/make_admin.py 976130670

Дополнительно можно выдать вторую роль:

    ... scripts/make_admin.py 976130670 ASSISTANT
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.core.db import session_scope
from app.models.enums import RoleCode, UserStatus
from app.models.rbac import Role, UserRole
from app.models.user import User
from app.services.audit import write_audit
from app.services.bootstrap import grant_role
from app.services.rbac import ROLE_TITLES


async def main() -> None:
    if len(sys.argv) < 2:
        print("Укажите Telegram ID: python scripts/make_admin.py 123456789 [РОЛЬ]")
        sys.exit(2)

    telegram_id = int(sys.argv[1])
    extra_role = RoleCode(sys.argv[2]) if len(sys.argv) > 2 else None

    async with session_scope() as session:
        user = (
            await session.execute(select(User).where(User.telegram_user_id == telegram_id))
        ).scalar_one_or_none()

        if user is None:
            print(f"Сотрудник с Telegram ID {telegram_id} не найден.")
            print("Сначала пройдите регистрацию в боте командой /start.")
            sys.exit(1)

        before = {"status": user.status}
        user.status = UserStatus.ACTIVE

        await grant_role(session, user, RoleCode.ADMIN)
        granted = [RoleCode.ADMIN]

        if extra_role is not None:
            await grant_role(session, user, extra_role)
            granted.append(extra_role)
        elif user.requested_role:
            requested = RoleCode(user.requested_role)
            if requested != RoleCode.ADMIN:
                await grant_role(session, user, requested)
                granted.append(requested)

        await write_audit(
            session,
            actor_id=user.id,
            action="user.grant_admin",
            entity_type="user",
            entity_id=user.id,
            before=before,
            after={"status": user.status, "roles": [str(r) for r in granted]},
            reason="выдано скриптом восстановления доступа",
            source="script",
        )

        rows = await session.execute(
            select(Role.code).join(UserRole, UserRole.role_id == Role.id).where(UserRole.user_id == user.id)
        )
        current = [ROLE_TITLES.get(RoleCode(code), code) for (code,) in rows.all()]

        print(f"{user.full_name}: доступ открыт")
        print(f"Роли: {', '.join(current)}")
        print("Откройте бота и отправьте /start — меню обновится.")


if __name__ == "__main__":
    asyncio.run(main())
