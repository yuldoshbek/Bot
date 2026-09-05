"""Проверка блока 3: календарь и встречи.

Время подставляется явно (`now=`), поэтому результат не зависит от того, когда
запущен прогон. Опорная точка — понедельник 7 сентября 2026, 08:00 в Ташкенте.

Половина проверок здесь про то, что окно **не** предлагается: в обед, в выходной,
в праздник, впритык к встрече, в отпуск участника, поздно вечером без разрешения.
Отказ проверять важнее — его легко получить по неверной причине, поэтому рядом
с каждым «нельзя» стоит парный случай «а вот так можно».

    docker compose -f docker-compose.yml -f docker-compose.dev.yml \
        run --rm --no-deps migrate python scripts/smoke_block3.py
"""
import asyncio
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, func, select

from app.core.db import session_scope
from app.core.timeutil import to_local, utcnow
from app.models import (
    Absence,
    AvailabilityState,
    AuditLog,
    CalendarBlock,
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
    Room,
    RoomBooking,
    SlotHold,
    Task,
    TimeQuota,
    User,
    UserRole,
    UserStatus,
    WorkingHours,
)
from app.models.enums import (
    AbsenceKind,
    AttendanceSource,
    Availability,
    NotificationPriority,
    ParticipantRole,
    QuotaPeriod,
    RequestStatus,
    RoleCode,
)
from app.services import attendance, meetings, quotas, slots as slot_service
from app.services.availability import set_state
from app.services.bootstrap import bootstrap, ensure_default_working_hours, grant_role
from app.services.rbac import load_grants, visible_department_ids

TEST_ORG_PREFIX = "ТЕСТ "
TZ = ZoneInfo("Asia/Tashkent")
# Понедельник. Все ожидания в проверках отсчитываются от этой точки.
MONDAY = date(2026, 9, 7)
NOW = datetime.combine(MONDAY, time(8, 0), tzinfo=TZ)

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


def local(slot) -> str:
    return to_local(slot.start, "Asia/Tashkent").strftime("%a %d.%m %H:%M")


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=TZ)


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
        if meeting_ids:
            for model in (MeetingParticipant, MeetingAttendance, MeetingRating, RoomBooking):
                await session.execute(delete(model).where(model.meeting_id.in_(meeting_ids)))
        if user_ids:
            await session.execute(delete(SlotHold).where(SlotHold.owner_id.in_(user_ids)))
            await session.execute(
                delete(MeetingRequest).where(MeetingRequest.owner_id.in_(user_ids))
            )
        if meeting_ids:
            await session.execute(delete(Meeting).where(Meeting.id.in_(meeting_ids)))
        if user_ids:
            for model in (UserRole, WorkingHours, Notification, CalendarBlock, Absence):
                await session.execute(delete(model).where(model.user_id.in_(user_ids)))
            await session.execute(delete(TimeQuota).where(TimeQuota.owner_id.in_(user_ids)))
            await session.execute(delete(AuditLog).where(AuditLog.actor_id.in_(user_ids)))
            await session.execute(delete(Task).where(Task.organization_id.in_(org_ids)))
            await session.execute(delete(User).where(User.id.in_(user_ids)))
        await session.execute(delete(Room).where(Room.organization_id.in_(org_ids)))
        await session.execute(delete(Holiday).where(Holiday.organization_id.in_(org_ids)))
        await session.execute(delete(Department).where(Department.organization_id.in_(org_ids)))
        await session.execute(delete(Organization).where(Organization.id.in_(org_ids)))


async def person(session, org, name, role, tg, timezone="Asia/Tashkent") -> User:
    user = User(
        organization_id=org.id, telegram_user_id=tg, full_name=name,
        status=UserStatus.ACTIVE, timezone=timezone, locale="ru",
    )
    session.add(user)
    await session.flush()
    await ensure_default_working_hours(session, user)
    await grant_role(session, user, role)
    return user


async def busy(session, owner, start: datetime, end: datetime, title="Занято") -> Meeting:
    meeting = Meeting(
        organization_id=owner.organization_id, owner_id=owner.id, title=title,
        start_at=start, end_at=end, status=MeetingStatus.CONFIRMED,
        created_by=owner.id,
    )
    session.add(meeting)
    await session.flush()
    return meeting


async def test_people(session) -> tuple[int, int, int]:
    """Организация, руководитель и сотрудник тестовой площадки в новой сессии."""
    org_id = (
        await session.execute(
            select(Organization.id).where(Organization.name == f"{TEST_ORG_PREFIX}Календарь")
        )
    ).scalar_one()
    ids = [
        row[0] for row in (
            await session.execute(
                select(User.id).where(User.organization_id == org_id).order_by(User.id).limit(2)
            )
        ).all()
    ]
    return org_id, ids[0], ids[1]


async def main() -> None:
    await cleanup()
    tg = 940_000_000

    async with session_scope() as session:
        await bootstrap(session)
        org = Organization(name=f"{TEST_ORG_PREFIX}Календарь", timezone="Asia/Tashkent")
        session.add(org)
        await session.flush()

        chief = await person(session, org, "Руководитель", RoleCode.EXECUTIVE, tg + 1)
        worker = await person(session, org, "Сотрудник", RoleCode.EMPLOYEE, tg + 2)
        # Часовой пояс на два часа западнее: рабочий день там начинается позже
        # по ташкентскому времени и раньше заканчивается.
        remote = await person(
            session, org, "Коллега из Москвы", RoleCode.EMPLOYEE, tg + 3, timezone="Europe/Moscow"
        )

        print("\n1. Обычный день")
        found = await slot_service.free_slots(
            session, owner=chief, duration_minutes=30, days_ahead=0, now=NOW, limit=10
        )
        check(bool(found), "окна в рабочий день находятся", f"найдено: {len(found)}")
        check(
            found and to_local(found[0].start, "Asia/Tashkent").hour == 9,
            "первое окно — начало рабочего дня",
            local(found[0]) if found else "пусто",
        )
        check(
            all(9 <= to_local(s.start, "Asia/Tashkent").hour < 19 for s in found),
            "все окна внутри рабочего дня",
        )

        print("\n2. Обед")
        lunch_slots = [
            s for s in found
            if 13 <= to_local(s.start, "Asia/Tashkent").hour < 14
        ]
        check(not lunch_slots, "в обед окон нет", f"нашлось: {[local(s) for s in lunch_slots]}")
        after_lunch = [s for s in found if to_local(s.start, "Asia/Tashkent").hour == 14]
        check(bool(after_lunch), "сразу после обеда окно есть")

        print("\n3. Выходные")
        saturday = MONDAY + timedelta(days=5)
        weekend = await slot_service.free_slots(
            session, owner=chief, duration_minutes=30, days_ahead=7,
            now=datetime.combine(saturday, time(8, 0), tzinfo=TZ), limit=10,
        )
        check(
            all(to_local(s.start, "Asia/Tashkent").weekday() < 5 for s in weekend),
            "в субботу и воскресенье окон нет",
            f"{[local(s) for s in weekend[:3]]}",
        )

        print("\n4. Праздник и перенесённый рабочий день")
        tuesday = MONDAY + timedelta(days=1)
        session.add(Holiday(
            organization_id=org.id, day=tuesday, title="ТЕСТ праздник", is_working_day=False
        ))
        await session.flush()
        two_days = await slot_service.free_slots(
            session, owner=chief, duration_minutes=30, days_ahead=1, now=NOW, limit=30
        )
        check(
            not any(to_local(s.start, "Asia/Tashkent").date() == tuesday for s in two_days),
            "в праздник окон нет",
        )

        session.add(Holiday(
            organization_id=org.id, day=saturday, title="ТЕСТ рабочая суббота", is_working_day=True
        ))
        await session.flush()
        working_saturday = await slot_service.free_slots(
            session, owner=chief, duration_minutes=30, days_ahead=7, now=NOW, limit=60
        )
        check(
            any(to_local(s.start, "Asia/Tashkent").date() == saturday for s in working_saturday),
            "перенесённый рабочий день окна открывает",
        )

        print("\n5. Буфер вокруг встречи")
        wednesday = MONDAY + timedelta(days=2)
        await busy(session, chief, at(wednesday, 10), at(wednesday, 11), "Совещание")
        around = await slot_service.free_slots(
            session, owner=chief, duration_minutes=30, days_ahead=2, now=at(wednesday, 8), limit=20
        )
        same_day = [s for s in around if to_local(s.start, "Asia/Tashkent").date() == wednesday]
        check(
            not any(s.overlaps(at(wednesday, 10), at(wednesday, 11)) for s in same_day),
            "во время встречи окон нет",
        )
        check(
            not any(
                s.overlaps(at(wednesday, 9, 45), at(wednesday, 10))
                or s.overlaps(at(wednesday, 11), at(wednesday, 11, 15))
                for s in same_day
            ),
            "и впритык к ней тоже — буфер 15 минут с обеих сторон",
            f"{[local(s) for s in same_day[:6]]}",
        )
        check(
            any(to_local(s.start, "Asia/Tashkent").strftime("%H:%M") == "11:15" for s in same_day),
            "сразу за буфером окно появляется",
        )

        print("\n6. Личная блокировка и удержанный слот")
        thursday = MONDAY + timedelta(days=3)
        session.add(CalendarBlock(
            user_id=chief.id, title="ТЕСТ блок",
            start_at=at(thursday, 9), end_at=at(thursday, 12), created_by=chief.id,
        ))
        session.add(SlotHold(
            owner_id=chief.id, held_by=worker.id,
            start_at=at(thursday, 14), end_at=at(thursday, 15),
            expires_at=utcnow() + timedelta(hours=4), created_at=utcnow(),
        ))
        await session.flush()
        thu = [
            s for s in await slot_service.free_slots(
                session, owner=chief, duration_minutes=30, days_ahead=3,
                now=at(thursday, 8), limit=30,
            )
            if to_local(s.start, "Asia/Tashkent").date() == thursday
        ]
        check(
            not any(s.overlaps(at(thursday, 9), at(thursday, 12)) for s in thu),
            "личная блокировка закрывает время",
        )
        check(
            not any(s.overlaps(at(thursday, 14), at(thursday, 15)) for s in thu),
            "удержанное окно другим не предлагается",
        )
        check(bool(thu), "остальное время дня свободно")

        print("\n7. Отпуск участника")
        friday = MONDAY + timedelta(days=4)
        session.add(Absence(
            user_id=worker.id, kind=AbsenceKind.VACATION,
            start_date=friday, end_date=friday, comment="ТЕСТ отпуск",
        ))
        await session.flush()
        with_worker = await slot_service.free_slots(
            session, owner=chief, duration_minutes=30, days_ahead=4,
            participants=[worker], now=NOW, limit=40,
        )
        check(
            not any(to_local(s.start, "Asia/Tashkent").date() == friday for s in with_worker),
            "в отпуск участника общих окон нет",
        )
        alone = await slot_service.free_slots(
            session, owner=chief, duration_minutes=30, days_ahead=4, now=NOW, limit=40
        )
        check(
            any(to_local(s.start, "Asia/Tashkent").date() == friday for s in alone),
            "а без него у руководителя тот же день свободен",
        )

        print("\n8. Поздний приём")
        late_off = await slot_service.free_slots(
            session, owner=chief, duration_minutes=30, days_ahead=0, now=at(MONDAY, 18), limit=10
        )
        check(
            not any(to_local(s.start, "Asia/Tashkent").hour >= 19 for s in late_off),
            "без разрешения окон после 19:00 нет",
        )
        for row in (
            await session.execute(select(WorkingHours).where(WorkingHours.user_id == chief.id))
        ).scalars().all():
            row.allow_late = True
        await set_state(
            session, user=chief, state=Availability.OPEN, minutes=180, opens_late_slots=True
        )
        # Срок разрешения привязываем к моделируемому вечеру: сам set_state
        # считает его от настоящего «сейчас», а проверка живёт в 2026-09-07.
        state_row = (
            await session.execute(
                select(AvailabilityState).where(AvailabilityState.user_id == chief.id)
            )
        ).scalar_one()
        state_row.until_at = at(MONDAY, 22)
        await session.flush()
        late_on = await slot_service.free_slots(
            session, owner=chief, duration_minutes=30, days_ahead=0, now=at(MONDAY, 18), limit=10
        )
        check(
            any(to_local(s.start, "Asia/Tashkent").hour >= 19 for s in late_on),
            "с разрешением поздние окна появляются",
            f"{[local(s) for s in late_on]}",
        )
        check(
            all(s.is_late for s in late_on if to_local(s.start, "Asia/Tashkent").hour >= 19),
            "и помечены как поздние",
        )
        check(
            all(to_local(s.start, "Asia/Tashkent").hour < 22 for s in late_on),
            "поздний приём не выходит за названный срок",
        )
        # Разрешение дано на сегодняшний вечер. Через неделю оно не действует.
        next_week = await slot_service.free_slots(
            session, owner=chief, duration_minutes=30, days_ahead=0,
            now=at(MONDAY + timedelta(days=7), 18), limit=10,
        )
        check(
            not any(to_local(s.start, "Asia/Tashkent").hour >= 19 for s in next_week),
            "и на следующую неделю не переносится",
            f"{[local(s) for s in next_week]}",
        )

        print("\n9. Участники в разных часовых поясах")
        # Москва на два часа западнее: её 09:00–19:00 — это 11:00–21:00 в Ташкенте.
        # Общее время должно быть пересечением, а не суммой.
        next_monday = MONDAY + timedelta(days=7)
        cross = await slot_service.free_slots(
            session, owner=chief, duration_minutes=30, days_ahead=0,
            participants=[remote], now=at(next_monday, 8), limit=20,
        )
        hours_local = {to_local(s.start, "Asia/Tashkent").hour for s in cross}
        check(bool(cross), "общие окна с коллегой из другого пояса находятся")
        check(
            all(h >= 11 for h in hours_local),
            "до начала его рабочего дня окон нет",
            f"часы: {sorted(hours_local)}",
        )
        check(
            all(h < 19 for h in hours_local),
            "и после конца рабочего дня владельца тоже",
            f"часы: {sorted(hours_local)}",
        )

        print("\n10. Длительность соблюдается")
        long_slots = await slot_service.free_slots(
            session, owner=chief, duration_minutes=90, days_ahead=0,
            now=at(next_monday, 8), limit=5,
        )
        check(
            all((s.end - s.start) == timedelta(minutes=90) for s in long_slots),
            "окна ровно запрошенной длительности",
        )
        check(
            all(s.start.minute % 15 == 0 for s in long_slots),
            "и начинаются на круглом времени",
        )

        print("\n11. Проверка занятости перед подтверждением")
        free_now = await slot_service.is_free(
            session, owner=chief,
            start_at=at(next_monday, 15), end_at=at(next_monday, 16),
        )
        check(free_now, "свободное время видно как свободное")
        taken = await busy(session, chief, at(next_monday, 15), at(next_monday, 16), "Занято")
        check(
            not await slot_service.is_free(
                session, owner=chief,
                start_at=at(next_monday, 15), end_at=at(next_monday, 16),
            ),
            "занятое — как занятое",
        )
        check(
            await slot_service.is_free(
                session, owner=chief,
                start_at=at(next_monday, 15), end_at=at(next_monday, 16),
                exclude_meeting_id=taken.id,
            ),
            "своя же встреча при переносе себе не мешает",
        )

    print("\n12. База запрещает двойное бронирование")
    async with session_scope() as session:
        chief_id = (
            await session.execute(
                select(User.id).join(Organization, Organization.id == User.organization_id)
                .where(Organization.name == f"{TEST_ORG_PREFIX}Календарь")
                .order_by(User.id).limit(1)
            )
        ).scalar_one()
        org_id = (
            await session.execute(
                select(Organization.id).where(Organization.name == f"{TEST_ORG_PREFIX}Календарь")
            )
        ).scalar_one()
        far = MONDAY + timedelta(days=14)
        session.add(Meeting(
            organization_id=org_id, owner_id=chief_id, title="ТЕСТ первая",
            start_at=at(far, 10), end_at=at(far, 11),
            status=MeetingStatus.CONFIRMED, created_by=chief_id,
        ))
        await session.flush()

    from sqlalchemy.exc import IntegrityError

    async with session_scope() as session:
        org_id = (
            await session.execute(
                select(Organization.id).where(Organization.name == f"{TEST_ORG_PREFIX}Календарь")
            )
        ).scalar_one()
        chief_id = (
            await session.execute(
                select(User.id).where(User.organization_id == org_id).order_by(User.id).limit(1)
            )
        ).scalar_one()
        far = MONDAY + timedelta(days=14)
        session.add(Meeting(
            organization_id=org_id, owner_id=chief_id, title="ТЕСТ вторая",
            start_at=at(far, 10, 30), end_at=at(far, 11, 30),
            status=MeetingStatus.CONFIRMED, created_by=chief_id,
        ))
        try:
            await session.flush()
            check(False, "пересекающаяся встреча отвергается базой", "вставилась")
        except IntegrityError:
            check(True, "пересекающаяся встреча отвергается базой")
            await session.rollback()

    print("\n13. Заявка занимает окно немедленно")
    async with session_scope() as session:
        org_id, chief_id, worker_id = await test_people(session)
        chief = await session.get(User, chief_id)
        worker = await session.get(User, worker_id)
        day = MONDAY + timedelta(days=21)

        before = await session.scalar(select(func.count(SlotHold.id)))
        outcome = await meetings.create_request(
            session, initiator=worker, owner=chief,
            start_at=at(day, 10), duration_minutes=30, title="ТЕСТ обсуждение",
        )
        check(outcome.ok, "заявка создана", outcome.reason or "")
        check(
            await session.scalar(select(func.count(SlotHold.id))) == before + 1,
            "удержание появилось вместе с заявкой",
        )
        check(
            not await slot_service.is_free(
                session, owner=chief, start_at=at(day, 10), end_at=at(day, 10, 30)
            ),
            "окно сразу видно занятым, решения ещё нет",
        )
        check(
            outcome.request.status == RequestStatus.NEW,
            "заявка ждёт решения",
        )
        owner_note = await session.scalar(
            select(func.count(Notification.id)).where(
                Notification.event_key == f"meeting_request:{outcome.request.id}:new"
            )
        )
        check(owner_note == 1, "руководитель уведомлён один раз", f"писем: {owner_note}")
        request_id = outcome.request.id

    print("\n14. Отказы")
    async with session_scope() as session:
        org_id, chief_id, worker_id = await test_people(session)
        chief = await session.get(User, chief_id)
        worker = await session.get(User, worker_id)
        day = MONDAY + timedelta(days=21)

        taken = await meetings.create_request(
            session, initiator=worker, owner=chief,
            start_at=at(day, 10), duration_minutes=30, title="ТЕСТ то же окно",
        )
        check(not taken.ok, "второе окно поверх занятого не берётся")
        check(bool(taken.alternatives), "и в отказе есть варианты", "список пуст")
        check(
            all(
                not s.overlaps(at(day, 10), at(day, 10, 30))
                for s in taken.alternatives
            ),
            "варианты не повторяют занятое время",
        )

        past = await meetings.create_request(
            session, initiator=worker, owner=chief,
            start_at=at(MONDAY - timedelta(days=30), 10), duration_minutes=30,
            title="ТЕСТ прошлое",
        )
        check(not past.ok, "на прошедшее время заявку не принять")

        other_org = Organization(name=f"{TEST_ORG_PREFIX}Чужая", timezone="Asia/Tashkent")
        session.add(other_org)
        await session.flush()
        stranger = await person(session, other_org, "Чужой", RoleCode.EMPLOYEE, 940_000_099)
        cross = await meetings.create_request(
            session, initiator=stranger, owner=chief,
            start_at=at(day, 16), duration_minutes=30, title="ТЕСТ чужая организация",
        )
        check(not cross.ok, "заявка через границу организации не проходит")

    print("\n15. Десять заявок на одно окно одновременно")
    day = MONDAY + timedelta(days=22)

    barrier = asyncio.Barrier(10)

    async def attempt(person_id: int) -> tuple[bool, str | None]:
        async with session_scope() as s:
            chief_id = (
                await s.execute(
                    select(User.id).join(Organization, Organization.id == User.organization_id)
                    .where(Organization.name == f"{TEST_ORG_PREFIX}Календарь")
                    .order_by(User.id).limit(1)
                )
            ).scalar_one()
            initiator = await s.get(User, person_id)
            owner = await s.get(User, chief_id)
            # Все десять подходят к вставке одновременно. Без этого gather
            # успевал бы выполнить их по очереди, и запрет в базе — то, ради
            # чего всё затевалось, — ни разу не сработал бы.
            await barrier.wait()
            result = await meetings.create_request(
                s, initiator=initiator, owner=owner,
                start_at=at(day, 11), duration_minutes=30, title="ТЕСТ гонка",
            )
            return result.ok, result.reason

    async with session_scope() as session:
        org = (
            await session.execute(
                select(Organization).where(Organization.name == f"{TEST_ORG_PREFIX}Календарь")
            )
        ).scalar_one()
        racers = [
            (await person(session, org, f"Гонщик {i}", RoleCode.EMPLOYEE, 940_001_000 + i)).id
            for i in range(10)
        ]

    results = await asyncio.gather(*(attempt(pid) for pid in racers), return_exceptions=True)
    crashed = [r for r in results if isinstance(r, BaseException)]
    won = [r for r in results if not isinstance(r, BaseException) and r[0]]
    reasons = [r[1] for r in results if not isinstance(r, BaseException) and not r[0]]
    check(not crashed, "ни одна заявка не упала с ошибкой", f"{crashed[:1]}")
    check(len(won) == 1, "окно досталось ровно одному", f"победителей: {len(won)}")
    check(
        any("только что" in (r or "") for r in reasons),
        "отсеял именно запрет в базе, а не предварительная проверка",
        f"причины: {sorted(set(reasons))}",
    )

    async with session_scope() as session:
        holds = await session.scalar(
            select(func.count(SlotHold.id)).where(
                SlotHold.start_at == at(day, 11), SlotHold.released_at.is_(None)
            )
        )
        requests = await session.scalar(
            select(func.count(MeetingRequest.id)).where(MeetingRequest.start_at == at(day, 11))
        )
        check(holds == 1, "действующее удержание одно", f"их {holds}")
        check(requests == 1, "и заявка тоже одна — проигравшие не оставили следов", f"их {requests}")

    print("\n16. Отклонение освобождает окно сразу")
    async with session_scope() as session:
        org_id, chief_id, worker_id = await test_people(session)
        chief = await session.get(User, chief_id)
        request = await session.get(MeetingRequest, request_id)

        done = await meetings.decline(session, request=request, actor=chief, reason="Занят")
        check(done, "отклонение принято")
        check(request.status == RequestStatus.DECLINED, "заявка отклонена")
        released = await session.scalar(
            select(func.count(SlotHold.id)).where(
                SlotHold.request_id == request_id, SlotHold.released_at.is_(None)
            )
        )
        check(released == 0, "удержание снято", f"осталось: {released}")
        check(
            await slot_service.is_free(
                session, owner=chief,
                start_at=request.start_at, end_at=request.end_at,
            ),
            "окно снова свободно",
        )
        told = await session.scalar(
            select(func.count(Notification.id)).where(
                Notification.event_key == f"meeting_request:{request_id}:declined"
            )
        )
        check(told == 1, "инициатор узнал об отказе", f"писем: {told}")

        again = await meetings.decline(session, request=request, actor=chief, reason="Ещё раз")
        check(not again, "повторное отклонение ничего не меняет")
        told_again = await session.scalar(
            select(func.count(Notification.id)).where(
                Notification.event_key == f"meeting_request:{request_id}:declined"
            )
        )
        check(told_again == 1, "и второго письма инициатору не уходит", f"писем: {told_again}")

    print("\n17. Просроченное удержание освобождается само")
    async with session_scope() as session:
        org_id, chief_id, worker_id = await test_people(session)
        chief = await session.get(User, chief_id)
        worker = await session.get(User, worker_id)
        day = MONDAY + timedelta(days=23)

        outcome = await meetings.create_request(
            session, initiator=worker, owner=chief,
            start_at=at(day, 15), duration_minutes=30, title="ТЕСТ без ответа",
        )
        check(outcome.ok, "заявка создана", outcome.reason or "")
        stale_id = outcome.request.id
        check(
            outcome.hold.expires_at <= at(day, 15),
            "удержание не переживает само окно",
        )

        # Ничего не решили — срок вышел.
        outcome.hold.expires_at = utcnow() - timedelta(minutes=1)
        await session.flush()

        released = await meetings.expire_holds(session)
        check(released >= 1, "обработчик снял удержание", f"снято: {released}")
        stale = await session.get(MeetingRequest, stale_id)
        check(stale.status == RequestStatus.EXPIRED, "заявка помечена истёкшей")
        check(
            await slot_service.is_free(
                session, owner=chief, start_at=at(day, 15), end_at=at(day, 15, 30)
            ),
            "окно вернулось в оборот",
        )
        told = await session.scalar(
            select(func.count(Notification.id)).where(
                Notification.event_key == f"meeting_request:{stale_id}:expired"
            )
        )
        check(told == 1, "инициатор узнал, что ответа не было", f"писем: {told}")

        second_pass = await meetings.expire_holds(session)
        check(second_pass == 0, "второй проход не находит уже снятых")

    print("\n18. Подтверждение заявки")
    async with session_scope() as session:
        org_id, chief_id, worker_id = await test_people(session)
        chief = await session.get(User, chief_id)
        worker = await session.get(User, worker_id)
        day = MONDAY + timedelta(days=24)

        outcome = await meetings.create_request(
            session, initiator=worker, owner=chief,
            start_at=at(day, 11), duration_minutes=30, title="ТЕСТ подтверждение",
        )
        check(outcome.ok, "заявка создана", outcome.reason or "")
        request = outcome.request

        denied = await meetings.approve(session, request=request, actor=worker)
        check(not denied.ok, "сотрудник не подтверждает заявку сам себе", denied.reason or "")
        check(
            request.status == RequestStatus.NEW,
            "и заявка от этого не меняется",
        )

        done = await meetings.approve(session, request=request, actor=chief)
        check(done.ok, "руководитель подтверждает", done.reason or "")
        meeting_id = done.meeting.id
        check(done.meeting.status == MeetingStatus.CONFIRMED, "встреча подтверждена")
        check(request.status == RequestStatus.APPROVED, "заявка закрыта подтверждением")
        check(request.meeting_id == meeting_id, "заявка и встреча связаны")

        people = await session.scalar(
            select(func.count(MeetingParticipant.id)).where(
                MeetingParticipant.meeting_id == meeting_id
            )
        )
        check(people == 2, "участников двое: руководитель и инициатор", f"их {people}")
        held = await session.scalar(
            select(func.count(SlotHold.id)).where(
                SlotHold.request_id == request.id, SlotHold.released_at.is_(None)
            )
        )
        check(held == 0, "удержание снято — его заменила сама встреча", f"осталось {held}")
        told = await session.scalar(
            select(func.count(Notification.id)).where(
                Notification.event_key.like(f"meeting:{meeting_id}:confirmed:%")
            )
        )
        check(told == 2, "о подтверждении узнали оба", f"писем: {told}")

        twice = await meetings.approve(session, request=request, actor=chief)
        check(not twice.ok, "повторное подтверждение ничего не создаёт", twice.reason or "")

    print("\n19. Перенос")
    async with session_scope() as session:
        org_id, chief_id, worker_id = await test_people(session)
        chief = await session.get(User, chief_id)
        worker = await session.get(User, worker_id)
        meeting = await session.get(Meeting, meeting_id)
        day = MONDAY + timedelta(days=24)

        no_reason = await meetings.reschedule(
            session, meeting=meeting, actor=chief, new_start=at(day, 16), reason="  ",
        )
        check(not no_reason.ok, "перенос без причины не принимается", no_reason.reason or "")
        check(meeting.start_at == at(day, 11), "время осталось прежним")

        not_yours = await meetings.reschedule(
            session, meeting=meeting, actor=worker,
            new_start=at(day, 16), reason="Мне так удобнее",
        )
        check(not not_yours.ok, "сотрудник не переносит чужую встречу", not_yours.reason or "")
        check(meeting.start_at == at(day, 11), "и время опять не изменилось")

        moved = await meetings.reschedule(
            session, meeting=meeting, actor=chief,
            new_start=at(day, 16), reason="Совещание у директора",
        )
        check(moved.ok, "руководитель переносит", moved.reason or "")
        check(meeting.start_at == at(day, 16), "время изменилось")
        check(meeting.reschedule_count == 1, "перенос сосчитан")
        told = await session.scalar(
            select(func.count(Notification.id)).where(
                Notification.event_key.like(f"meeting:{meeting_id}:moved:1:%")
            )
        )
        check(told == 2, "о переносе узнали все участники", f"писем: {told}")
        body = await session.scalar(
            select(Notification.body).where(
                Notification.event_key.like(f"meeting:{meeting_id}:moved:1:%")
            ).limit(1)
        )
        check("Совещание у директора" in body, "и причина в письме есть")

        past = await meetings.reschedule(
            session, meeting=meeting, actor=chief,
            new_start=at(MONDAY - timedelta(days=10), 11), reason="Назад во времени",
        )
        check(not past.ok, "перенос в прошлое не проходит", past.reason or "")

        blocker = await busy(session, chief, at(day, 9), at(day, 10), "ТЕСТ занято")
        onto_busy = await meetings.reschedule(
            session, meeting=meeting, actor=chief,
            new_start=at(day, 9, 15), reason="Поверх другой встречи",
        )
        check(not onto_busy.ok, "перенос на занятое время не проходит", onto_busy.reason or "")
        check(meeting.reschedule_count == 1, "и счётчик переносов не вырос")

    print("\n20. Отмена")
    async with session_scope() as session:
        org_id, chief_id, worker_id = await test_people(session)
        chief = await session.get(User, chief_id)
        worker = await session.get(User, worker_id)
        meeting = await session.get(Meeting, meeting_id)

        empty = await meetings.cancel(session, meeting=meeting, actor=chief, reason="")
        check(not empty.ok, "отмена без причины не принимается", empty.reason or "")
        stranger = await meetings.cancel(
            session, meeting=meeting, actor=worker, reason="Передумал",
        )
        check(not stranger.ok, "сотрудник не отменяет чужую встречу", stranger.reason or "")
        check(meeting.status == MeetingStatus.CONFIRMED, "встреча всё ещё в силе")

        killed = await meetings.cancel(
            session, meeting=meeting, actor=chief, reason="Командировка",
        )
        check(killed.ok, "руководитель отменяет", killed.reason or "")
        check(meeting.status == MeetingStatus.CANCELLED, "встреча отменена")
        told = await session.scalar(
            select(func.count(Notification.id)).where(
                Notification.event_key.like(f"meeting:{meeting_id}:cancelled:%")
            )
        )
        check(told == 2, "об отмене узнали все", f"писем: {told}")
        check(
            await slot_service.is_free(
                session, owner=chief, start_at=meeting.start_at, end_at=meeting.end_at
            ),
            "время отменённой встречи снова свободно",
        )

        again = await meetings.cancel(session, meeting=meeting, actor=chief, reason="Ещё раз")
        check(not again.ok, "повторная отмена ничего не меняет", again.reason or "")
        told_again = await session.scalar(
            select(func.count(Notification.id)).where(
                Notification.event_key.like(f"meeting:{meeting_id}:cancelled:%")
            )
        )
        check(told_again == 2, "и второго круга писем нет", f"писем: {told_again}")

        moved_dead = await meetings.reschedule(
            session, meeting=meeting, actor=chief,
            new_start=at(MONDAY + timedelta(days=25), 11), reason="Верните",
        )
        check(not moved_dead.ok, "отменённую встречу не перенести", moved_dead.reason or "")

    print("\n21. Быстрое совещание")
    async with session_scope() as session:
        org_id, chief_id, worker_id = await test_people(session)
        chief = await session.get(User, chief_id)
        worker = await session.get(User, worker_id)
        others = [
            row[0] for row in (
                await session.execute(
                    select(User.id).where(
                        User.organization_id == org_id, User.id.notin_([chief_id])
                    ).order_by(User.id).limit(3)
                )
            ).all()
        ]
        day = MONDAY + timedelta(days=26)

        nameless = await meetings.quick(
            session, organizer=chief, participant_ids=others,
            title="   ", start_at=at(day, 12),
        )
        check(not nameless.ok, "совещание без темы не собрать", nameless.reason or "")

        nobody = await meetings.quick(
            session, organizer=chief, participant_ids=[chief_id],
            title="ТЕСТ сам с собой", start_at=at(day, 12),
        )
        check(not nobody.ok, "совещание из одного организатора не собрать", nobody.reason or "")

        called = await meetings.quick(
            session, organizer=chief, participant_ids=others,
            title="ТЕСТ планёрка", start_at=at(day, 12), duration_minutes=20,
        )
        check(called.ok, "совещание собрано", called.reason or "")
        quick_id = called.meeting.id
        count = await session.scalar(
            select(func.count(MeetingParticipant.id)).where(
                MeetingParticipant.meeting_id == quick_id
            )
        )
        check(count == len(others) + 1, "все приглашены плюс организатор", f"их {count}")
        told = await session.scalar(
            select(func.count(Notification.id)).where(
                Notification.event_key.like(f"meeting:{quick_id}:called:%")
            )
        )
        check(told == len(others), "письма ушли всем, кроме организатора", f"писем: {told}")
        urgent = await session.scalar(
            select(Notification.priority).where(
                Notification.event_key.like(f"meeting:{quick_id}:called:%")
            ).limit(1)
        )
        check(
            urgent == NotificationPriority.CRITICAL,
            "и уходят как срочные — тихие часы им не помеха",
            f"приоритет: {urgent}",
        )

        stranger_org = (
            await session.execute(
                select(Organization.id).where(Organization.name == f"{TEST_ORG_PREFIX}Чужая")
            )
        ).scalar_one()
        stranger_id = (
            await session.execute(
                select(User.id).where(User.organization_id == stranger_org).limit(1)
            )
        ).scalar_one()
        mixed = await meetings.quick(
            session, organizer=chief, participant_ids=[*others, stranger_id],
            title="ТЕСТ с чужаком", start_at=at(day, 15),
        )
        check(mixed.ok, "совещание с чужаком в списке всё же собирается", mixed.reason or "")
        invited = [
            row[0] for row in (
                await session.execute(
                    select(MeetingParticipant.user_id).where(
                        MeetingParticipant.meeting_id == mixed.meeting.id
                    )
                )
            ).all()
        ]
        check(stranger_id not in invited, "но человек из другой организации не приглашён")

    print("\n22. Двойное нажатие")
    async with session_scope() as session:
        org_id, chief_id, worker_id = await test_people(session)
        chief = await session.get(User, chief_id)
        day = MONDAY + timedelta(days=27)
        target = await busy(session, chief, at(day, 10), at(day, 11), "ТЕСТ двойное нажатие")
        session.add(MeetingParticipant(
            meeting_id=target.id, user_id=chief_id,
            role=ParticipantRole.ORGANIZER, created_at=utcnow(),
        ))
        session.add(MeetingParticipant(
            meeting_id=target.id, user_id=worker_id,
            role=ParticipantRole.REQUIRED, created_at=utcnow(),
        ))
        await session.flush()
        double_id = target.id

        # Проверка самого замка, а не раннего возврата по статусу: повторная
        # рассылка с тем же ключом события не должна создать ни одного письма.
        first = await meetings._tell_everyone(
            session, target, key=f"meeting:{double_id}:probe",
            kind="meeting.probe", header="Проверка",
        )
        second = await meetings._tell_everyone(
            session, target, key=f"meeting:{double_id}:probe",
            kind="meeting.probe", header="Проверка",
        )
        check(first == 2, "первая рассылка дошла до обоих", f"писем: {first}")
        check(second == 0, "повторная с тем же ключом не создаёт ничего", f"писем: {second}")

    # Два одновременных нажатия «Отменить»: проверка статуса их не разведёт,
    # обе транзакции видят встречу подтверждённой.
    tap_barrier = asyncio.Barrier(2)

    async def tap_cancel() -> bool:
        async with session_scope() as s:
            actor = await s.get(User, (await test_people(s))[1])
            meeting = await s.get(Meeting, double_id)
            await tap_barrier.wait()
            result = await meetings.cancel(
                s, meeting=meeting, actor=actor, reason="Двойное нажатие",
            )
            return result.ok

    taps = await asyncio.gather(tap_cancel(), tap_cancel(), return_exceptions=True)
    broke = [r for r in taps if isinstance(r, BaseException)]
    check(not broke, "одновременная отмена не падает", f"{broke[:1]}")

    async with session_scope() as session:
        letters = await session.scalar(
            select(func.count(Notification.id)).where(
                Notification.event_key.like(f"meeting:{double_id}:cancelled:%")
            )
        )
        state = await session.scalar(
            select(Meeting.status).where(Meeting.id == double_id)
        )
        check(state == MeetingStatus.CANCELLED, "встреча отменена", f"состояние: {state}")
        check(letters == 2, "письмо каждому участнику ровно одно", f"писем: {letters}")

    print("\n23. Приглашение отметиться")
    async with session_scope() as session:
        org_id, chief_id, worker_id = await test_people(session)
        chief = await session.get(User, chief_id)
        worker = await session.get(User, worker_id)
        org = await session.get(Organization, org_id)
        helper = await person(session, org, "Ассистент", RoleCode.ASSISTANT, 940_002_001)
        day = MONDAY + timedelta(days=28)

        gathering = await busy(session, chief, at(day, 10), at(day, 11), "ТЕСТ явка")
        for uid, role in (
            (chief_id, ParticipantRole.ORGANIZER),
            (worker_id, ParticipantRole.REQUIRED),
        ):
            session.add(MeetingParticipant(
                meeting_id=gathering.id, user_id=uid, role=role, created_at=utcnow()
            ))
        await session.flush()
        gathering_id = gathering.id

        early = await attendance.open_checkins(session, now=at(day, 9))
        check(early == 0, "за час до начала никого не зовут", f"писем: {early}")

        called = await attendance.open_checkins(session, now=at(day, 9, 57))
        check(called == 2, "за три минуты позвали обоих", f"писем: {called}")

        again = await attendance.open_checkins(session, now=at(day, 9, 58))
        check(again == 0, "повторный проход не зовёт второй раз", f"писем: {again}")

    print("\n24. Отметка о присутствии")
    async with session_scope() as session:
        org_id, chief_id, worker_id = await test_people(session)
        worker = await session.get(User, worker_id)
        chief = await session.get(User, chief_id)
        gathering = await session.get(Meeting, gathering_id)
        day = MONDAY + timedelta(days=28)

        too_early, why = await attendance.check_in(
            session, meeting=gathering, user=worker, now=at(day, 9, 30)
        )
        check(not too_early, "заранее отметиться нельзя", why or "")

        marked, why = await attendance.check_in(
            session, meeting=gathering, user=worker, now=at(day, 9, 58)
        )
        check(marked, "в окне отметка принимается", why or "")

        twice, why = await attendance.check_in(
            session, meeting=gathering, user=worker, now=at(day, 9, 59)
        )
        check(not twice, "двойное нажатие второй записи не создаёт", why or "")
        rows = await session.scalar(
            select(func.count(MeetingAttendance.id)).where(
                MeetingAttendance.meeting_id == gathering_id,
                MeetingAttendance.user_id == worker_id,
            )
        )
        check(rows == 1, "запись о явке ровно одна", f"их {rows}")

        late_ok, why = await attendance.check_in(
            session, meeting=gathering, user=chief, now=at(day, 10, 7)
        )
        check(late_ok, "опоздавший отмечается", why or "")
        late = await session.scalar(
            select(MeetingAttendance.late_minutes).where(
                MeetingAttendance.meeting_id == gathering_id,
                MeetingAttendance.user_id == chief_id,
            )
        )
        check(late == 7, "и опоздание записано в минутах", f"записано: {late}")

    print("\n25. Правка явки после встречи")
    async with session_scope() as session:
        org_id, chief_id, worker_id = await test_people(session)
        chief = await session.get(User, chief_id)
        worker = await session.get(User, worker_id)
        helper = (
            await session.execute(
                select(User).where(User.telegram_user_id == 940_002_001)
            )
        ).scalar_one()
        gathering = await session.get(Meeting, gathering_id)
        day = MONDAY + timedelta(days=28)

        late_self, why = await attendance.check_in(
            session, meeting=gathering, user=worker, now=at(day, 12)
        )
        check(not late_self, "после окончания сам себя не отметить", why or "")

        denied, why = await attendance.correct(
            session, meeting=gathering, user_id=chief_id, actor=worker, present=False
        )
        check(not denied, "сотрудник чужую явку не правит", why or "")

        fixed, why = await attendance.correct(
            session, meeting=gathering, user_id=worker_id, actor=helper,
            present=False, now=at(day, 12),
        )
        check(fixed, "ассистент правит явку и после встречи", why or "")
        source = await session.scalar(
            select(MeetingAttendance.source).where(
                MeetingAttendance.meeting_id == gathering_id,
                MeetingAttendance.user_id == worker_id,
            )
        )
        check(source == AttendanceSource.ASSISTANT, "и видно, что правил ассистент", f"{source}")

        sheet = await attendance.roll_call(session, gathering)
        check(len(sheet) == 2, "перекличка охватывает всех участников", f"строк {len(sheet)}")
        check(
            any(u.id == chief_id and was and mins == 7 for u, was, mins in sheet),
            "в перекличке видно опоздание",
        )
        check(
            any(u.id == worker_id and not was for u, was, _ in sheet),
            "и исправленное отсутствие",
        )

    print("\n26. Оценка встречи")
    async with session_scope() as session:
        org_id, chief_id, worker_id = await test_people(session)
        chief = await session.get(User, chief_id)
        worker = await session.get(User, worker_id)
        helper = (
            await session.execute(
                select(User).where(User.telegram_user_id == 940_002_001)
            )
        ).scalar_one()
        gathering = await session.get(Meeting, gathering_id)
        day = MONDAY + timedelta(days=28)

        too_soon, why = await attendance.rate(
            session, meeting=gathering, actor=chief, score=1, now=at(day, 10, 30)
        )
        check(not too_soon, "недошедшую встречу не оценить", why or "")

        nonsense, why = await attendance.rate(
            session, meeting=gathering, actor=chief, score=5, now=at(day, 12)
        )
        check(not nonsense, "оценка бывает только 1, 0 или -1", why or "")

        not_mine, why = await attendance.rate(
            session, meeting=gathering, actor=worker, score=1, now=at(day, 12)
        )
        check(not not_mine, "сотрудник встречу не оценивает", why or "")

        rated, why = await attendance.rate(
            session, meeting=gathering, actor=chief, score=-1,
            comment="Не подготовились", voice_file_id="ТЕСТ-голос",
            now=at(day, 12),
        )
        check(rated, "руководитель ставит оценку", why or "")

        changed, why = await attendance.rate(
            session, meeting=gathering, actor=chief, score=1, now=at(day, 12, 5)
        )
        check(changed, "и может передумать", why or "")
        rows = await session.scalar(
            select(func.count(MeetingRating.id)).where(
                MeetingRating.meeting_id == gathering_id
            )
        )
        check(rows == 1, "оценка при этом остаётся одна", f"их {rows}")
        stored = (
            await session.execute(
                select(MeetingRating).where(MeetingRating.meeting_id == gathering_id)
            )
        ).scalar_one()
        check(stored.score == 1, "с новым значением", f"{stored.score}")
        check(stored.voice_file_id == "ТЕСТ-голос", "голосовой комментарий сохранён")

    print("\n27. Кто видит оценки")
    async with session_scope() as session:
        org_id, chief_id, worker_id = await test_people(session)
        chief = await session.get(User, chief_id)
        worker = await session.get(User, worker_id)
        helper = (
            await session.execute(
                select(User).where(User.telegram_user_id == 940_002_001)
            )
        ).scalar_one()
        gathering = await session.get(Meeting, gathering_id)

        check(
            await attendance.rating_for(session, meeting=gathering, viewer=chief) is not None,
            "руководитель свою оценку видит",
        )
        check(
            await attendance.rating_for(session, meeting=gathering, viewer=helper) is not None,
            "ассистент тоже",
        )
        check(
            await attendance.rating_for(session, meeting=gathering, viewer=worker) is None,
            "а участник встречи — нет",
        )
        stranger_org = (
            await session.execute(
                select(Organization.id).where(Organization.name == f"{TEST_ORG_PREFIX}Чужая")
            )
        ).scalar_one()
        outsider = (
            await session.execute(
                select(User).where(User.organization_id == stranger_org).limit(1)
            )
        ).scalar_one()
        check(
            await attendance.rating_for(session, meeting=gathering, viewer=outsider) is None,
            "и человек из другой организации — тем более",
        )

        average, count = await attendance.average_score(
            session, organization_id=org_id, since=at(MONDAY, 0),
        )
        check(count == 1, "в обезличенной выборке оценка учтена", f"их {count}")
        check(average == 1.0, "со своим значением", f"{average}")

    print("\n28. Лимиты времени")
    week = MONDAY + timedelta(days=35)          # понедельник
    moment = at(week, 8)
    async with session_scope() as session:
        org_id, chief_id, worker_id = await test_people(session)
        chief = await session.get(User, chief_id)
        worker = await session.get(User, worker_id)

        free = await quotas.view(session, owner=chief, subject=worker, now=moment)
        check(free.unlimited, "без нормы лимита нет")
        check(
            not await quotas.would_exceed(
                session, owner=chief, subject=worker, minutes=600, now=moment
            ),
            "и десять часов не превышают ненайденную норму",
        )

        dept = Department(organization_id=org_id, name="ТЕСТ отдел")
        session.add(dept)
        await session.flush()
        worker.department_id = dept.id
        session.add(TimeQuota(
            organization_id=org_id, owner_id=chief_id,
            subject_department_id=dept.id, minutes=60, period=QuotaPeriod.WEEK,
        ))
        await session.flush()

        empty = await quotas.view(session, owner=chief, subject=worker, now=moment)
        check(empty.limit == 60, "отдельская норма нашлась", f"{empty.limit}")
        check(empty.spent == 0, "расход пока нулевой", f"{empty.spent}")
        check(empty.left == 60, "остаток равен норме", f"{empty.left}")

        # Две встречи по полчаса в этой неделе.
        for hour in (9, 10):
            m = await busy(session, chief, at(week, hour), at(week, hour, 30), "ТЕСТ расход")
            session.add(MeetingParticipant(
                meeting_id=m.id, user_id=worker_id,
                role=ParticipantRole.REQUIRED, created_at=utcnow(),
            ))
        await session.flush()

        used = await quotas.view(session, owner=chief, subject=worker, now=moment)
        check(used.spent == 60, "расход считается по встречам периода", f"{used.spent}")
        check(used.left == 0, "остатка не осталось", f"{used.left}")

        # Отменённая времени не съела.
        dead = await busy(session, chief, at(week, 11), at(week, 12), "ТЕСТ отменённая")
        dead.status = MeetingStatus.CANCELLED
        session.add(MeetingParticipant(
            meeting_id=dead.id, user_id=worker_id,
            role=ParticipantRole.REQUIRED, created_at=utcnow(),
        ))
        await session.flush()
        after_cancel = await quotas.view(session, owner=chief, subject=worker, now=moment)
        check(after_cancel.spent == 60, "отменённая в расход не идёт", f"{after_cancel.spent}")

        # Встреча прошлой недели в этот период не попадает.
        # Вторник предыдущей недели: тот понедельник уже занят встречей из
        # проверки явки, а пересечения запрещает сама база.
        last_week = week - timedelta(days=6)
        old = await busy(
            session, chief, at(last_week, 9), at(last_week, 11), "ТЕСТ прошлая неделя",
        )
        session.add(MeetingParticipant(
            meeting_id=old.id, user_id=worker_id,
            role=ParticipantRole.REQUIRED, created_at=utcnow(),
        ))
        await session.flush()
        this_week = await quotas.view(session, owner=chief, subject=worker, now=moment)
        check(this_week.spent == 60, "прошлая неделя в расход не идёт", f"{this_week.spent}")
        check(
            to_local(this_week.period_start, "Asia/Tashkent").weekday() == 0,
            "период начинается в понедельник по местному календарю",
        )

    print("\n29. Превышение помечает, но не запрещает")
    async with session_scope() as session:
        org_id, chief_id, worker_id = await test_people(session)
        chief = await session.get(User, chief_id)
        worker = await session.get(User, worker_id)

        check(
            await quotas.would_exceed(
                session, owner=chief, subject=worker, minutes=30, now=moment
            ),
            "тридцать минут сверх исчерпанной нормы — это превышение",
        )
        outcome = await meetings.create_request(
            session, initiator=worker, owner=chief,
            start_at=at(week, 15), duration_minutes=30,
            title="ТЕСТ сверх лимита", now=moment,
        )
        check(outcome.ok, "заявка сверх лимита всё равно создаётся", outcome.reason or "")
        check(outcome.request.over_quota, "и помечена как сверхлимитная")
        letter = await session.scalar(
            select(Notification.body).where(
                Notification.event_key == f"meeting_request:{outcome.request.id}:new"
            )
        )
        check("Сверх лимита" in letter, "руководитель видит пометку в письме")

        separate = await quotas.over_quota_requests(session, owner=chief)
        check(
            any(r.id == outcome.request.id for r in separate),
            "и такие заявки собраны отдельно",
            f"их {len(separate)}",
        )

        # Личное исключение сильнее отдельской нормы.
        session.add(TimeQuota(
            organization_id=org_id, owner_id=chief_id,
            subject_user_id=worker_id, minutes=240, period=QuotaPeriod.WEEK,
        ))
        await session.flush()
        personal = await quotas.view(session, owner=chief, subject=worker, now=moment)
        check(personal.limit == 240, "личная норма перебивает отдельскую", f"{personal.limit}")
        check(
            not await quotas.would_exceed(
                session, owner=chief, subject=worker, minutes=30, now=moment
            ),
            "и та же заявка в неё укладывается",
        )
        calm = await meetings.create_request(
            session, initiator=worker, owner=chief,
            start_at=at(week, 16), duration_minutes=30,
            title="ТЕСТ в пределах нормы", now=moment,
        )
        check(calm.ok, "заявка в пределах нормы проходит", calm.reason or "")
        check(not calm.request.over_quota, "и пометки на ней нет")

    print("\n30. Встреча с участниками — одна встреча, а не несколько подряд")
    async with session_scope() as session:
        org_id, chief_id, _ = await test_people(session)
        org = await session.get(Organization, org_id)
        chief = await session.get(User, chief_id)

        crowd_day = MONDAY + timedelta(days=42)
        crowded = await busy(
            session, chief, at(crowd_day, 9), at(crowd_day, 10), "ТЕСТ совещание на троих"
        )
        for number in range(3):
            guest = await person(
                session, org, f"ТЕСТ участник {number}",
                RoleCode.EMPLOYEE, 941_000_000 + number,
            )
            session.add(MeetingParticipant(
                meeting_id=crowded.id, user_id=guest.id,
                role=ParticipantRole.REQUIRED, created_at=utcnow(),
            ))
        await session.flush()

        # Запрос грузит встречу по строке на каждого участника. Если дубли
        # доходят до счётчика «встреч подряд», одна встреча на троих читается
        # как цепочка из четырёх, и окно сразу за буфером пропадает молча.
        after = await slot_service.free_slots(
            session, owner=chief, duration_minutes=30, days_ahead=0,
            now=at(crowd_day, 8), limit=20,
        )
        check(
            any(s.start == at(crowd_day, 10, 15) for s in after),
            "окно сразу за буфером предлагается",
            f"нашлось: {[local(s) for s in after[:4]]}",
        )

        # Обратная сторона: настоящая цепочка из трёх встреч предел всё так же
        # держит. Без этой проверки исправление дублей могло бы снять лимит.
        chain_day = MONDAY + timedelta(days=43)
        await busy(session, chief, at(chain_day, 9), at(chain_day, 9, 30), "ТЕСТ подряд 1")
        await busy(session, chief, at(chain_day, 9, 45), at(chain_day, 10, 15), "ТЕСТ подряд 2")
        await busy(session, chief, at(chain_day, 10, 30), at(chain_day, 11), "ТЕСТ подряд 3")
        await session.flush()

        chained = await slot_service.free_slots(
            session, owner=chief, duration_minutes=30, days_ahead=0,
            now=at(chain_day, 8), limit=20,
        )
        check(
            not any(s.start == at(chain_day, 11, 15) for s in chained),
            "четвёртая встреча подряд не предлагается",
            f"нашлось: {[local(s) for s in chained[:4]]}",
        )
        check(
            any(s.start == at(chain_day, 11, 45) for s in chained),
            "а после паузы окно снова открыто",
            f"нашлось: {[local(s) for s in chained[:4]]}",
        )

    print("\n31. Карточка встречи и отметка явки закрыты для посторонних")
    async with session_scope() as session:
        org_id, chief_id, worker_id = await test_people(session)
        org = await session.get(Organization, org_id)
        chief = await session.get(User, chief_id)
        insider = await session.get(User, worker_id)
        outsider = await person(
            session, org, "ТЕСТ посторонний", RoleCode.EMPLOYEE, 942_000_001
        )

        card_day = MONDAY + timedelta(days=44)
        closed = await busy(
            session, chief, at(card_day, 15), at(card_day, 15, 30), "ТЕСТ закрытая встреча"
        )
        session.add(MeetingParticipant(
            meeting_id=closed.id, user_id=chief.id,
            role=ParticipantRole.ORGANIZER, created_at=utcnow(),
        ))
        session.add(MeetingParticipant(
            meeting_id=closed.id, user_id=insider.id,
            role=ParticipantRole.REQUIRED, created_at=utcnow(),
        ))
        await session.flush()

        check(
            await meetings.may_read(session, meeting=closed, viewer=chief),
            "ведущий открывает карточку своей встречи",
        )
        check(
            await meetings.may_read(session, meeting=closed, viewer=insider),
            "участник открывает карточку своей встречи",
        )
        check(
            not await meetings.may_read(session, meeting=closed, viewer=outsider),
            "сотрудник со стороны карточку не открывает",
        )

        # Право на запись описано дважды: проверкой записи и условием запроса.
        # Расходятся они молча, поэтому сверяются прогоном по матрице
        # «встреча × человек» — так же, как для документов в блоке 4.
        all_meetings = (
            await session.execute(select(Meeting).where(Meeting.organization_id == org_id))
        ).scalars().all()
        for viewer in (chief, insider, outsider):
            grants = await load_grants(session, viewer)
            departments = await visible_department_ids(session, viewer)
            in_query = set(
                (
                    await session.execute(
                        select(Meeting.id).where(
                            *meetings.visible_filter(viewer, grants, departments)
                        )
                    )
                ).scalars().all()
            )
            mismatch = []
            for candidate in all_meetings:
                direct = await meetings.may_read(
                    session, meeting=candidate, viewer=viewer
                )
                if direct != (candidate.id in in_query):
                    mismatch.append(candidate.id)
            check(
                not mismatch,
                f"проверка записи и условие запроса совпадают: {viewer.full_name}",
                f"разошлись на встречах {mismatch[:5]} из {len(all_meetings)}",
            )

        moment = at(card_day, 15, 5)
        ok, why = await attendance.check_in(
            session, meeting=closed, user=outsider, now=moment
        )
        check(not ok, "посторонний не отмечается на чужой встрече", why or "прошло")
        ok, why = await attendance.check_in(
            session, meeting=closed, user=insider, now=moment
        )
        check(ok, "участник отмечается", why or "")
        marks = await session.scalar(
            select(func.count(MeetingAttendance.id)).where(
                MeetingAttendance.meeting_id == closed.id
            )
        )
        check(marks == 1, "в журнале явки ровно одна запись", f"записей: {marks}")

    print("\n32. Уборка не трогает боевые данные")
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
        left = await session.scalar(select(func.count(Meeting.id)))
    check(real_after == real_before, "настоящие сотрудники не удалены", f"{real_before} → {real_after}")
    check(left == 0, "тестовые встречи убраны")

    print(f"\n{'=' * 50}\nПройдено: {passed}   Ошибок: {failed}\n{'=' * 50}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
