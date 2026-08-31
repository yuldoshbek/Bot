"""Проверка блока 1 без Telegram.

Прогоняет живой сценарий по базе: регистрация тремя путями, подтверждение
администратором, проверка прав на уровне записи, индикатор доступности,
журнал аудита. Запускается на пустой или уже наполненной базе.

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
    RoleCode,
    User,
    UserRole,
    UserStatus,
    WorkingHours,
)
from app.services.availability import get_view, open_executives, set_state
from app.services.bootstrap import bootstrap
from app.services.rbac import can_access_object, load_grants, scope_of
from app.services.registration import (
    approve_user,
    create_invite,
    list_departments,
    pending_users,
    start_registration,
)

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
    """Убирает данные предыдущего прогона, не трогая настоящих сотрудников."""
    async with session_scope() as session:
        rows = await session.execute(
            select(User.id).where(User.telegram_user_id >= TEST_TG_BASE)
        )
        ids = [row[0] for row in rows.all()]
        if ids:
            for model in (
                UserRole, WorkingHours, AvailabilityState, AvailabilityLog,
                CalendarBlock, Absence,
            ):
                await session.execute(delete(model).where(model.user_id.in_(ids)))
            await session.execute(
                delete(Delegation).where(
                    (Delegation.from_user_id.in_(ids)) | (Delegation.to_user_id.in_(ids))
                )
            )
            await session.execute(delete(AuditLog).where(AuditLog.actor_id.in_(ids)))
            await session.execute(delete(AuditLog).where(AuditLog.on_behalf_of_id.in_(ids)))
            await session.execute(delete(Invite).where(Invite.created_by.in_(ids)))
            await session.execute(delete(User).where(User.id.in_(ids)))
        await session.execute(delete(Department).where(Department.name.like("ТЕСТ %")))


async def main() -> None:
    await cleanup()

    async with session_scope() as session:
        org = await bootstrap(session)

        print("\n1. Отделы и ссылки-приглашения")
        finance = Department(organization_id=org.id, name="ТЕСТ Финансы")
        projects = Department(organization_id=org.id, name="ТЕСТ Проекты")
        session.add_all([finance, projects])
        await session.flush()

        dept_link = await create_invite(
            session,
            organization_id=org.id,
            created_by=None,
            role=RoleCode.EMPLOYEE,
            department_id=finance.id,
            label="ТЕСТ Финансы — сотрудники",
            multi_use=True,
            max_uses=50,
            ttl_hours=None,
        )
        personal_link = await create_invite(
            session,
            organization_id=org.id,
            created_by=None,
            role=RoleCode.EXECUTIVE,
            department_id=None,
            label="ТЕСТ Руководитель",
            multi_use=False,
        )
        check(len(await list_departments(session, org.id)) >= 2, "отделы созданы")

        print("\n2. Регистрация по ссылке отдела — доступ сразу")
        ivanov = await start_registration(
            session, organization=org, telegram_user_id=TEST_TG_BASE + 1,
            telegram_username="ivanov", full_name="Иванов Иван",
            department_id=None, requested_role=RoleCode.EMPLOYEE, invite=dept_link,
        )
        check(ivanov.status == UserStatus.ACTIVE, "сотрудник активирован без подтверждения")
        check(ivanov.department_id == finance.id, "отдел взят из ссылки")

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
            session, organization_id=org.id, created_by=None,
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
        check(len(queue) >= 2, "заявки видны администратору", f"в очереди: {len(queue)}")
        await approve_user(session, user=petrov, role=RoleCode.DEPT_HEAD, approved_by=rakhimov.id)
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
        visible = await open_executives(session, org.id)
        check(
            any(u.id == rakhimov.id for u, _ in visible),
            "сотрудники видят, кто на связи",
        )

        state_row = (
            await session.execute(
                select(AvailabilityState).where(AvailabilityState.user_id == rakhimov.id)
            )
        ).scalar_one()
        state_row.until_at = utcnow() - timedelta(minutes=1)
        await session.flush()
        expired = await get_view(session, rakhimov.id)
        check(
            expired.state == Availability.OFFLINE,
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
        late = await get_view(session, rakhimov.id)
        check(late.opens_late_slots, "поздний приём открывает окна после рабочего дня")

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
            .where(AuditLog.actor_id.in_([ivanov.id, petrov.id, rakhimov.id]))
            .group_by(AuditLog.action)
        )
        logged = dict(actions.all())
        check("user.register" in logged, "регистрация записана в журнал")
        check("user.approve" in logged, "подтверждение записано в журнал")
        check("availability.set" in logged, "переключение доступности записано в журнал")

    print(f"\n{'=' * 46}\nПройдено: {passed}   Ошибок: {failed}\n{'=' * 46}")
    await cleanup()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
