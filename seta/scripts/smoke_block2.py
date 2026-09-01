"""Проверка блока 2: поручения, контроль, сроки, уведомления.

Изоляция та же, что в блоке 1: все данные создаются в организации «ТЕСТ ...»
и удаляются строго по organization_id. Боевые записи скрипт не трогает.

Время для проверки сроков подставляется явно (deadlines.process(now=...)),
поэтому ждать двое суток не нужно.

    docker compose -f docker-compose.yml -f docker-compose.dev.yml \
        run --rm --no-deps migrate python scripts/smoke_block2.py
"""
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, func, select

from app.core.dates import parse_due
from app.core.db import session_scope
from app.core.timeutil import utcnow
from app.models import (
    AuditLog,
    Department,
    Notification,
    Organization,
    Priority,
    RoleCode,
    Task,
    TaskComment,
    TaskEvent,
    TaskExtension,
    TaskStatus,
    TaskTemplate,
    User,
    UserRole,
    UserStatus,
    WorkingHours,
)
from app.models.enums import NotificationPriority, NotificationStatus
from app.services import deadlines
from app.services import tasks as service
from app.services.bootstrap import bootstrap, ensure_default_working_hours, grant_role
from app.services.notifications import deliver_pending, enqueue, group_messages
from app.services.rbac import load_grants

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
        task_ids = [
            row[0]
            for row in (
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
            await session.execute(delete(TaskTemplate).where(TaskTemplate.organization_id.in_(org_ids)))
            await session.execute(delete(User).where(User.id.in_(user_ids)))
        await session.execute(delete(Department).where(Department.organization_id.in_(org_ids)))
        await session.execute(delete(Organization).where(Organization.id.in_(org_ids)))


async def make_person(session, org, name, role, department=None, tg=0) -> User:
    person = User(
        organization_id=org.id,
        telegram_user_id=tg,
        full_name=name,
        status=UserStatus.ACTIVE,
        department_id=department.id if department else None,
        timezone="Asia/Tashkent",
        locale="ru",
    )
    session.add(person)
    await session.flush()
    await ensure_default_working_hours(session, person)
    await grant_role(session, person, role)
    return person


async def main() -> None:
    await cleanup()
    base_tg = 910_000_000

    async with session_scope() as session:
        await bootstrap(session)
        org = Organization(name=f"{TEST_ORG_PREFIX}Поручения", timezone="Asia/Tashkent")
        session.add(org)
        await session.flush()

        finance = Department(organization_id=org.id, name="ТЕСТ Финансы")
        projects = Department(organization_id=org.id, name="ТЕСТ Проекты")
        session.add_all([finance, projects])
        await session.flush()

        chief = await make_person(session, org, "Рахимов Руководитель", RoleCode.EXECUTIVE, tg=base_tg + 1)
        assistant = await make_person(session, org, "Каримова Ассистент", RoleCode.ASSISTANT, tg=base_tg + 2)
        head = await make_person(session, org, "Петров Начальник", RoleCode.DEPT_HEAD, finance, tg=base_tg + 3)
        worker = await make_person(session, org, "Иванов Исполнитель", RoleCode.EMPLOYEE, finance, tg=base_tg + 4)
        outsider = await make_person(session, org, "Сидоров Чужой", RoleCode.EMPLOYEE, projects, tg=base_tg + 5)
        finance.head_user_id = head.id
        await session.flush()

        print("\n1. Создание поручения")
        task = await service.create_task(
            session, creator=head, assignee=worker,
            title="Подготовить финансовый расчёт",
            due_at=parse_due("через 5 дней", worker.timezone),
            priority=Priority.NORMAL,
        )
        check(task.status == TaskStatus.NEW, "поручение создано в статусе «Новое»")
        check(not task.requires_review, "для обычного приоритета проверка не требуется")
        check(task.department_id == finance.id, "отдел взят от исполнителя")

        notified = await session.scalar(
            select(func.count(Notification.id)).where(
                Notification.user_id == worker.id, Notification.kind == "task.assigned"
            )
        )
        check(notified == 1, "исполнителю поставлено уведомление")

        important = await service.create_task(
            session, creator=head, assignee=worker,
            title="Срочно подготовить справку", priority=Priority.HIGH,
            due_at=parse_due("завтра", worker.timezone),
        )
        check(important.requires_review, "для высокого приоритета проверка включается сама")
        check(important.reviewer_id == head.id, "проверяющий по умолчанию — автор поручения")

        from_chief = await service.create_task(
            session, creator=chief, assignee=worker,
            title="Поручение руководителя", priority=Priority.CRITICAL,
        )
        check(
            from_chief.reviewer_id == assistant.id,
            "проверка от руководителя делегирована ассистенту",
            f"получен reviewer_id={from_chief.reviewer_id}",
        )

        print("\n2. Права на уровне записи")
        worker_grants = await load_grants(session, worker)
        outsider_grants = await load_grants(session, outsider)
        head_grants = await load_grants(session, head)
        chief_grants = await load_grants(session, chief)

        check((await service.access_for(session, task, worker, worker_grants)).can_view,
              "исполнитель видит своё поручение")
        check(not (await service.access_for(session, task, outsider, outsider_grants)).can_view,
              "сотрудник другого отдела НЕ видит поручение")
        check((await service.access_for(session, task, head, head_grants)).can_view,
              "начальник отдела видит поручение своего сотрудника")
        check((await service.access_for(session, task, chief, chief_grants)).can_view,
              "руководитель видит поручение по всей организации")

        access = await service.access_for(session, task, worker, worker_grants)
        check(access.can_accept and not access.can_review, "исполнителю доступно «Принять», но не проверка")
        check(
            not (await service.access_for(session, task, outsider, outsider_grants)).can_comment,
            "чужой не может даже комментировать",
        )

        print("\n3. Полный путь поручения")
        await service.accept(session, task, worker)
        check(task.status == TaskStatus.ACKNOWLEDGED, "принято исполнителем")
        await service.start(session, task, worker)
        check(task.status == TaskStatus.IN_PROGRESS, "взято в работу")
        result = await service.submit(session, task, worker, comment="Готово")
        check(result == TaskStatus.DONE and task.status == TaskStatus.DONE,
              "без проверки поручение закрывается сразу")

        print("\n4. Контроль качества: возврат на доработку")
        await service.accept(session, important, worker)
        await service.submit(session, important, worker, comment="Сделал")
        check(important.status == TaskStatus.REVIEW, "с проверкой уходит на проверку, а не в «Выполнено»")

        try:
            await service.return_for_rework(session, important, head, "   ")
            check(False, "пустой комментарий при возврате отклоняется")
        except service.TaskError:
            check(True, "пустой комментарий при возврате отклоняется")

        await service.return_for_rework(session, important, head, "Нет расчёта по второму кварталу")
        check(important.status == TaskStatus.IN_PROGRESS, "возвращено в работу")
        check(important.rework_count == 1, "возврат посчитан")

        await service.submit(session, important, worker)
        await service.approve(session, important, head, comment="Принято")
        check(important.status == TaskStatus.DONE, "после доработки работа принята")
        check(important.completed_at is not None, "проставлено время завершения")

        print("\n5. Продление срока")
        long_task = await service.create_task(
            session, creator=head, assignee=worker, title="Задача с переносом",
            due_at=parse_due("завтра", worker.timezone), priority=Priority.NORMAL,
        )
        old_due = long_task.due_at
        extension = await service.request_extension(
            session, long_task, worker,
            old_due + timedelta(days=3), "Жду данные от подрядчика",
        )
        check(extension.status == "NEW", "запрос на продление создан")
        check(long_task.due_at == old_due, "до решения автора срок не меняется")

        try:
            await service.request_extension(session, long_task, worker, old_due - timedelta(days=1), "назад")
            check(False, "перенос назад запрещён")
        except service.TaskError:
            check(True, "перенос назад запрещён")

        await service.decide_extension(session, extension, long_task, head, approved=True)
        check(long_task.due_at == old_due + timedelta(days=3), "срок продлён")
        check(long_task.extensions_count == 1, "продление посчитано — видно, кто двигает сроки")

        print("\n6. Комментарии")
        await service.add_comment(session, long_task, worker, text="Подрядчик обещал к среде")
        comments = await session.scalar(
            select(func.count(TaskComment.id)).where(TaskComment.task_id == long_task.id)
        )
        check(comments == 1, "комментарий сохранён в поручении")
        check(
            await session.scalar(
                select(func.count(Notification.id)).where(
                    Notification.user_id == head.id, Notification.kind == "task.comment"
                )
            ) == 1,
            "автор уведомлён о комментарии",
        )

        print("\n7. Сроки: напоминания и просрочка")
        watched = await service.create_task(
            session, creator=head, assignee=worker, title="Задача со сроком",
            due_at=utcnow() + timedelta(hours=47, minutes=30), priority=Priority.NORMAL,
        )
        stats = await deadlines.process(session, organization_id=org.id)
        check(stats["reminded"] >= 1, "напоминание за 48 часов поставлено")

        repeat = await deadlines.process(session, organization_id=org.id)
        check(repeat["reminded"] == 0, "повторный проход не создаёт дублей")

        watched.due_at = utcnow() - timedelta(hours=2)
        await session.flush()
        stats = await deadlines.process(session, organization_id=org.id)
        check(watched.status == TaskStatus.OVERDUE, "после срока статус стал «Просрочено»")
        check(
            await session.scalar(
                select(func.count(Notification.id)).where(
                    Notification.event_key == f"task:{watched.id}:overdue:0"
                )
            ) == 1,
            "исполнителю сообщено о просрочке",
        )

        print("\n8. Эскалация фильтрует, а не транслирует")
        watched.due_at = utcnow() - timedelta(days=1, hours=1)
        await session.flush()
        await deadlines.process(session, organization_id=org.id)
        check(
            await session.scalar(
                select(func.count(Notification.id)).where(
                    Notification.user_id == head.id,
                    Notification.event_key == f"task:{watched.id}:esc1:0",
                )
            ) == 1,
            "через день просрочка ушла начальнику отдела",
        )

        watched.due_at = utcnow() - timedelta(days=3, hours=1)
        await session.flush()
        await deadlines.process(session, organization_id=org.id)
        check(
            await session.scalar(
                select(func.count(Notification.id)).where(
                    Notification.user_id == assistant.id,
                    Notification.event_key == f"task:{watched.id}:esc3:0",
                )
            ) == 1,
            "через три дня подключился ассистент",
        )
        check(
            await session.scalar(
                select(func.count(Notification.id)).where(
                    Notification.user_id == chief.id,
                    Notification.kind == "task.escalation",
                )
            ) == 0,
            "руководителю поштучно НЕ приходит — только в сводке",
        )

        print("\n9. Личный контроль руководителя — исключение из правила")
        personal = await service.create_task(
            session, creator=chief, assignee=worker, title="Поручение на личном контроле",
            due_at=utcnow() - timedelta(hours=1), priority=Priority.HIGH,
            personal_control=True,
        )
        await deadlines.process(session, organization_id=org.id)
        pc = (
            await session.execute(
                select(Notification).where(
                    Notification.user_id == chief.id,
                    Notification.event_key == f"task:{personal.id}:pc:0",
                )
            )
        ).scalar_one_or_none()
        check(pc is not None, "о просрочке на личном контроле руководитель узнаёт сразу")
        check(
            pc is not None and pc.priority == NotificationPriority.CRITICAL,
            "такое уведомление критичное — тихие часы его не задержат",
        )

        print("\n10. Уведомления: дубли, группировка, доставка")
        first = await enqueue(
            session, user_id=worker.id, event_key="test:dup", kind="test",
            body="Первое", priority=NotificationPriority.NORMAL,
        )
        second = await enqueue(
            session, user_id=worker.id, event_key="test:dup", kind="test",
            body="Второе", priority=NotificationPriority.NORMAL,
        )
        check(first and not second, "повторная постановка того же события отклонена")

        sample = [
            Notification(user_id=worker.id, event_key=f"g{i}", kind="test",
                         priority=NotificationPriority.NORMAL, body=f"Событие {i}",
                         status=NotificationStatus.PENDING, scheduled_at=utcnow(),
                         created_at=utcnow())
            for i in range(4)
        ]
        for item in sample:
            item.id = 1000 + sample.index(item)
        grouped = group_messages(sample)
        check(len(grouped) == 1, "четыре обычных уведомления объединяются в одно")
        check("Обновления по вашим поручениям: 4" in grouped[0][1], "в сводке указано их количество")

        critical = Notification(
            id=2000, user_id=worker.id, event_key="c1", kind="test",
            priority=NotificationPriority.CRITICAL, body="Критично",
            status=NotificationStatus.PENDING, scheduled_at=utcnow(), created_at=utcnow(),
        )
        grouped = group_messages(sample + [critical])
        check(len(grouped) == 2, "критичное уведомление идёт отдельным сообщением")

        sent_to: list[tuple[int, str]] = []

        async def fake_send(telegram_id: int, text: str) -> None:
            sent_to.append((telegram_id, text))

        delivered = await deliver_pending(session, fake_send, organization_id=org.id)
        check(delivered > 0, f"уведомления доставлены ({delivered})")
        test_recipients = {
            row[0]
            for row in (
                await session.execute(
                    select(User.telegram_user_id).where(User.organization_id == org.id)
                )
            ).all()
        }
        check(
            all(tg in test_recipients for tg, _ in sent_to),
            "сообщения ушли только сотрудникам тестовой организации",
            f"посторонние получатели: {[tg for tg, _ in sent_to if tg not in test_recipients]}",
        )
        left = await session.scalar(
            select(func.count(Notification.id)).where(
                Notification.status == NotificationStatus.PENDING,
                Notification.user_id.in_([worker.id, head.id, chief.id, assistant.id]),
                Notification.scheduled_at <= utcnow(),
            )
        )
        check(left == 0, "очередь разобрана")

        print("\n11. Сводка контроля")
        counters = await service.control_counters(session, head, scope_all=False)
        check(counters["overdue"] >= 1, "в контроле видны просроченные")
        check(counters["done"] >= 2, "в контроле видны выполненные")

        buckets = await service.my_tasks(session, worker, bucket="active")
        check(all(t.status in service.ACTIVE_STATUSES for t in buckets), "фильтр «активные» отбирает верно")
        done_list = await service.my_tasks(session, worker, bucket="done")
        check(all(t.status == TaskStatus.DONE for t in done_list), "фильтр «выполненные» отбирает верно")

        print("\n12. История поручения")
        events = (
            await session.execute(
                select(TaskEvent.kind).where(TaskEvent.task_id == important.id).order_by(TaskEvent.id)
            )
        ).scalars().all()
        check("CREATED" in events and "RETURNED" in events and "APPROVED" in events,
              "история содержит создание, возврат и приёмку",
              f"события: {list(events)}")

    print("\n13. Уборка не трогает боевые данные")
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
        tasks_left = await session.scalar(
            select(func.count(Task.id)).where(
                Task.organization_id.in_(
                    select(Organization.id).where(
                        Organization.name.like(f"{TEST_ORG_PREFIX}%")
                    )
                )
            )
        )
    check(real_after == real_before, "настоящие сотрудники не удалены", f"{real_before} → {real_after}")
    check(tasks_left == 0, "тестовые поручения убраны")

    print(f"\n{'=' * 46}\nПройдено: {passed}   Ошибок: {failed}\n{'=' * 46}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
