"""Проверка блока укрепления.

Каждая проверка здесь закрывает конкретную находку ревью. Если она позеленела
случайно — значит, проверка написана плохо: почти все сценарии проверяют отказ,
а отказ легко получить по неверной причине. Поэтому рядом с каждым «нельзя»
стоит парный случай «а вот так можно».

    docker compose -f docker-compose.yml -f docker-compose.dev.yml \
        run --rm --no-deps migrate python scripts/smoke_hardening.py
"""
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.core.db import session_scope
from app.core.timeutil import utcnow
from app.api.health_page import render
from app.models import (
    AuditLog,
    ErrorLog,
    Department,
    ExtensionStatus,
    Notification,
    Organization,
    Priority,
    RoleCode,
    Task,
    TaskComment,
    TaskEvent,
    TaskExtension,
    TaskStatus,
    User,
    UserRole,
    UserStatus,
    WorkingHours,
)
from app.models.enums import NotificationPriority, NotificationStatus
from app.models.rbac import Role
from app.services import deadlines
from app.services import tasks as service
from app.services.health import collect, record_error
from app.services.bootstrap import bootstrap, ensure_default_working_hours, grant_role
from app.services.notifications import (
    GROUP_MAX_ITEMS,
    MAX_ATTEMPTS,
    enqueue,
    group_messages,
    mark_failed,
    pending_for_delivery,
)
from app.services.rbac import can_access_object, load_grants, user_role_codes
from app.services.registration import (
    approve_user,
    is_empty_organization,
    start_registration,
)

TEST_ORG_PREFIX = "ТЕСТ "
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
    async with session_scope() as session:
        org_ids = [
            row[0] for row in (
                await session.execute(
                    select(Organization.id).where(Organization.name.like(f"{TEST_ORG_PREFIX}%"))
                )
            ).all()
        ]
        if not org_ids:
            return
        user_ids = [
            row[0] for row in (
                await session.execute(select(User.id).where(User.organization_id.in_(org_ids)))
            ).all()
        ]
        task_ids = [
            row[0] for row in (
                await session.execute(select(Task.id).where(Task.organization_id.in_(org_ids)))
            ).all()
        ]
        if task_ids:
            for model in (TaskEvent, TaskComment, TaskExtension):
                await session.execute(delete(model).where(model.task_id.in_(task_ids)))
            await session.execute(delete(Task).where(Task.id.in_(task_ids)))
        if user_ids:
            for model in (UserRole, WorkingHours, Notification):
                await session.execute(delete(model).where(model.user_id.in_(user_ids)))
            await session.execute(delete(AuditLog).where(AuditLog.actor_id.in_(user_ids)))
            await session.execute(delete(User).where(User.id.in_(user_ids)))
        await session.execute(delete(Department).where(Department.organization_id.in_(org_ids)))
        await session.execute(delete(Organization).where(Organization.id.in_(org_ids)))


async def person(session, org, name, role, department=None, tg=0, status=UserStatus.ACTIVE) -> User:
    user = User(
        organization_id=org.id, telegram_user_id=tg, full_name=name, status=status,
        department_id=department.id if department else None,
        timezone="Asia/Tashkent", locale="ru",
    )
    session.add(user)
    await session.flush()
    await ensure_default_working_hours(session, user)
    await grant_role(session, user, role)
    return user


async def main() -> None:
    await cleanup()
    tg = 930_000_000

    async with session_scope() as session:
        await bootstrap(session)
        org = Organization(name=f"{TEST_ORG_PREFIX}Укрепление", timezone="Asia/Tashkent")
        other_org = Organization(name=f"{TEST_ORG_PREFIX}Соседи", timezone="Asia/Tashkent")
        session.add_all([org, other_org])
        await session.flush()

        finance = Department(organization_id=org.id, name="ТЕСТ Финансы")
        projects = Department(organization_id=org.id, name="ТЕСТ Проекты")
        session.add_all([finance, projects])
        await session.flush()

        admin = await person(session, org, "Админ", RoleCode.ADMIN, tg=tg + 1)
        chief = await person(session, org, "Руководитель", RoleCode.EXECUTIVE, tg=tg + 2)
        assistant = await person(session, org, "Ассистент", RoleCode.ASSISTANT, tg=tg + 3)
        head = await person(session, org, "Начальник финансов", RoleCode.DEPT_HEAD, finance, tg=tg + 4)
        worker = await person(session, org, "Исполнитель", RoleCode.EMPLOYEE, finance, tg=tg + 5)
        stranger = await person(session, org, "Из другого отдела", RoleCode.EMPLOYEE, projects, tg=tg + 6)
        nobody = await person(session, org, "Без отдела", RoleCode.EMPLOYEE, tg=tg + 7)
        outsider = await person(session, other_org, "Чужой организации", RoleCode.EMPLOYEE, tg=tg + 8)
        finance.head_user_id = head.id
        await session.flush()

        print("\n1. Область права при создании поручения")
        emp_grants = await load_grants(session, worker)
        head_grants = await load_grants(session, head)
        chief_grants = await load_grants(session, chief)

        check(
            not await can_access_object(
                session, worker, emp_grants, "task.create",
                owner_id=chief.id, department_id=chief.department_id,
            ),
            "сотрудник НЕ может поручить руководителю",
        )
        check(
            await can_access_object(
                session, worker, emp_grants, "task.create",
                owner_id=worker.id, department_id=worker.department_id,
            ),
            "сотрудник может создать поручение себе",
        )
        check(
            await can_access_object(
                session, head, head_grants, "task.create",
                owner_id=worker.id, department_id=finance.id,
            ),
            "начальник отдела может поручить своему сотруднику",
        )
        check(
            not await can_access_object(
                session, head, head_grants, "task.create",
                owner_id=stranger.id, department_id=projects.id,
            ),
            "начальник отдела НЕ может поручить в чужой отдел",
        )
        check(
            await can_access_object(
                session, chief, chief_grants, "task.create",
                owner_id=stranger.id, department_id=projects.id,
            ),
            "руководитель может поручить кому угодно в организации",
        )

        print("\n2. Человек без отдела")
        nobody_grants = await load_grants(session, nobody)
        check(
            not await can_access_object(
                session, nobody, nobody_grants, "task.read", owner_id=worker.id
            ),
            "сотрудник без отдела не видит чужие поручения",
        )
        check(
            not await can_access_object(
                session, head, head_grants, "task.read", owner_id=nobody.id, department_id=None
            ),
            "поручение без отдела не попадает в чужую область по умолчанию",
        )

        print("\n3. Правило первого администратора")
        check(not await is_empty_organization(session, org.id), "организация с людьми не считается пустой")
        check(await is_empty_organization(session, other_org.id) is False, "и соседняя тоже — в ней есть человек")

        empty = Organization(name=f"{TEST_ORG_PREFIX}Пустая", timezone="Asia/Tashkent")
        session.add(empty)
        await session.flush()
        founder = await start_registration(
            session, organization=empty, telegram_user_id=tg + 20,
            telegram_username="founder", full_name="Основатель",
            department_id=None, requested_role=RoleCode.EXECUTIVE, invite=None,
        )
        founder_roles = await user_role_codes(session, founder)
        check(RoleCode.ADMIN in founder_roles, "первый в пустой системе получает права администратора")
        check(
            RoleCode.EXECUTIVE not in founder_roles,
            "но роль руководителя себе не выдаёт",
            f"роли: {sorted(str(r) for r in founder_roles)}",
        )

        # Приостанавливаем единственного администратора: дверь не должна открыться снова.
        founder.status = UserStatus.SUSPENDED
        await session.flush()
        second = await start_registration(
            session, organization=empty, telegram_user_id=tg + 21,
            telegram_username="second", full_name="Второй",
            department_id=None, requested_role=RoleCode.EXECUTIVE, invite=None,
        )
        check(
            second.status == UserStatus.PENDING,
            "после приостановки администратора правило НЕ срабатывает повторно",
            f"статус: {second.status}",
        )

        print("\n4. Изменение роли заменяет, а не добавляет")
        applicant = await start_registration(
            session, organization=org, telegram_user_id=tg + 22,
            telegram_username="applicant", full_name="Заявитель",
            department_id=finance.id, requested_role=RoleCode.EMPLOYEE, invite=None,
        )
        await approve_user(session, user=applicant, role=RoleCode.EMPLOYEE, approved_by=admin.id)
        await approve_user(session, user=applicant, role=RoleCode.DEPT_HEAD, approved_by=admin.id)
        roles_now = await user_role_codes(session, applicant)
        check(roles_now == {RoleCode.DEPT_HEAD}, "осталась одна роль, выбранная последней", f"роли: {roles_now}")

        print("\n5. Просроченная роль не действует")
        temp = await person(session, org, "Временный", RoleCode.EMPLOYEE, finance, tg=tg + 23)
        link = (
            await session.execute(
                select(UserRole).join(Role, Role.id == UserRole.role_id)
                .where(UserRole.user_id == temp.id)
            )
        ).scalar_one()
        link.valid_to = utcnow() - timedelta(days=1)
        await session.flush()
        check(not await user_role_codes(session, temp), "истёкшая роль не попадает в список ролей")
        check(not await load_grants(session, temp), "и прав не даёт")

        print("\n6. Ключ уведомления версионируется продлением срока")
        task = await service.create_task(
            session, creator=head, assignee=worker, title="Задача с продлением",
            due_at=utcnow() - timedelta(hours=2), priority=Priority.NORMAL,
        )
        await deadlines.process(session, organization_id=org.id)
        check(task.status == TaskStatus.OVERDUE, "первая просрочка отмечена")
        check(
            await session.scalar(
                select(func.count(Notification.id)).where(
                    Notification.event_key == f"task:{task.id}:overdue:0"
                )
            ) == 1,
            "уведомление о первой просрочке поставлено",
        )

        extension = await service.request_extension(
            session, task, worker, utcnow() + timedelta(days=2), "нужно время"
        )
        await service.decide_extension(session, extension, task, head, approved=True)
        task.due_at = utcnow() - timedelta(hours=1)
        task.status = TaskStatus.IN_PROGRESS
        await session.flush()
        await deadlines.process(session, organization_id=org.id)
        check(
            await session.scalar(
                select(func.count(Notification.id)).where(
                    Notification.event_key == f"task:{task.id}:overdue:1"
                )
            ) == 1,
            "после продления повторная просрочка уведомляет снова",
        )

        print("\n7. Планировщик не пишет впустую")
        before_rows = await session.scalar(select(func.count(Notification.id)))
        stats = await deadlines.process(session, organization_id=org.id)
        after_rows = await session.scalar(select(func.count(Notification.id)))
        check(
            stats == {"reminded": 0, "overdue": 0, "escalated": 0},
            "повторный проход ничего не делает",
            f"счётчики: {stats}",
        )
        check(before_rows == after_rows, "и ни одной строки в очередь не добавляет")

        print("\n8. Ступени эскалации проходятся по одному разу")
        task.due_at = utcnow() - timedelta(days=1, hours=1)
        await session.flush()
        await deadlines.process(session, organization_id=org.id)
        check(task.escalation_level == deadlines.LEVEL_DEPT_HEAD, "ступень поднялась до начальника отдела")
        check(
            await session.scalar(
                select(func.count(Notification.id)).where(
                    Notification.user_id == head.id,
                    Notification.event_key == f"task:{task.id}:esc1:1",
                )
            ) == 1,
            "начальник отдела уведомлён один раз",
        )
        await deadlines.process(session, organization_id=org.id)
        check(task.escalation_level == deadlines.LEVEL_DEPT_HEAD, "повторный проход ступень не меняет")

        print("\n9. Откат при сбое доставки")
        await enqueue(
            session, user_id=worker.id, organization_id=org.id, event_key="hard:retry",
            kind="test", body="Проверка отката", priority=NotificationPriority.NORMAL,
        )
        item = (
            await session.execute(select(Notification).where(Notification.event_key == "hard:retry"))
        ).scalar_one()
        await mark_failed(session, [item.id], "Bad Gateway")
        await session.refresh(item)
        check(item.attempts == 1, "попытка засчитана")
        check(item.status == NotificationStatus.PENDING, "уведомление осталось в очереди")
        check(item.next_attempt_at is not None and item.next_attempt_at > utcnow(),
              "следующая попытка отложена, а не сразу")
        ready = await pending_for_delivery(session, organization_id=org.id)
        check(
            all(n.id != item.id for n in ready),
            "до окончания паузы уведомление не берётся в работу",
        )

        print("\n10. Ошибка, которую повтор не лечит")
        await enqueue(
            session, user_id=worker.id, organization_id=org.id, event_key="hard:blocked",
            kind="test", body="Заблокировал бота", priority=NotificationPriority.NORMAL,
        )
        blocked = (
            await session.execute(select(Notification).where(Notification.event_key == "hard:blocked"))
        ).scalar_one()
        await mark_failed(session, [blocked.id], "Forbidden: bot was blocked by the user")
        await session.refresh(blocked)
        check(blocked.status == NotificationStatus.FAILED, "помечено сразу, без восьми бесполезных попыток")
        check(blocked.attempts == 0, "попытки на это не тратились")

        print("\n11. Ограничение частоты от Telegram")
        await enqueue(
            session, user_id=worker.id, organization_id=org.id, event_key="hard:flood",
            kind="test", body="Слишком часто", priority=NotificationPriority.NORMAL,
        )
        flood = (
            await session.execute(select(Notification).where(Notification.event_key == "hard:flood"))
        ).scalar_one()
        await mark_failed(session, [flood.id], "Too Many Requests", retry_after=30)
        await session.refresh(flood)
        check(flood.attempts == 0, "пауза по требованию Telegram попыткой не считается")
        check(flood.next_attempt_at is not None, "и откладывает доставку")

        print("\n12. Большая пачка режется на сообщения")
        many = [
            Notification(
                id=10_000 + i, user_id=worker.id, organization_id=org.id,
                event_key=f"grp{i}", kind="test", priority=NotificationPriority.NORMAL,
                body=f"Событие {i}", status=NotificationStatus.PENDING,
                scheduled_at=utcnow(), created_at=utcnow(),
            )
            for i in range(40)
        ]
        groups = group_messages(many)
        check(len(groups) >= 3, f"сорок уведомлений разошлись на {len(groups)} сообщения")
        check(
            all(len(text) <= 4096 for _, text in groups),
            "ни одно не превышает предел Telegram",
            f"максимум: {max(len(t) for _, t in groups)}",
        )
        check(
            all(len(ids) <= GROUP_MAX_ITEMS for ids, _ in groups),
            "в одно сообщение не собирается больше допустимого",
        )
        covered = sum(len(ids) for ids, _ in groups)
        check(covered == 40, "ни одно уведомление не потеряно при делении", f"вошло: {covered}")

        print("\n13. Критичное доставляется первым")
        for key, priority in (
            ("hard:ord:low", NotificationPriority.LOW),
            ("hard:ord:normal", NotificationPriority.NORMAL),
            ("hard:ord:critical", NotificationPriority.CRITICAL),
        ):
            await enqueue(
                session, user_id=stranger.id, organization_id=org.id, event_key=key,
                kind="test", body=f"Проверка {key}", priority=priority,
            )
        queue = await pending_for_delivery(session, organization_id=org.id)
        mine = [n for n in queue if n.event_key.startswith("hard:ord:")]
        check(
            mine and mine[0].priority == NotificationPriority.CRITICAL,
            "критичное стоит первым в очереди",
            f"порядок: {[n.priority for n in mine]}",
        )

        print("\n14. Очередь ограничена своей организацией")
        await enqueue(
            session, user_id=outsider.id, organization_id=other_org.id,
            event_key="hard:foreign", kind="test", body="Чужой организации",
            priority=NotificationPriority.NORMAL,
        )
        ours = await pending_for_delivery(session, organization_id=org.id)
        check(
            all(n.event_key != "hard:foreign" for n in ours),
            "уведомление чужой организации не попадает в нашу выборку",
        )

        print("\n15. Журнал знает свою организацию")
        entry = (
            await session.execute(
                select(AuditLog).where(AuditLog.actor_id == head.id).limit(1)
            )
        ).scalar_one_or_none()
        check(
            entry is not None and entry.organization_id == org.id,
            "запись журнала помечена организацией автора",
        )

    print("\n16. Один открытый запрос на продление — правило в схеме")
    async with session_scope() as session:
        task_id = (
            await session.execute(
                select(Task.id).join(Organization, Organization.id == Task.organization_id)
                .where(Organization.name == f"{TEST_ORG_PREFIX}Укрепление").limit(1)
            )
        ).scalar_one()
        requester = (
            await session.execute(
                select(User.id).join(Organization, Organization.id == User.organization_id)
                .where(Organization.name == f"{TEST_ORG_PREFIX}Укрепление").limit(1)
            )
        ).scalar_one()
        session.add_all([
            TaskExtension(
                task_id=task_id, requested_by=requester, new_due_at=utcnow() + timedelta(days=1),
                reason="первый", status=ExtensionStatus.NEW, created_at=utcnow(),
            ),
            TaskExtension(
                task_id=task_id, requested_by=requester, new_due_at=utcnow() + timedelta(days=2),
                reason="второй", status=ExtensionStatus.NEW, created_at=utcnow(),
            ),
        ])
        try:
            await session.flush()
            check(False, "база отвергает второй открытый запрос на продление", "вставились оба")
        except IntegrityError:
            check(True, "база отвергает второй открытый запрос на продление")
            await session.rollback()

    print("\n17. Обработчики не берут настоящего бота напрямую")
    # Это не придирка к стилю. Обработчик, импортирующий app.bot.loader.bot,
    # держит боевой токен и шлёт настоящие сообщения даже из проверочного
    # прогона с подменённой сетью. Однажды так и случилось: тест отправил
    # владельцу пять уведомлений о вымышленном сотруднике.
    handlers_dir = Path(__file__).resolve().parents[1] / "app" / "bot" / "handlers"
    offenders = [
        path.name
        for path in handlers_dir.glob("*.py")
        if "from app.bot.loader import bot" in path.read_text(encoding="utf-8")
    ]
    check(
        not offenders,
        "ни один обработчик не импортирует глобального бота",
        f"нарушители: {offenders}",
    )

    print("\n18. Журнал ошибок и состояние системы")
    async with session_scope() as session:
        before = await session.scalar(select(func.count(ErrorLog.id)))

    await record_error(
        ValueError("проверочная ошибка блока укрепления"),
        source="test",
        context="проверка журнала",
        telegram_user_id=930_000_999,
    )

    async with session_scope() as session:
        after = await session.scalar(select(func.count(ErrorLog.id)))
        latest = (
            await session.execute(
                select(ErrorLog).order_by(ErrorLog.id.desc()).limit(1)
            )
        ).scalar_one()
    check(after == before + 1, "ошибка записана в журнал")
    check(latest.kind == "ValueError", "тип ошибки сохранён")
    check(latest.details and "ValueError" in latest.details, "сохранена и трассировка")
    check(latest.context == "проверка журнала", "сохранено, что человек делал")

    status = await collect()
    check(status.numbers["errors_day"] >= 1, "ошибка попала в показатели состояния")
    check(
        any(e["kind"] == "ValueError" for e in status.errors),
        "и в список последних ошибок",
    )
    # Список перечислен явно, а не взят из кода: иначе новый фоновый цикл,
    # забытый на странице состояния, прошёл бы незамеченным.
    check(
        set(status.services) == {
            "bot", "worker:delivery", "worker:deadlines",
            "worker:meetings", "worker:digest", "worker:documents",
        },
        "состояние следит за всеми шестью службами",
        f"следит за: {sorted(status.services)}",
    )

    page = render(status, utcnow())
    check("<title>SETA" in page and "Состояние системы" in page, "страница состояния собирается")
    check(
        "проверочная ошибка" in page,
        "и показывает последнюю ошибку человеку",
    )

    async with session_scope() as session:
        await session.execute(delete(ErrorLog).where(ErrorLog.source == "test"))

    print("\n19. Уборка не трогает боевые данные")
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
        left = await session.scalar(
            select(func.count(Organization.id)).where(
                Organization.name.like(f"{TEST_ORG_PREFIX}%")
            )
        )
    check(real_after == real_before, "настоящие сотрудники не удалены", f"{real_before} → {real_after}")
    check(left == 0, "тестовые организации убраны")

    print(f"\n{'=' * 50}\nПройдено: {passed}   Ошибок: {failed}\n{'=' * 50}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
