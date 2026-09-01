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
from app.models.enums import AbsenceKind, Availability, RoleCode
from app.services import slots as slot_service
from app.services.availability import set_state
from app.services.bootstrap import bootstrap, ensure_default_working_hours, grant_role

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
        left = await session.scalar(select(func.count(Meeting.id)))
    check(real_after == real_before, "настоящие сотрудники не удалены", f"{real_before} → {real_after}")
    check(left == 0, "тестовые встречи убраны")

    print(f"\n{'=' * 50}\nПройдено: {passed}   Ошибок: {failed}\n{'=' * 50}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
