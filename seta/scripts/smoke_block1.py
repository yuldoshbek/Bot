"""Проверка блока 1 без Telegram.

Прогоняет живой сценарий по базе: регистрация тремя путями, подтверждение
администратором, права на уровне записи, индикатор доступности, журнал аудита,
правило первого администратора.

ИЗОЛЯЦИЯ. Все тестовые данные живут в отдельных организациях с названием
"ТЕСТ ...". Уборка удаляет только их. Боевая организация и настоящие сотрудники
недосягаемы для этого скрипта — по идентификаторам Telegram ничего не удаляется,
потому что реальный ID сотрудника может попасть в любой числовой диапазон.

    docker compose -f docker-compose.yml -f docker-compose.dev.yml \
        run --rm --no-deps migrate python scripts/smoke_block1.py
"""
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, func, select

from app.core.db import session_scope
from app.core.timeutil import utcnow
from app.models import (
    Absence,
    AuditLog,
    Availability,
    AvailabilityLog,
    AvailabilityState,
    CalendarBlock,
    Delegation,
    Department,
    Invite,
    Organization,
    RoleCode,
    User,
    UserRole,
    UserStatus,
    WorkingHours,
)
from app.services.availability import get_view, open_executives, set_state
from app.services.bootstrap import bootstrap, ensure_default_working_hours, grant_role
from app.services.rbac import can_access_object, load_grants, scope_of, user_role_codes
from app.services.registration import (
    approve_user,
    create_invite,
    has_any_admin,
    list_departments,
    pending_users,
    start_registration,
)

TEST_ORG_PREFIX = "ТЕСТ "
TEST_TG_BASE = 900_000_000
passed = 0
failed = 0


def check(condition: bool, title: str, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  OK   {title}")
    else:
        failed += 1
        print(f"  FAIL {title} {detail}")


async def cleanup() -> None:
    """Удаляет только тестовые организации и всё, что к ним привязано."""
    async with session_scope() as session:
        org_ids = [
            row[0]
            for row in (
                await session.execute(
                    select(Organization.id).where(Organization.name.like(f"{TEST_ORG_PREFIX}%"))
                )
            ).all()
        ]
        if not org_ids:
            return

        user_ids = [
            row[0]
            for row in (
                await session.execute(select(User.id).where(User.organization_id.in_(org_ids)))
            ).all()
        ]
        if user_ids:
            for model in (
                UserRole, WorkingHours, AvailabilityState, AvailabilityLog,
                CalendarBlock, Absence,
            ):
                await session.execute(delete(model).where(model.user_id.in_(user_ids)))
            await session.execute(
                delete(Delegation).where(
                    (Delegation.from_user_id.in_(user_ids))
                    | (Delegation.to_user_id.in_(user_ids))
                )
            )
            await session.execute(delete(AuditLog).where(AuditLog.actor_id.in_(user_ids)))
            await session.execute(delete(AuditLog).where(AuditLog.on_behalf_of_id.in_(user_ids)))
            await session.execute(delete(User).where(User.id.in_(user_ids)))

        await session.execute(delete(Invite).where(Invite.organization_id.in_(org_ids)))
        await session.execute(delete(Department).where(Department.organization_id.in_(org_ids)))
        await session.execute(delete(Organization).where(Organization.id.in_(org_ids)))


async def make_test_org(session, name: str) -> Organization:
    org = Organization(name=f"{TEST_ORG_PREFIX}{name}", timezone="Asia/Tashkent")
    session.add(org)
    await session.flush()
    return org


async def main() -> None:
    await cleanup()

    async with session_scope() as session:
        # Справочники ролей и прав общие для всей базы.
        await bootstrap(session)

        org = await make_test_org(session, "Организация")

        # Администратор нужен сразу: иначе сработает правило первого вошедшего
        # и первый же сотрудник получит административные права.
        admin = User(
            organization_id=org.id,
            telegram_user_id=TEST_TG_BASE,
            full_name="ТЕСТ Администратор",
            status=UserStatus.ACTIVE,
            timezone="Asia/Tashkent",
            locale="ru",
        )
        session.add(admin)
        await session.flush()
        await ensure_default_working_hours(session, admin)
        await grant_role(session, admin, RoleCode.ADMIN)

        print("\n1. Отделы и ссылки-приглашения")
        finance = Department(organization_id=org.id, name="ТЕСТ Финансы")
        projects = Department(organization_id=org.id, name="ТЕСТ Проекты")
        session.add_all([finance, projects])
        await session.flush()

        dept_link = await create_invite(
            session, organization_id=org.id, created_by=admin.id,
            role=RoleCode.EMPLOYEE, department_id=finance.id,
            label="ТЕСТ Финансы — сотрудники", multi_use=True, max_uses=50, ttl_hours=None,
        )
        personal_link = await create_invite(
            session, organization_id=org.id, created_by=admin.id,
            role=RoleCode.EXECUTIVE, department_id=None,
            label="ТЕСТ Руководитель", multi_use=False,
        )
        check(len(await list_departments(session, org.id)) == 2, "отделы созданы")

        print("\n2. Регистрация по ссылке отдела — доступ сразу")
        ivanov = await start_registration(
            session, organization=org, telegram_user_id=TEST_TG_BASE + 1,
            telegram_username="ivanov", full_name="Иванов Иван",
            department_id=None, requested_role=RoleCode.EMPLOYEE, invite=dept_link,
        )
        check(ivanov.status == UserStatus.ACTIVE, "сотрудник активирован без подтверждения")
        check(ivanov.department_id == finance.id, "отдел взят из ссылки")
        check(
            RoleCode.ADMIN not in await user_role_codes(session, ivanov),
            "обычный сотрудник не получает административных прав",
        )

        print("\n3. Свободная заявка на повышенную роль — только через подтверждение")
        petrov = await start_registration(
            session, organization=org, telegram_user_id=TEST_TG_BASE + 2,
            telegram_username="petrov", full_name="Петров Пётр",
            department_id=finance.id, requested_role=RoleCode.DEPT_HEAD, invite=None,
        )
        check(petrov.status == UserStatus.PENDING, "заявка ждёт администратора")
        grants = await load_grants(session, petrov)
        check(not grants, "до подтверждения прав нет", f"получено: {list(grants)}")

        print("\n4. Многоразовую ссылку нельзя использовать для повышенной роли")
        leaked = await create_invite(
            session, organization_id=org.id, created_by=admin.id,
            role=RoleCode.DEPT_HEAD, department_id=projects.id,
            label="ТЕСТ утечка", multi_use=True, max_uses=10, ttl_hours=None,
        )
        sidorov = await start_registration(
            session, organization=org, telegram_user_id=TEST_TG_BASE + 3,
            telegram_username="sidorov", full_name="Сидоров Сидор",
            department_id=None, requested_role=RoleCode.EMPLOYEE, invite=leaked,
        )
        check(
            sidorov.status == UserStatus.PENDING,
            "пересланная ссылка не выдаёт роль начальника отдела",
        )

        print("\n5. Персональная одноразовая ссылка выдаёт роль сразу")
        rakhimov = await start_registration(
            session, organization=org, telegram_user_id=TEST_TG_BASE + 4,
            telegram_username="rakhimov", full_name="Рахимов Рустам",
            department_id=None, requested_role=RoleCode.EMPLOYEE, invite=personal_link,
        )
        check(rakhimov.status == UserStatus.ACTIVE, "руководитель активирован по личной ссылке")
        exec_grants = await load_grants(session, rakhimov)
        check("task.create" in exec_grants, "у руководителя есть право создавать поручения")

        print("\n6. Подтверждение заявки администратором")
        queue = await pending_users(session, org.id)
        check(len(queue) == 2, "заявки видны администратору", f"в очереди: {len(queue)}")
        await approve_user(session, user=petrov, role=RoleCode.DEPT_HEAD, approved_by=admin.id)
        check(petrov.status == UserStatus.ACTIVE, "после подтверждения доступ открыт")

        print("\n7. Права на уровне записи")
        emp_grants = await load_grants(session, ivanov)
        head_grants = await load_grants(session, petrov)

        check(str(scope_of(emp_grants, "task.read")) == "SELF", "сотрудник видит только свои записи")
        check(
            await can_access_object(session, ivanov, emp_grants, "task.read", owner_id=ivanov.id),
            "сотрудник открывает своё поручение",
        )
        check(
            not await can_access_object(
                session, ivanov, emp_grants, "task.read", owner_id=rakhimov.id
            ),
            "сотрудник НЕ открывает чужое поручение",
        )
        check(
            await can_access_object(
                session, petrov, head_grants, "task.read",
                owner_id=ivanov.id, department_id=finance.id,
            ),
            "начальник отдела видит своё подразделение",
        )
        check(
            not await can_access_object(
                session, petrov, head_grants, "task.read",
                owner_id=sidorov.id, department_id=projects.id,
            ),
            "начальник отдела НЕ видит соседнее подразделение",
        )
        check(
            not await can_access_object(session, ivanov, emp_grants, "admin.users", owner_id=ivanov.id),
            "у сотрудника нет административных прав",
        )

        print("\n8. Индикатор доступности руководителя")
        view = await set_state(
            session, user=rakhimov, state=Availability.OPEN, minutes=60,
            note="Кабинет 402", opens_late_slots=False,
        )
        check(view.is_open, "статус «принимаю» включён")
        check(
            any(u.id == rakhimov.id for u, _ in await open_executives(session, org.id)),
            "сотрудники видят, кто на связи",
        )

        state_row = (
            await session.execute(
                select(AvailabilityState).where(AvailabilityState.user_id == rakhimov.id)
            )
        ).scalar_one()
        state_row.until_at = utcnow() - timedelta(minutes=1)
        await session.flush()
        check(
            (await get_view(session, rakhimov.id)).state == Availability.OFFLINE,
            "истёкший статус снимается сам, без ручного выключения",
        )
        check(
            not any(u.id == rakhimov.id for u, _ in await open_executives(session, org.id)),
            "истёкший статус исчезает из списка «кто на связи»",
        )

        await set_state(
            session, user=rakhimov, state=Availability.OPEN, minutes=120,
            note="Поздний приём", opens_late_slots=True,
        )
        check(
            (await get_view(session, rakhimov.id)).opens_late_slots,
            "поздний приём открывает окна после рабочего дня",
        )

        print("\n9. Рабочее время и журнал")
        hours = (
            await session.execute(
                select(WorkingHours).where(
                    WorkingHours.user_id == rakhimov.id, WorkingHours.weekday == 0
                )
            )
        ).scalar_one()
        check(
            hours.start_time.strftime("%H:%M") == "09:00"
            and hours.end_time.strftime("%H:%M") == "19:00",
            "рабочий день по умолчанию 09:00–19:00",
            f"получено {hours.start_time}-{hours.end_time}",
        )
        saturday = (
            await session.execute(
                select(WorkingHours).where(
                    WorkingHours.user_id == rakhimov.id, WorkingHours.weekday == 5
                )
            )
        ).scalar_one()
        check(not saturday.is_working, "суббота нерабочая")

        actions = await session.execute(
            select(AuditLog.action, func.count(AuditLog.id))
            .where(AuditLog.actor_id.in_([ivanov.id, petrov.id, rakhimov.id, admin.id]))
            .group_by(AuditLog.action)
        )
        logged = dict(actions.all())
        check("user.register" in logged, "регистрация записана в журнал")
        check("user.approve" in logged, "подтверждение записано в журнал")
        check("availability.set" in logged, "переключение доступности записано в журнал")

        print("\n10. Пустая система: первый вошедший становится администратором")
        empty_org = await make_test_org(session, "Организация без админа")
        check(not await has_any_admin(session, empty_org.id), "в новой организации администраторов нет")

        founder = await start_registration(
            session, organization=empty_org, telegram_user_id=TEST_TG_BASE + 5,
            telegram_username="founder", full_name="Каримов Карим",
            department_id=None, requested_role=RoleCode.ASSISTANT, invite=None,
        )
        founder_roles = await user_role_codes(session, founder)
        check(founder.status == UserStatus.ACTIVE, "первый вошедший активирован сразу")
        check(RoleCode.ADMIN in founder_roles, "первому выдана роль администратора")
        check(
            RoleCode.ASSISTANT not in founder_roles,
            "повышенную роль первый вошедший себе НЕ выдаёт",
            f"получены роли: {sorted(str(r) for r in founder_roles)}",
        )
        check(
            RoleCode.EMPLOYEE in founder_roles,
            "вместо неё выдана роль рядового сотрудника",
        )

        second = await start_registration(
            session, organization=empty_org, telegram_user_id=TEST_TG_BASE + 6,
            telegram_username="second", full_name="Назаров Назар",
            department_id=None, requested_role=RoleCode.ASSISTANT, invite=None,
        )
        check(
            second.status == UserStatus.PENDING,
            "второй уже проходит подтверждение — правило срабатывает один раз",
        )

    print("\n11. Уборка не трогает боевые данные")
    async with session_scope() as session:
        real_before = await session.scalar(
            select(func.count(User.id)).where(
                User.organization_id.notin_(
                    select(Organization.id).where(Organization.name.like(f"{TEST_ORG_PREFIX}%"))
                )
            )
        )
    await cleanup()
    async with session_scope() as session:
        real_after = await session.scalar(select(func.count(User.id)))
        test_left = await session.scalar(
            select(func.count(Organization.id)).where(
                Organization.name.like(f"{TEST_ORG_PREFIX}%")
            )
        )
    check(real_after == real_before, "настоящие сотрудники не удалены", f"{real_before} → {real_after}")
    check(test_left == 0, "тестовые организации убраны")

    print(f"\n{'=' * 46}\nПройдено: {passed}   Ошибок: {failed}\n{'=' * 46}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
