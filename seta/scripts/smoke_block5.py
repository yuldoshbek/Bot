"""Проверка блока 5, фаза 1: пятнадцать показателей.

Показателю нельзя верить на слово: «загрузка календаря 23%» выглядит убедительно
при любой ошибке в формуле. Поэтому здесь на каждый показатель заводятся данные,
у которых ответ известен заранее и посчитан руками в комментарии, — и проверка
сверяет число, а не факт «что-то вернулось».

Вторая половина проверок — про отсутствие данных. «Пунктуальность 0%» без
отметок явки читается как «все опаздывают», хотя означает «никто не отмечался».
Каждый показатель обязан отличать одно от другого.

    docker compose -f docker-compose.yml -f docker-compose.dev.yml \
        run --rm --no-deps migrate python scripts/smoke_block5.py
"""
import asyncio
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, func, select

from app.core.db import session_scope
from app.core.dates import parse_due
from app.core.timeutil import to_local, utcnow
from app.models import (
    Absence,
    AuditLog,
    CalendarBlock,
    Decision,
    Department,
    Holiday,
    Meeting,
    MeetingAttendance,
    MeetingParticipant,
    MeetingRating,
    MeetingRequest,
    MeetingStatus,
    Notification,
    Organization,
    SlotHold,
    Task,
    TaskEvent,
    TaskExtension,
    TaskStatus,
    TaskTemplate,
    TimeQuota,
    FeatureFlag,
    User,
    UserRole,
    UserStatus,
    WorkingHours,
)
from app.models.enums import (
    AbsenceKind,
    ExtensionStatus,
    ParticipantRole,
    Priority,
    RequestStatus,
    RoleCode,
    TaskEventKind,
)
from app.services import analytics, dashboard, digest, features, orgadmin, templates
from app.services import slots as slot_service
from app.services.bootstrap import bootstrap, ensure_default_working_hours, grant_role
from app.services.rbac import load_grants

TEST_ORG_PREFIX = "ТЕСТ "
ORG_NAME = f"{TEST_ORG_PREFIX}Показатели"
TZ = ZoneInfo("Asia/Tashkent")
# Опорный понедельник. Все ожидания отсчитываются от него.
MONDAY = date(2026, 11, 2)
NOW = datetime.combine(MONDAY + timedelta(days=14), time(9, 0), tzinfo=TZ)

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


def close_to(value, expected, tolerance=0.05) -> bool:
    """Сравнение чисел с плавающей точкой: доля процента роли не играет."""
    return value is not None and abs(float(value) - float(expected)) <= tolerance


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=TZ)


def find(metrics, key):
    return next(m for m in metrics if m.key == key)


def to_local_date_utc(moment: datetime) -> str:
    """Дата того же момента по серверу — для сверки с местной датой получателя."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d")


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
        meeting_ids = [
            row[0] for row in (
                await session.execute(
                    select(Meeting.id).where(Meeting.organization_id.in_(org_ids))
                )
            ).all()
        ]
        task_ids = [
            row[0] for row in (
                await session.execute(select(Task.id).where(Task.organization_id.in_(org_ids)))
            ).all()
        ]
        if meeting_ids:
            for model in (MeetingParticipant, MeetingAttendance, MeetingRating):
                await session.execute(delete(model).where(model.meeting_id.in_(meeting_ids)))
        if task_ids:
            for model in (TaskEvent, TaskExtension):
                await session.execute(delete(model).where(model.task_id.in_(task_ids)))
        if user_ids:
            await session.execute(delete(SlotHold).where(SlotHold.owner_id.in_(user_ids)))
            await session.execute(
                delete(MeetingRequest).where(MeetingRequest.owner_id.in_(user_ids))
            )
        await session.execute(
            delete(Notification).where(Notification.organization_id.in_(org_ids))
        )
        await session.execute(delete(Decision).where(Decision.organization_id.in_(org_ids)))
        await session.execute(
            delete(TaskTemplate).where(TaskTemplate.organization_id.in_(org_ids))
        )
        await session.execute(
            delete(FeatureFlag).where(FeatureFlag.organization_id.in_(org_ids))
        )
        await session.execute(
            delete(Holiday).where(Holiday.organization_id.in_(org_ids))
        )
        await session.execute(delete(Task).where(Task.organization_id.in_(org_ids)))
        if meeting_ids:
            await session.execute(delete(Meeting).where(Meeting.id.in_(meeting_ids)))
        if user_ids:
            for model in (UserRole, WorkingHours, Notification, CalendarBlock, Absence):
                await session.execute(delete(model).where(model.user_id.in_(user_ids)))
            await session.execute(delete(TimeQuota).where(TimeQuota.owner_id.in_(user_ids)))
            await session.execute(delete(AuditLog).where(AuditLog.actor_id.in_(user_ids)))
            await session.execute(delete(User).where(User.id.in_(user_ids)))
        await session.execute(delete(Department).where(Department.organization_id.in_(org_ids)))
        await session.execute(delete(Organization).where(Organization.id.in_(org_ids)))


async def person(session, org, name, role, tg, department_id=None) -> User:
    user = User(
        organization_id=org.id, telegram_user_id=tg, full_name=name,
        status=UserStatus.ACTIVE, timezone="Asia/Tashkent", locale="ru",
        department_id=department_id,
    )
    session.add(user)
    await session.flush()
    await ensure_default_working_hours(session, user)
    await grant_role(session, user, role)
    return user


async def meeting(session, owner, start, end, *, title="ТЕСТ встреча",
                  status=MeetingStatus.CONFIRMED, guests=()) -> Meeting:
    item = Meeting(
        organization_id=owner.organization_id, owner_id=owner.id, title=title,
        start_at=start, end_at=end, status=status, created_by=owner.id,
    )
    session.add(item)
    await session.flush()
    for guest in (owner, *guests):
        session.add(MeetingParticipant(
            meeting_id=item.id, user_id=guest.id,
            role=ParticipantRole.REQUIRED, created_at=start,
        ))
    await session.flush()
    return item


async def cast(session) -> dict[str, User]:
    org_id = (
        await session.execute(select(Organization.id).where(Organization.name == ORG_NAME))
    ).scalar_one()
    people = (
        await session.execute(select(User).where(User.organization_id == org_id))
    ).scalars().all()
    return {p.full_name.split()[-1].lower(): p for p in people}


async def main() -> None:
    await cleanup()
    tg = 960_000_000
    week = analytics.Period(since=at(MONDAY, 0), until=at(MONDAY + timedelta(days=21), 0))

    async with session_scope() as session:
        await bootstrap(session)
        org = Organization(name=ORG_NAME, timezone="Asia/Tashkent")
        session.add(org)
        await session.flush()

        finance = Department(organization_id=org.id, name="ТЕСТ Финансы")
        logistics = Department(organization_id=org.id, name="ТЕСТ Логистика")
        session.add_all([finance, logistics])
        await session.flush()

        chief = await person(session, org, "ТЕСТ Руководитель", RoleCode.EXECUTIVE, tg + 1)
        head = await person(
            session, org, "ТЕСТ Начальник", RoleCode.DEPT_HEAD, tg + 2, finance.id
        )
        worker = await person(
            session, org, "ТЕСТ Сотрудник", RoleCode.EMPLOYEE, tg + 3, finance.id
        )
        outsider = await person(
            session, org, "ТЕСТ Логист", RoleCode.EMPLOYEE, tg + 4, logistics.id
        )

        print("\n1. Область расчёта по правам")
        chief_view = await analytics.audience_for(
            session, viewer=chief, grants=await load_grants(session, chief)
        )
        head_view = await analytics.audience_for(
            session, viewer=head, grants=await load_grants(session, head)
        )
        worker_view = await analytics.audience_for(
            session, viewer=worker, grants=await load_grants(session, worker)
        )
        check(chief_view is not None and chief_view.everyone, "руководитель считает по организации")
        check(
            head_view is not None and not head_view.everyone
            and head_view.user_ids == {head.id, worker.id},
            "начальник отдела — только по своему отделу",
            f"{head_view.user_ids if head_view else None}",
        )
        check(worker_view is None, "рядовому сотруднику аналитика не открыта")
        check(
            chief_view.timezone == "Asia/Tashkent",
            "часовой пояс области взят у смотрящего",
            chief_view.timezone,
        )

        print("\n2. Пустая система: нет данных — это не ноль")
        empty = await analytics.all_metrics(session, audience=chief_view, period=week, now=NOW)
        check(len(empty) == 15, "показателей ровно пятнадцать", str(len(empty)))
        # Молчать обязан тот показатель, у которого нет знаменателя. У загрузки
        # календаря знаменатель есть и на пустой системе — рабочие часы заданы, —
        # поэтому её ноль настоящий: календарь действительно пуст.
        silent = [m for m in empty if m.no_data]
        check(
            len(silent) == 14,
            "молчат четырнадцать: делить не на что",
            f"заговорили: {[m.key for m in empty if not m.no_data]}",
        )
        load = find(empty, "calendar_load")
        check(
            load.value == 0.0,
            "а загрузка календаря показывает настоящий ноль: часы есть, встреч нет",
            f"{load.render()}",
        )
        check(
            all("нет данных" in m.render() for m in silent),
            "и каждый молчащий честно пишет «нет данных», а не 0",
        )

        # Тот же показатель без рабочих часов знаменателя лишается — и молчит.
        bare = User(
            organization_id=org.id, telegram_user_id=tg + 9, full_name="ТЕСТ Безчасов",
            status=UserStatus.ACTIVE, timezone="Asia/Tashkent", locale="ru",
        )
        session.add(bare)
        await session.flush()
        bare_load = await analytics.calendar_load(
            session,
            audience=analytics.Audience(organization_id=org.id, user_ids={bare.id}),
            period=week,
        )
        check(
            bare_load.no_data,
            "без заданных рабочих часов загрузка тоже не выдумывает ноль",
            bare_load.render(),
        )
        check(
            await analytics.all_metrics(
                session,
                audience=analytics.Audience(organization_id=org.id),
                period=week,
                now=NOW,
            ) == [],
            "пустая область не считает ничего",
        )


async def stage_two() -> None:
    """Данные с заранее известным ответом и сверка каждого показателя."""
    week = analytics.Period(since=at(MONDAY, 0), until=at(MONDAY + timedelta(days=21), 0))

    async with session_scope() as session:
        who = await cast(session)
        chief, head, worker, outsider = (
            who["руководитель"], who["начальник"], who["сотрудник"], who["логист"]
        )
        org_id = chief.organization_id

        print("\n3. Загрузка календаря и стоимость совещаний")
        # Три встречи руководителя: 60 + 30 + 90 = 180 минут.
        # Рабочая неделя по умолчанию: пять дней по (19:00−09:00) минус час обеда
        # = 5 × 540 = 2700 минут на человека. Считаем по четверым: 10 800.
        # За 21 день доступно 10 800 × 21 / 7 = 32 400. Итого 180/32400 = 0,56%.
        m1 = await meeting(session, chief, at(MONDAY, 10), at(MONDAY, 11), guests=[worker])
        await meeting(session, chief, at(MONDAY + timedelta(days=1), 10),
                      at(MONDAY + timedelta(days=1), 10, 30), guests=[worker, head])
        await meeting(session, chief, at(MONDAY + timedelta(days=2), 14),
                      at(MONDAY + timedelta(days=2), 15, 30), guests=[head])
        await session.flush()

        audience = await analytics.audience_for(
            session, viewer=chief, grants=await load_grants(session, chief)
        )
        load = await analytics.calendar_load(session, audience=audience, period=week)
        check(close_to(load.value, 0.6, 0.05), "загрузка календаря совпала с расчётом",
              f"получено {load.value}, ожидали ≈0,56")
        # Само значение округляется до десятых, и ошибка в знаменателе в нём
        # тонет: без вычета обеда вышло бы 0,5 против 0,56 — разница меньше шага
        # округления. Поэтому сверяется и знаменатель целиком.
        check(
            "180 мин из 32400" in load.detail,
            "и знаменатель — рабочие часы за вычетом обеда",
            load.detail,
        )

        cost = await analytics.meeting_cost(session, audience=audience, period=week)
        # Самая дорогая — третья: 90 минут × 2 человека = 3 человеко-часа.
        check(
            cost.rows and close_to(cost.rows[0][1], 3.0),
            "самое дорогое совещание посчитано в человеко-часах",
            f"{cost.rows[:2]}",
        )

        spenders = await analytics.time_spenders(session, audience=audience, period=week)
        # Сотрудник был на встречах 60 и 30 минут = 90; начальник на 30 и 90 = 120.
        top = dict(spenders.rows)
        check(
            close_to(top.get("ТЕСТ Начальник"), 120) and close_to(top.get("ТЕСТ Сотрудник"), 90),
            "минуты по инициаторам разошлись верно",
            f"{spenders.rows}",
        )

        print("\n4. Пунктуальность")
        empty_punctuality = await analytics.punctuality(
            session, audience=audience, period=week
        )
        check(empty_punctuality.no_data, "без отметок явки показателя нет, а не ноль")

        session.add(MeetingAttendance(
            meeting_id=m1.id, user_id=chief.id, present=True,
            checked_in_at=at(MONDAY, 10), late_minutes=0,
        ))
        session.add(MeetingAttendance(
            meeting_id=m1.id, user_id=worker.id, present=True,
            checked_in_at=at(MONDAY, 10, 7), late_minutes=7,
        ))
        # Отсутствовавший в пунктуальность не идёт: он не опоздал, его не было.
        # Отсутствие — отдельный факт, и смешивать их значило бы разбавлять
        # пунктуальность прогулами. Без этой строки фильтр присутствия
        # ничего не меняет и проверка проходит при любом исходе.
        session.add(MeetingAttendance(
            meeting_id=m1.id, user_id=head.id, present=False, late_minutes=0,
        ))
        await session.flush()
        punctual = await analytics.punctuality(session, audience=audience, period=week)
        check(close_to(punctual.value, 50.0), "одна отметка из двух с опозданием — 50%",
              f"{punctual.value}")
        check(
            "из 2" in punctual.detail,
            "и отсутствовавший в знаменатель не попал",
            punctual.detail,
        )


async def stage_three() -> None:
    """Поручения: дисциплина, реакция, возвраты, продления, тренд."""
    week = analytics.Period(since=at(MONDAY, 0), until=at(MONDAY + timedelta(days=21), 0))

    async with session_scope() as session:
        who = await cast(session)
        chief, head, worker = who["руководитель"], who["начальник"], who["сотрудник"]
        org_id = chief.organization_id
        department_id = worker.department_id

        def task(title, **kw) -> Task:
            item = Task(
                organization_id=org_id, title=title, creator_id=chief.id,
                assignee_id=worker.id, department_id=department_id,
                created_at=at(MONDAY, 9), **kw,
            )
            session.add(item)
            return item

        print("\n5. Дисциплина сроков")
        # Три завершённых со сроком: два вовремя, один с опозданием → 66,7%.
        task("ТЕСТ в срок раз", status=TaskStatus.DONE,
             due_at=at(MONDAY + timedelta(days=2), 18), completed_at=at(MONDAY + timedelta(days=1), 12))
        task("ТЕСТ в срок два", status=TaskStatus.DONE,
             due_at=at(MONDAY + timedelta(days=3), 18), completed_at=at(MONDAY + timedelta(days=3), 10))
        task("ТЕСТ опоздал", status=TaskStatus.DONE,
             due_at=at(MONDAY + timedelta(days=2), 18), completed_at=at(MONDAY + timedelta(days=5), 10))
        # Незавершённое в знаменатель идти не должно.
        task("ТЕСТ ещё в работе", status=TaskStatus.IN_PROGRESS,
             due_at=at(MONDAY + timedelta(days=9), 18))
        await session.flush()

        audience = await analytics.audience_for(
            session, viewer=chief, grants=await load_grants(session, chief)
        )
        discipline = await analytics.deadline_discipline(
            session, audience=audience, period=week
        )
        check(close_to(discipline.value, 66.7, 0.2),
              "два срока из трёх соблюдены — 66,7%", f"{discipline.value}")

        print("\n6. Скорость реакции")
        quick = task("ТЕСТ приняли быстро", status=TaskStatus.IN_PROGRESS,
                     accepted_at=at(MONDAY, 11))
        slow = task("ТЕСТ приняли долго", status=TaskStatus.IN_PROGRESS,
                    accepted_at=at(MONDAY, 14))
        await session.flush()
        # Приняты через 2 и 5 часов после создания в 09:00 → среднее 3,5.
        reaction = await analytics.reaction_time(session, audience=audience, period=week)
        check(close_to(reaction.value, 3.5, 0.1), "среднее время принятия — 3,5 часа",
              f"{reaction.value}")

        print("\n7. Возвраты на доработку")
        returned = task("ТЕСТ вернули", status=TaskStatus.IN_PROGRESS,
                        requires_review=True, submitted_at=at(MONDAY + timedelta(days=1), 10))
        accepted = task("ТЕСТ приняли с первого раза", status=TaskStatus.DONE,
                        requires_review=True, submitted_at=at(MONDAY + timedelta(days=1), 11),
                        due_at=at(MONDAY + timedelta(days=4), 18),
                        completed_at=at(MONDAY + timedelta(days=2), 10))
        # Отправлено, но проверки не требовало: возвращать было некуда, и в
        # знаменатель оно попадать не должно. Без этой строки проверка проходит
        # и с фильтром «только прошедшие проверку», и без него.
        task("ТЕСТ сдали без проверки", status=TaskStatus.DONE,
             requires_review=False, submitted_at=at(MONDAY + timedelta(days=1), 12),
             due_at=at(MONDAY + timedelta(days=4), 18),
             completed_at=at(MONDAY + timedelta(days=2), 11))
        await session.flush()
        session.add(TaskEvent(
            task_id=returned.id, actor_id=chief.id, kind=TaskEventKind.RETURNED,
            created_at=at(MONDAY + timedelta(days=1), 12),
        ))
        await session.flush()
        rework = await analytics.rework_rate(session, audience=audience, period=week)
        check(close_to(rework.value, 50.0), "один возврат из двух проверок — 50%",
              f"{rework.value}")

        print("\n8. Хронические переносы")
        moved = task("ТЕСТ двигали срок", status=TaskStatus.IN_PROGRESS,
                     due_at=at(MONDAY + timedelta(days=10), 18))
        await session.flush()
        for number, status in ((1, ExtensionStatus.APPROVED), (2, ExtensionStatus.APPROVED),
                               (3, ExtensionStatus.DECLINED)):
            session.add(TaskExtension(
                task_id=moved.id, requested_by=worker.id,
                new_due_at=at(MONDAY + timedelta(days=10 + number), 18),
                reason=f"ТЕСТ причина {number}", status=status,
                decided_by=chief.id, decided_at=at(MONDAY + timedelta(days=number), 12),
                created_at=at(MONDAY + timedelta(days=number), 10),
            ))
        await session.flush()
        extensions = await analytics.chronic_extensions(
            session, audience=audience, period=week
        )
        # Отклонённое продление ничего не сдвинуло и в счёт не идёт: 2, не 3.
        check(
            extensions.rows and close_to(extensions.rows[0][1], 2),
            "считаются только одобренные продления",
            f"{extensions.rows}",
        )


async def stage_four() -> None:
    """Встречи без результата, заявки, повторы, пробки, прогноз, границы прав."""
    week = analytics.Period(since=at(MONDAY, 0), until=at(MONDAY + timedelta(days=21), 0))

    async with session_scope() as session:
        who = await cast(session)
        chief, head, worker = who["руководитель"], who["начальник"], who["сотрудник"]
        audience = await analytics.audience_for(
            session, viewer=chief, grants=await load_grants(session, chief)
        )

        print("\n9. Встречи без результата")
        fruitful = await meeting(
            session, chief, at(MONDAY + timedelta(days=7), 10), at(MONDAY + timedelta(days=7), 11),
            title="ТЕСТ с итогом", status=MeetingStatus.FINISHED, guests=[worker],
        )
        await meeting(
            session, chief, at(MONDAY + timedelta(days=7), 12), at(MONDAY + timedelta(days=7), 13),
            title="ТЕСТ без итога", status=MeetingStatus.FINISHED, guests=[worker],
        )
        session.add(Decision(
            organization_id=chief.organization_id, meeting_id=fruitful.id,
            title="ТЕСТ решение со встречи", author_id=chief.id,
        ))
        await session.flush()
        fruitless = await analytics.fruitless_meetings(
            session, audience=audience, period=week
        )
        check(close_to(fruitless.value, 50.0), "одна завершённая из двух без итогов — 50%",
              f"{fruitless.value}")

        print("\n10. Скорость решений и ожидание окна")
        session.add(MeetingRequest(
            organization_id=chief.organization_id, initiator_id=worker.id, owner_id=chief.id,
            title="ТЕСТ заявка быстрая", duration_minutes=30,
            start_at=at(MONDAY + timedelta(days=9), 10), end_at=at(MONDAY + timedelta(days=9), 10, 30),
            status=RequestStatus.APPROVED, decided_by=chief.id,
            decided_at=at(MONDAY + timedelta(days=7), 11), created_at=at(MONDAY + timedelta(days=7), 9),
        ))
        session.add(MeetingRequest(
            organization_id=chief.organization_id, initiator_id=head.id, owner_id=chief.id,
            title="ТЕСТ заявка долгая", duration_minutes=30,
            start_at=at(MONDAY + timedelta(days=11), 10), end_at=at(MONDAY + timedelta(days=11), 10, 30),
            status=RequestStatus.APPROVED, decided_by=chief.id,
            decided_at=at(MONDAY + timedelta(days=7), 13), created_at=at(MONDAY + timedelta(days=7), 9),
        ))
        await session.flush()
        # Решения через 2 и 4 часа → среднее 3.
        speed = await analytics.decision_speed(session, audience=audience, period=week)
        check(close_to(speed.value, 3.0, 0.1), "среднее время ответа на заявку — 3 часа",
              f"{speed.value}")
        # Окна через 2 и 4 дня → среднее 3.
        lag = await analytics.availability_lag(session, audience=audience, period=week)
        check(close_to(lag.value, 3.0, 0.1), "среднее ожидание окна — 3 дня", f"{lag.value}")

        print("\n11. Повторяющиеся темы и пробки")
        repeat_day = MONDAY + timedelta(days=8)
        for number in range(4):
            await meeting(
                session, chief,
                at(repeat_day, 9 + number), at(repeat_day, 9 + number, 30),
                title="ТЕСТ Склад" if number % 2 == 0 else "тест склад",
                guests=[worker],
            )
        await session.flush()
        # Четыре встречи с одной темой в разном регистре — одна тема.
        topics = await analytics.repeating_topics(session, audience=audience, period=week)
        check(
            topics.rows and close_to(topics.rows[0][1], 4),
            "четыре встречи одной темы найдены несмотря на регистр",
            f"{topics.rows}",
        )
        jams = await analytics.schedule_jams(session, audience=audience, period=week)
        check(
            any(day == repeat_day.strftime("%d.%m") for day, _ in jams.rows),
            "день с четырьмя встречами отмечен как пробка",
            f"{jams.rows}",
        )

        print("\n12. Прогноз перегрузки смотрит вперёд")
        ahead = NOW + timedelta(days=2)
        await meeting(session, chief, at(ahead.date(), 10), at(ahead.date(), 11),
                      title="ТЕСТ впереди", guests=[worker])
        session.add(Task(
            organization_id=chief.organization_id, title="ТЕСТ срок впереди",
            creator_id=chief.id, assignee_id=worker.id, status=TaskStatus.IN_PROGRESS,
            priority=Priority.CRITICAL, due_at=ahead, created_at=at(MONDAY, 9),
        ))
        await session.flush()
        forecast = await analytics.overload_forecast(session, audience=audience, now=NOW)
        check(close_to(forecast.value, 2), "неделя впереди: одна встреча и один срок",
              f"{forecast.value}: {forecast.detail}")
        check("важных 1" in forecast.detail, "важный срок отмечен отдельно", forecast.detail)

        print("\n13. Тренд просрочек по отделам")
        trend = await analytics.overdue_trend(session, audience=audience, period=week)
        check(bool(trend.rows), "тренд посчитан по отделам", f"{trend.rows}")
        check(
            all(isinstance(name, str) for name, _ in trend.rows),
            "и назван отделами, а не их номерами",
        )

        print("\n14. Область прав меняет цифры")
        head_audience = await analytics.audience_for(
            session, viewer=head, grants=await load_grants(session, head)
        )
        chief_load = await analytics.calendar_load(session, audience=audience, period=week)
        head_load = await analytics.calendar_load(
            session, audience=head_audience, period=week
        )
        check(
            head_load.value != chief_load.value,
            "начальник отдела и руководитель видят разные числа",
            f"{head_load.value} против {chief_load.value}",
        )
        check(
            head_audience.user_ids and chief.id not in head_audience.user_ids,
            "руководитель не входит в область начальника отдела",
        )

        print("\n15. Главный экран — пять показателей из пятнадцати")
        head_line = await analytics.headline(
            session, audience=audience, period=week, now=NOW
        )
        check(len(head_line) == 5, "на главном экране пять показателей", str(len(head_line)))
        check(
            [m.key for m in head_line] == list(analytics.HEADLINE_KEYS),
            "и именно те, что названы в архитектуре",
            f"{[m.key for m in head_line]}",
        )
        full = await analytics.all_metrics(session, audience=audience, period=week, now=NOW)
        check(len(full) == 15, "полный набор — пятнадцать", str(len(full)))
        check(
            find(full, "calendar_load").value == find(head_line, "calendar_load").value,
            "главный экран и полный набор считают одно и то же",
        )


async def stage_five() -> None:
    """Ни одного запроса внутри цикла и уборка."""
    from sqlalchemy import event
    from app.core.db import engine

    week = analytics.Period(since=at(MONDAY, 0), until=at(MONDAY + timedelta(days=21), 0))

    async with session_scope() as session:
        who = await cast(session)
        chief = who["руководитель"]
        audience = await analytics.audience_for(
            session, viewer=chief, grants=await load_grants(session, chief)
        )

        print("\n16. Показатель считает база, а не Python")
        statements: list[str] = []

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", record)
        try:
            await analytics.all_metrics(session, audience=audience, period=week, now=NOW)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record)

        # Пятнадцать показателей: у большинства один запрос, у трёх — два
        # (значение плюс имена для рейтинга), у прогноза — четыре счётчика.
        # Предел с запасом; важно, что число не растёт с объёмом данных.
        check(
            len(statements) <= 30,
            "все пятнадцать укладываются в три десятка запросов",
            f"запросов: {len(statements)}",
        )
        selects = [q for q in statements if q.lstrip().upper().startswith("SELECT")]
        check(
            all("LIMIT" in q.upper() or "COUNT(" in q.upper() or "SUM(" in q.upper()
                or "AVG(" in q.upper() or "users" in q.lower()
                for q in selects),
            "ни один показатель не выгружает строки целиком",
            f"подозрительных: {sum(1 for q in selects if 'COUNT(' not in q.upper() and 'SUM(' not in q.upper() and 'AVG(' not in q.upper() and 'LIMIT' not in q.upper() and 'users' not in q.lower())}",
        )

    print("\n17. Экран руководителя: четыре вопроса в одном сообщении")
    async with session_scope() as session:
        who = await cast(session)
        chief, head, worker = who["руководитель"], who["начальник"], who["сотрудник"]
        grants = await load_grants(session, chief)

        board = await dashboard.build(session, viewer=chief, grants=grants, now=NOW)
        check(len(board.metrics) == 5, "на экране пять показателей", str(len(board.metrics)))
        check(board.overdue_total > 0, "просрочки посчитаны", str(board.overdue_total))
        # Сводка по отделам обязана сходиться с общим числом: иначе экрану
        # перестанут верить на второй же неделе.
        check(
            sum(count for _, count in board.overdue_by_department)
            + board.overdue_other == board.overdue_total,
            "сводка по отделам сходится с общим числом",
            f"{board.overdue_by_department} + {board.overdue_other}"
            f" против {board.overdue_total}",
        )
        check(
            board.to_review >= 0 and board.requests_waiting >= 0,
            "блоки «требует решения» посчитаны",
        )

        print("\n18. Личный контроль — исключение из правила сводки")
        marked = Task(
            organization_id=chief.organization_id, title="ТЕСТ на личном контроле",
            creator_id=chief.id, assignee_id=worker.id, status=TaskStatus.IN_PROGRESS,
            personal_control=True, due_at=at(MONDAY + timedelta(days=1), 18),
            created_at=at(MONDAY, 9),
        )
        session.add(marked)
        await session.flush()
        with_personal = await dashboard.build(
            session, viewer=chief, grants=grants, now=NOW
        )
        check(
            any(t.id == marked.id for t in with_personal.personal_overdue),
            "поручение на личном контроле показано поимённо",
            f"{[t.title for t in with_personal.personal_overdue]}",
        )
        ordinary = [
            t for t in with_personal.personal_overdue if not t.personal_control
        ]
        check(not ordinary, "а обычные просрочки поимённо не показываются")

        print("\n19. Поручение без отдела не теряется в сводке")
        homeless = Task(
            organization_id=chief.organization_id, title="ТЕСТ без отдела",
            creator_id=chief.id, assignee_id=chief.id, status=TaskStatus.IN_PROGRESS,
            department_id=None, due_at=at(MONDAY + timedelta(days=1), 18),
            created_at=at(MONDAY, 9),
        )
        session.add(homeless)
        await session.flush()
        wide = await dashboard.build(session, viewer=chief, grants=grants, now=NOW)
        check(
            sum(count for _, count in wide.overdue_by_department)
            + wide.overdue_other == wide.overdue_total,
            "сумма сходится и с поручением без отдела",
            f"{wide.overdue_by_department} + {wide.overdue_other}"
            f" против {wide.overdue_total}",
        )
        check(
            any(name == "вне отделов" for name, _ in wide.overdue_by_department),
            "и оно попало в строку «вне отделов»",
            f"{wide.overdue_by_department}",
        )

        print("\n20. Экран сотрудника и счёт запросов")
        worker_board = await dashboard.build(
            session, viewer=worker, grants=await load_grants(session, worker), now=NOW
        )
        check(not worker_board.metrics, "сотруднику показатели не считаются")
        check(
            worker_board.requests_waiting == 0 and worker_board.stale_decisions == 0,
            "и блоки руководителя ему не собираются",
        )

        from sqlalchemy import event
        from app.core.db import engine

        statements: list[str] = []

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", record)
        try:
            await dashboard.build(session, viewer=chief, grants=grants, now=NOW)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record)
        # Экран открывают несколько раз в день; он обязан оставаться дешёвым
        # и не расти вместе с числом поручений.
        check(
            len(statements) <= 40,
            "экран собирается ограниченным числом запросов",
            f"запросов: {len(statements)}",
        )

    print("\n21. Пустая организация: экран не врёт нулями")
    async with session_scope() as session:
        empty_org = Organization(name=f"{TEST_ORG_PREFIX}Пустая", timezone="Asia/Tashkent")
        session.add(empty_org)
        await session.flush()
        lonely = await person(
            session, empty_org, "ТЕСТ Одинокий", RoleCode.EXECUTIVE, 961_000_001
        )
        board = await dashboard.build(
            session, viewer=lonely, grants=await load_grants(session, lonely), now=NOW
        )
        check(board.quiet, "экран признаёт, что ничего не происходит")
        check(board.overdue_total == 0 and not board.overdue_by_department,
              "сводки просрочек нет вовсе, а не «0»")
        text = " ".join(
            f"{m.title}: {m.value}" for m in board.metrics
        )
        check(bool(board.metrics), "показатели при этом посчитаны", text[:80])
        check(
            sum(1 for m in board.metrics if m.no_data) >= 4,
            "и почти все честно молчат",
            f"молчат {sum(1 for m in board.metrics if m.no_data)} из {len(board.metrics)}",
        )

    print("\n22. Утренняя сводка: одна в день, по месту получателя")
    async with session_scope() as session:
        who = await cast(session)
        chief, worker = who["руководитель"], who["сотрудник"]
        # 07:30 по Ташкенту в тот день, где у руководителя есть встречи.
        # В UTC — так же, как их подаёт фоновый цикл: подставлять местное время
        # значит скрыть от проверки ошибку «дата сервера вместо местной».
        morning = at(MONDAY, 7, 30).astimezone(timezone.utc)

        check(digest.due_now(morning, "Asia/Tashkent"), "в 07:30 по месту сводка положена")
        check(
            not digest.due_now(at(MONDAY, 7, 0), "Asia/Tashkent"),
            "в 07:00 ещё рано",
        )
        check(
            not digest.due_now(at(MONDAY, 13, 0), "Asia/Tashkent"),
            "после обеда сводка «на день» уже врёт и не уходит",
        )

        sent = await digest.send_digests(session, now=morning)
        check(sent >= 1, "сводка поставлена в очередь", f"писем: {sent}")
        again = await digest.send_digests(session, now=morning + timedelta(minutes=5))
        check(again == 0, "второй проход в тот же день ничего не добавляет", f"{again}")

        letters = (
            await session.execute(
                select(Notification).where(Notification.kind == "digest.morning")
            )
        ).scalars().all()
        check(len(letters) == sent, "писем ровно столько, сколько сообщил проход",
              f"{len(letters)} против {sent}")
        for letter in letters:
            check(
                letter.event_key.endswith(morning.strftime("%Y-%m-%d")),
                "ключ события содержит местную дату получателя",
                letter.event_key,
            )
            break
        # Тихие часы кончаются ровно в 07:30, и сводка не должна съезжать
        # на следующее утро: иначе она приходила бы сутками позже.
        for letter in letters:
            check(
                letter.scheduled_at <= morning + timedelta(minutes=1),
                "тихие часы сводку не задерживают",
                f"{letter.scheduled_at} против {morning}",
            )
            break

        body = letters[0].body if letters else ""
        check("Утро" in body, "письмо озаглавлено как утреннее", body[:40])
        check(
            "Мой день" not in body,
            "и не подписано заголовком экрана",
        )

    print("\n23. Пустой день не рассылается")
    async with session_scope() as session:
        quiet_org = Organization(name=f"{TEST_ORG_PREFIX}Тихая", timezone="Asia/Tashkent")
        session.add(quiet_org)
        await session.flush()
        lonely = await person(
            session, quiet_org, "ТЕСТ Тихий", RoleCode.EXECUTIVE, 962_000_001
        )
        text, board = await digest.build_for(
            session, viewer=lonely, now=at(MONDAY, 7, 30).astimezone(timezone.utc)
        )
        check(text is None, "сводки без событий нет вовсе", (text or "")[:60])
        check(board.quiet, "и доска подтверждает, что день пустой")

    print("\n24. Разные пояса не сдвигают сводку на сутки")
    async with session_scope() as session:
        who = await cast(session)
        chief = who["руководитель"]
        far_org = Organization(name=f"{TEST_ORG_PREFIX}Западная", timezone="Europe/Moscow")
        session.add(far_org)
        await session.flush()
        far_chief = User(
            organization_id=far_org.id, telegram_user_id=963_000_001,
            full_name="ТЕСТ Западный", status=UserStatus.ACTIVE,
            timezone="Europe/Moscow", locale="ru",
        )
        session.add(far_chief)
        await session.flush()
        await ensure_default_working_hours(session, far_chief)
        await grant_role(session, far_chief, RoleCode.EXECUTIVE)

        # Один и тот же момент: 07:30 в Ташкенте — это 05:30 в Москве.
        tashkent_morning = at(MONDAY, 7, 30).astimezone(timezone.utc)
        check(
            digest.due_now(tashkent_morning, "Asia/Tashkent"),
            "ташкентскому получателю пора",
        )
        check(
            not digest.due_now(tashkent_morning, "Europe/Moscow"),
            "а московскому ещё нет: у него 05:30",
        )
        moscow_morning = tashkent_morning + timedelta(hours=2)
        check(
            digest.due_now(moscow_morning, "Europe/Moscow"),
            "двумя часами позже пора и ему",
        )
        # Ключ считается по местной дате, а не по дате сервера. У Ташкента
        # в 07:30 обе даты совпадают, и на нём эту ошибку не увидеть — нужен
        # пояс, где 07:30 местного приходится на вчерашний день по UTC.
        far_east = "Pacific/Auckland"
        far_morning = datetime.combine(
            MONDAY, time(7, 30), tzinfo=ZoneInfo(far_east)
        ).astimezone(timezone.utc)
        check(
            to_local_date_utc(far_morning) != MONDAY.strftime("%Y-%m-%d"),
            "нашли момент, где дата сервера и местная расходятся",
            f"UTC {to_local_date_utc(far_morning)}, местная {MONDAY}",
        )
        check(
            digest.local_date_key(far_morning, far_east) == MONDAY.strftime("%Y-%m-%d"),
            "ключ берёт местную дату получателя, а не дату сервера",
            digest.local_date_key(far_morning, far_east),
        )
        check(
            digest.due_now(far_morning, far_east),
            "и время сводки у него наступает по его же поясу",
        )

    print("\n25. Ассистент видит день руководителя, а не только свой")
    async with session_scope() as session:
        who = await cast(session)
        chief = who["руководитель"]
        org = await session.get(Organization, chief.organization_id)
        helper = await person(
            session, org, "ТЕСТ Помощник", RoleCode.ASSISTANT, 964_000_001
        )
        text, _ = await digest.build_for(
            session, viewer=helper, now=at(MONDAY, 7, 30).astimezone(timezone.utc)
        )
        check(text is not None, "ассистенту сводка собралась")
        check(
            "У руководителя сегодня" in (text or ""),
            "и в ней есть блок про день руководителя",
            (text or "")[-160:],
        )
        check(
            "ТЕСТ Руководитель" in (text or ""),
            "с именем руководителя",
        )
        chief_text, _ = await digest.build_for(
            session, viewer=chief, now=at(MONDAY, 7, 30).astimezone(timezone.utc)
        )
        check(
            "У руководителя сегодня" not in (chief_text or ""),
            "а руководителю этот блок не нужен: состав сводок разный",
        )

    print("\n26. Шаблон: срок считается от дня применения")
    async with session_scope() as session:
        who = await cast(session)
        chief, worker = who["руководитель"], who["сотрудник"]
        grants = await load_grants(session, chief)

        made = await templates.create(
            session, actor=chief, grants=grants,
            title="ТЕСТ еженедельный отчёт", description="ТЕСТ по форме",
            priority=Priority.NORMAL, days=3, assignee=worker,
        )
        check(made.ok, "шаблон заведён", made.reason or "")
        template = made.item

        # Дважды применяем один шаблон с разницей в десять дней. Срок обязан
        # съехать ровно на столько же: дата, вшитая в шаблон, через месяц
        # рождала бы поручения просроченными с первой минуты.
        first_day = at(MONDAY, 9)
        later_day = at(MONDAY + timedelta(days=10), 9)
        first = await templates.apply(
            session, template=template, actor=chief, grants=grants, now=first_day
        )
        check(first.ok, "поручение из шаблона создано", first.reason or "")
        second = await templates.apply(
            session, template=template, actor=chief, grants=grants, now=later_day
        )
        check(second.ok, "и второе, десятью днями позже", second.reason or "")
        gap = (second.item.due_at - first.item.due_at).days
        check(gap == 10, "срок съехал ровно на десять дней", f"разница {gap} дн.")

        check(
            not second.duplicate,
            "применение через десять дней дублем не считается",
        )

        # Двойное нажатие проверяется настоящими часами, а не подставными:
        # `created_at` поручению ставит база по реальному времени, и окно
        # «свежести» сравнивается именно с ним. Подставить сюда ноябрь значит
        # проверить не то, что работает в бою.
        rapid = await templates.create(
            session, actor=chief, grants=grants,
            title="ТЕСТ двойное нажатие", days=1, assignee=worker,
        )
        check(rapid.ok, "заведён шаблон для проверки повтора", rapid.reason or "")
        once = await templates.apply(
            session, template=rapid.item, actor=chief, grants=grants
        )
        again = await templates.apply(
            session, template=rapid.item, actor=chief, grants=grants
        )
        check(once.ok and not once.duplicate, "первое нажатие создаёт поручение")
        check(again.ok and again.duplicate, "второе подряд — то же самое",
              f"duplicate={again.duplicate}")
        check(again.item.id == once.item.id, "и это ровно та же запись",
              f"{again.item.id} против {once.item.id}")
        born = await session.scalar(
            select(func.count(Task.id)).where(Task.title == "ТЕСТ двойное нажатие")
        )
        check(int(born or 0) == 1, "в базе одно поручение, а не два", f"{born}")

        # А за пределами окна повтор снова становится намерением. Момент
        # отсчитывается от настоящего времени: `created_at` у поручения
        # реальный, и сравнивать его с ноябрём подставной проверки бесполезно —
        # тогда окно любого размера не сработает и проверка ничего не докажет.
        # Двадцать минут записаны числом, а не через `templates.DOUBLE_TAP`:
        # проверка, читающая ту же константу, что проверяет, подтверждает
        # только механизм и молча принимает любое её значение — хоть месяц.
        later = utcnow() + timedelta(minutes=20)
        beyond = await templates.apply(
            session, template=rapid.item, actor=chief, grants=grants, now=later
        )
        check(
            beyond.ok and not beyond.duplicate,
            "через двадцать минут это уже второе намерение",
            f"duplicate={beyond.duplicate}, окно {templates.DOUBLE_TAP}",
        )
        after = await session.scalar(
            select(func.count(Task.id)).where(Task.title == "ТЕСТ двойное нажатие")
        )
        check(int(after or 0) == 2, "и теперь их два", f"{after}")
        same_template = await templates.create(
            session, actor=chief, grants=grants,
            title="ТЕСТ еженедельный отчёт", days=3, assignee=worker,
        )
        check(
            same_template.ok and same_template.duplicate,
            "и второй шаблон с тем же названием не заводится",
            f"ok={same_template.ok}, duplicate={same_template.duplicate}",
        )

        # Тот же срок, набранный руками, обязан дать ту же дату: два способа
        # задать «через три дня» с разными ответами объяснить нечем.
        by_hand = parse_due("через 3 дня", chief.timezone, now=first_day)
        check(
            first.item.due_at == by_hand,
            "срок из шаблона совпадает с набранным руками",
            f"{first.item.due_at} против {by_hand}",
        )

    print("\n26б. Одновременные нажатия, а не подряд")
    # Десять нажатий подряд проверены выше, но подряд — не то же, что разом.
    # Бот разбирает обновления параллельно: два быстрых касания приходят двумя
    # транзакциями, и ни одна не видит незакоммиченную запись другой. Проверка
    # «нет ли уже такого» пропускает обе, и появляются два поручения.
    #
    # Поэтому здесь настоящие параллельные транзакции, а не цикл: последовательный
    # вызов эту ошибку не воспроизводит вовсе и молча подтвердил бы защиту,
    # которой нет.
    async with session_scope() as session:
        who = await cast(session)
        chief, worker = who["руководитель"], who["сотрудник"]
        grants = await load_grants(session, chief)
        together = await templates.create(
            session, actor=chief, grants=grants,
            title="ТЕСТ одновременное нажатие", days=1, assignee=worker,
        )
        check(together.ok, "заведён шаблон для одновременных нажатий",
              together.reason or "")
        together_id = together.item.id
        chief_id = chief.id

    async def press() -> None:
        async with session_scope() as own:
            actor = await own.get(User, chief_id)
            template = await own.get(TaskTemplate, together_id)
            await templates.apply(
                own, template=template, actor=actor,
                grants=await load_grants(own, actor),
            )

    await asyncio.gather(*(press() for _ in range(10)), return_exceptions=True)

    async with session_scope() as session:
        together_born = await session.scalar(
            select(func.count(Task.id)).where(Task.title == "ТЕСТ одновременное нажатие")
        )
    check(int(together_born or 0) == 1,
          "десять одновременных нажатий дали одно поручение", f"их {together_born}")

    print("\n27. Поручение из шаблона неотличимо от созданного руками")
    async with session_scope() as session:
        who = await cast(session)
        chief, worker = who["руководитель"], who["сотрудник"]
        grants = await load_grants(session, chief)
        template = (
            await session.execute(
                select(TaskTemplate).where(TaskTemplate.title == "ТЕСТ еженедельный отчёт")
            )
        ).scalar_one()

        result = await templates.apply(
            session, template=template, actor=chief, grants=grants, now=at(MONDAY, 9)
        )
        task = result.item
        check(task.status == TaskStatus.NEW, "статус обычный", task.status)
        check(task.creator_id == chief.id, "автор — тот, кто применил")
        check(task.assignee_id == worker.id, "исполнитель из шаблона")
        check(task.department_id == worker.department_id, "отдел проставлен от исполнителя")
        events = await session.scalar(
            select(func.count(TaskEvent.id)).where(
                TaskEvent.task_id == task.id, TaskEvent.kind == TaskEventKind.CREATED
            )
        )
        check(int(events or 0) == 1, "запись о создании в истории есть", f"{events}")
        logged = await session.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.entity_type == "task", AuditLog.entity_id == task.id,
                AuditLog.action == "task.create",
            )
        )
        check(int(logged or 0) == 1, "и запись в журнале аудита", f"{logged}")
        applied = await session.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.entity_id == task.id, AuditLog.action == "template.apply"
            )
        )
        check(int(applied or 0) == 1, "плюс отдельная запись о применении шаблона", f"{applied}")

    print("\n28. Шаблон не обходит область права")
    async with session_scope() as session:
        who = await cast(session)
        chief, worker, head = who["руководитель"], who["сотрудник"], who["начальник"]
        worker_grants = await load_grants(session, worker)
        template = (
            await session.execute(
                select(TaskTemplate).where(TaskTemplate.title == "ТЕСТ еженедельный отчёт")
            )
        ).scalar_one()

        # Шаблон заведён руководителем и указывает на сотрудника. Другой
        # сотрудник с областью «только свои» применить его к нему не может.
        alien = await person(
            session, await session.get(Organization, chief.organization_id),
            "ТЕСТ Посторонний", RoleCode.EMPLOYEE, 965_000_001, worker.department_id,
        )
        alien_grants = await load_grants(session, alien)
        refused = await templates.apply(
            session, template=template, actor=alien, grants=alien_grants, now=at(MONDAY, 9)
        )
        check(
            not refused.ok,
            "сотрудник не поручает чужому через шаблон",
            refused.reason or "прошло",
        )
        # А себе — может: область «только свои» это и означает.
        to_self = await templates.apply(
            session, template=template, actor=alien, grants=alien_grants,
            assignee=alien, now=at(MONDAY, 9),
        )
        check(to_self.ok, "а себе поручает", to_self.reason or "")

        # Шаблон другой организации не применяется вовсе.
        other_org = Organization(name=f"{TEST_ORG_PREFIX}Соседи", timezone="Asia/Tashkent")
        session.add(other_org)
        await session.flush()
        stranger = await person(
            session, other_org, "ТЕСТ Сосед", RoleCode.EXECUTIVE, 966_000_001
        )
        cross = await templates.apply(
            session, template=template, actor=stranger,
            grants=await load_grants(session, stranger), now=at(MONDAY, 9),
        )
        check(not cross.ok, "и чужой организации он не открыт", cross.reason or "прошло")

    print("\n29. Правит шаблон автор или администратор")
    async with session_scope() as session:
        who = await cast(session)
        chief, worker = who["руководитель"], who["сотрудник"]
        template = (
            await session.execute(
                select(TaskTemplate).where(TaskTemplate.title == "ТЕСТ еженедельный отчёт")
            )
        ).scalar_one()

        check(
            await templates.may_edit(session, template=template, actor=chief),
            "автор шаблона правит его",
        )
        check(
            not await templates.may_edit(session, template=template, actor=worker),
            "посторонний — нет",
        )
        refused = await templates.remove(session, template=template, actor=worker)
        check(refused is not None, "и удалить не может", refused or "удалил")

        admin = await person(
            session, await session.get(Organization, chief.organization_id),
            "ТЕСТ Админ", RoleCode.ADMIN, 967_000_001,
        )
        check(
            await templates.may_edit(session, template=template, actor=admin),
            "администратор правит любой шаблон",
        )
        gone = await templates.remove(session, template=template, actor=admin)
        check(gone is None, "и удаляет его", gone or "")
        left = await session.scalar(
            select(func.count(TaskTemplate.id)).where(TaskTemplate.id == template.id)
        )
        check(int(left or 0) == 0, "шаблон действительно удалён", f"{left}")
        # Поручения, созданные по шаблону, удаление не трогает: они уже живут
        # своей жизнью, и терять их вместе с заготовкой было бы бедствием.
        alive = await session.scalar(
            select(func.count(Task.id)).where(Task.title == "ТЕСТ еженедельный отчёт")
        )
        check(int(alive or 0) >= 2, "а созданные по нему поручения остались", f"{alive}")

    print("\n30. Рабочие часы: настройка действует сразу")
    async with session_scope() as session:
        who = await cast(session)
        chief, worker = who["руководитель"], who["сотрудник"]
        org = await session.get(Organization, chief.organization_id)
        admin = await person(session, org, "ТЕСТ Настройщик", RoleCode.ADMIN, 970_000_001)
        admin_grants = await load_grants(session, admin)
        worker_grants = await load_grants(session, worker)

        # День для проверки берём чистый: на нём нет встреч из прошлых разделов.
        quiet_day = MONDAY + timedelta(days=56)
        before = await slot_service.free_slots(
            session, owner=chief, duration_minutes=30, days_ahead=0,
            now=at(quiet_day, 8), limit=50,
        )
        latest_before = max(
            (to_local(s.start, "Asia/Tashkent").hour for s in before), default=0
        )
        check(latest_before >= 18, "до правки день длинный", f"последнее окно в {latest_before}")

        result = await orgadmin.set_hours(
            session, actor=admin, grants=admin_grants, subject=chief,
            start=time(9, 0), end=time(14, 0),
        )
        check(result.ok, "рабочие часы изменены", result.reason or "")

        after = await slot_service.free_slots(
            session, owner=chief, duration_minutes=30, days_ahead=0,
            now=at(quiet_day, 8), limit=50,
        )
        latest_after = max(
            (to_local(s.start, "Asia/Tashkent").hour for s in after), default=0
        )
        check(
            latest_after < 14,
            "и расчёт окон изменился сразу, без перезапуска",
            f"последнее окно теперь в {latest_after}",
        )

        # Возвращаем как было, чтобы не мешать остальным разделам.
        await orgadmin.set_hours(
            session, actor=admin, grants=admin_grants, subject=chief,
            start=time(9, 0), end=time(19, 0),
        )

        print("\n31. Настройки правит только администратор, и всё попадает в журнал")
        refused = await orgadmin.set_hours(
            session, actor=worker, grants=worker_grants, subject=chief, start=time(8, 0)
        )
        check(not refused.ok, "сотрудник расписание не правит", refused.reason or "прошло")

        logged = await session.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.actor_id == admin.id, AuditLog.action == "settings.hours"
            )
        )
        check(int(logged or 0) >= 1, "правка записана в журнал", f"{logged}")
        entry = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.actor_id == admin.id, AuditLog.action == "settings.hours"
                ).order_by(AuditLog.id).limit(1)
            )
        ).scalar_one()
        check(
            entry.before_json is not None and entry.after_json is not None,
            "и записана с «было/стало», а не одним фактом",
            f"before={bool(entry.before_json)}, after={bool(entry.after_json)}",
        )
        # Через .get, а не по ключу: упавшая проверка уносит с собой все
        # следующие разделы, и отсутствие поля выглядело бы как «всё прошло».
        was_end = ((entry.before_json or {}).get("days") or [{}])[0].get("end")
        now_end = ((entry.after_json or {}).get("days") or [{}])[0].get("end")
        check(
            was_end == "19:00" and now_end == "14:00",
            "в журнале видно, что именно поменялось",
            f"{was_end} → {now_end}",
        )

        print("\n32. Обед обязан помещаться в рабочий день")
        bad = await orgadmin.set_hours(
            session, actor=admin, grants=admin_grants, subject=chief,
            lunch_start=time(20, 0), lunch_end=time(21, 0),
        )
        check(not bad.ok, "обед за пределами дня не принимается", bad.reason or "прошло")
        # Отказ не должен оставить половину недели изменённой.
        rows = await orgadmin.hours_of(session, chief)
        check(
            all(row.lunch_start == time(13, 0) for row in rows if row.lunch_start),
            "и ни один день при этом не изменился",
            f"{[str(r.lunch_start) for r in rows[:3]]}",
        )

    print("\n33. Праздник и отпуск закрывают окна")
    async with session_scope() as session:
        who = await cast(session)
        chief, worker = who["руководитель"], who["сотрудник"]
        admin = (
            await session.execute(
                select(User).where(User.full_name == "ТЕСТ Настройщик")
            )
        ).scalar_one()
        admin_grants = await load_grants(session, admin)

        feast = MONDAY + timedelta(days=63)
        open_before = await slot_service.free_slots(
            session, owner=chief, duration_minutes=30, days_ahead=0,
            now=at(feast, 8), limit=10,
        )
        check(bool(open_before), "до праздника окна есть", f"{len(open_before)}")

        holiday = await orgadmin.set_holiday(
            session, actor=admin, grants=admin_grants, day=feast,
            title="ТЕСТ праздник", today=feast,
        )
        check(holiday.ok, "праздник заведён", holiday.reason or "")
        closed = await slot_service.free_slots(
            session, owner=chief, duration_minutes=30, days_ahead=0,
            now=at(feast, 8), limit=10,
        )
        check(not closed, "и окна в этот день исчезли", f"{len(closed)}")

        # Повторное заведение той же даты правит запись, а не плодит вторую:
        # две записи на один день сделали бы расчёт зависящим от порядка выборки.
        again = await orgadmin.set_holiday(
            session, actor=admin, grants=admin_grants, day=feast,
            title="ТЕСТ праздник переименованный", today=feast,
        )
        check(again.ok, "тот же день заводится повторно", again.reason or "")
        count = await session.scalar(
            select(func.count(Holiday.id)).where(
                Holiday.organization_id == chief.organization_id, Holiday.day == feast
            )
        )
        check(int(count or 0) == 1, "и запись осталась одна", f"{count}")

        vacation_from = MONDAY + timedelta(days=70)
        vacation_to = vacation_from + timedelta(days=4)
        leave = await orgadmin.set_absence(
            session, actor=admin, grants=admin_grants, subject=worker,
            kind=AbsenceKind.VACATION, start_date=vacation_from, end_date=vacation_to,
        )
        check(leave.ok, "отпуск заведён", leave.reason or "")
        with_worker = await slot_service.free_slots(
            session, owner=chief, duration_minutes=30, days_ahead=0,
            participants=[worker], now=at(vacation_from, 8), limit=10,
        )
        check(not with_worker, "в отпуск общих окон с ним нет", f"{len(with_worker)}")

        overlap = await orgadmin.set_absence(
            session, actor=admin, grants=admin_grants, subject=worker,
            kind=AbsenceKind.TRIP, start_date=vacation_to, end_date=vacation_to + timedelta(days=2),
        )
        check(not overlap.ok, "пересекающееся отсутствие не заводится", overlap.reason or "прошло")

    print("\n34. Переключатель закрывает раздел, а не только кнопку")
    async with session_scope() as session:
        who = await cast(session)
        chief = who["руководитель"]
        admin = (
            await session.execute(
                select(User).where(User.full_name == "ТЕСТ Настройщик")
            )
        ).scalar_one()
        admin_grants = await load_grants(session, admin)
        org_id = chief.organization_id

        state = await features.load(session, org_id)
        check(all(state.values()), "по умолчанию включено всё", str(state))
        check(
            len(state) == len(features.FEATURES),
            "и переключателей столько же, сколько разделов",
            f"{len(state)} против {len(features.FEATURES)}",
        )

        problem = await features.switch(
            session, organization_id=org_id, code="analytics", enabled=False, actor=admin
        )
        check(problem is None, "раздел выключен", problem or "")
        state = await features.load(session, org_id)
        check(not state["analytics"], "состояние сохранилось")

        # Показатели обязаны исчезнуть с экрана, а не просто спрятаться.
        board = await dashboard.build(
            session, viewer=chief, grants=await load_grants(session, chief),
            now=NOW, features=state,
        )
        check(not board.metrics, "показатели с экрана исчезли", f"{len(board.metrics)}")
        # Кнопка меню исчезает вместе с разделом встреч.
        from app.bot.keyboards.common import MENU_MY_DAY, main_menu
        from app.core.i18n import t

        my_day = t(MENU_MY_DAY, "ru")

        await features.switch(
            session, organization_id=org_id, code="meetings", enabled=False, actor=admin
        )
        state = await features.load(session, org_id)
        buttons = {
            button.text
            for row in main_menu({RoleCode.EXECUTIVE}, state, "ru").keyboard
            for button in row
        }
        check(my_day not in buttons, "кнопка «Мой день» убрана из меню", str(sorted(buttons)[:3]))
        full = {
            button.text
            for row in main_menu({RoleCode.EXECUTIVE}, None, "ru").keyboard
            for button in row
        }
        check(my_day in full, "а при включённом разделе она на месте")

        # Выключение записано в журнал: настройка меняет поведение системы.
        logged = await session.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.actor_id == admin.id, AuditLog.action == "feature.switch"
            )
        )
        check(int(logged or 0) == 2, "оба переключения в журнале", f"{logged}")

        # Сводка выключенного раздела не собирается вовсе.
        await features.switch(
            session, organization_id=org_id, code="digest", enabled=False, actor=admin
        )
        text, _ = await digest.build_for(
            session, viewer=chief, now=at(MONDAY, 7, 30).astimezone(timezone.utc)
        )
        check(text is None, "выключенная сводка не собирается", (text or "")[:40])

        # Возвращаем всё как было: остальные разделы проверки на это опираются.
        for code in ("analytics", "meetings", "digest"):
            await features.switch(
                session, organization_id=org_id, code=code, enabled=True, actor=admin
            )
        restored = await features.load(session, org_id)
        check(all(restored.values()), "и всё включается обратно", str(restored))

    print("\n35. Уборка не трогает боевые данные")
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
            select(func.count(Organization.id)).where(Organization.name == ORG_NAME)
        )
    check(real_after == real_before, "настоящие сотрудники не удалены",
          f"{real_before} → {real_after}")
    check(left == 0, "тестовая организация убрана")

    print(f"\n{'=' * 50}\nПройдено: {passed}   Ошибок: {failed}\n{'=' * 50}")
    sys.exit(1 if failed else 0)


async def run() -> None:
    await main()
    await stage_two()
    await stage_three()
    await stage_four()
    await stage_five()


if __name__ == "__main__":
    asyncio.run(run())
