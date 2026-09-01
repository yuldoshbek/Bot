"""Проверка блока 4: встреча → решение → документ.

Как и в блоке 3, время подставляется явно, а половина проверок — про отказы.
Отказ проверять важнее: его слишком легко получить по неверной причине.

    docker compose -f docker-compose.yml -f docker-compose.dev.yml \
        run --rm --no-deps migrate python scripts/smoke_block4.py
"""
import asyncio
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, func, select, text

from app.core.db import session_scope
from app.core.timeutil import utcnow
from app.models import (
    AgendaItem,
    AuditLog,
    Decision,
    DecisionStatus,
    Department,
    Document,
    DocumentAccess,
    DocumentText,
    DocumentView,
    DownloadToken,
    Meeting,
    MeetingAttendance,
    MeetingParticipant,
    MeetingRating,
    MeetingRequest,
    MeetingStatus,
    Notification,
    Organization,
    ParticipantRole,
    RoomBooking,
    SlotHold,
    Task,
    TimeQuota,
    User,
    UserRole,
    UserStatus,
    WorkingHours,
)
from app.models.enums import RoleCode
from app.services import decisions as registry
from app.services import meetings as meeting_service
from app.services.bootstrap import bootstrap, ensure_default_working_hours, grant_role
from app.services.rbac import load_grants

TEST_ORG_PREFIX = "ТЕСТ "
TZ = ZoneInfo("Asia/Tashkent")
MONDAY = date(2026, 10, 5)

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
        doc_ids = [
            row[0] for row in (
                await session.execute(
                    select(Document.id).where(Document.organization_id.in_(org_ids))
                )
            ).all()
        ]
        meeting_ids = [
            row[0] for row in (
                await session.execute(
                    select(Meeting.id).where(Meeting.organization_id.in_(org_ids))
                )
            ).all()
        ]
        if doc_ids:
            for model in (DocumentAccess, DocumentText, DocumentView, DownloadToken):
                await session.execute(delete(model).where(model.document_id.in_(doc_ids)))
            await session.execute(delete(Document).where(Document.id.in_(doc_ids)))
        await session.execute(delete(Decision).where(Decision.organization_id.in_(org_ids)))
        if meeting_ids:
            await session.execute(
                delete(AgendaItem).where(AgendaItem.meeting_id.in_(meeting_ids))
            )
            for model in (MeetingParticipant, MeetingAttendance, MeetingRating, RoomBooking):
                await session.execute(delete(model).where(model.meeting_id.in_(meeting_ids)))
        if user_ids:
            await session.execute(delete(SlotHold).where(SlotHold.owner_id.in_(user_ids)))
            await session.execute(
                delete(MeetingRequest).where(MeetingRequest.owner_id.in_(user_ids))
            )
        if meeting_ids:
            await session.execute(delete(Meeting).where(Meeting.id.in_(meeting_ids)))
        await session.execute(delete(Task).where(Task.organization_id.in_(org_ids)))
        if user_ids:
            for model in (UserRole, WorkingHours, Notification):
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


async def cast(session) -> dict[str, User]:
    """Действующие лица тестовой организации в свежей сессии."""
    org_id = (
        await session.execute(
            select(Organization.id).where(Organization.name == f"{TEST_ORG_PREFIX}Итоги")
        )
    ).scalar_one()
    people = (
        await session.execute(select(User).where(User.organization_id == org_id))
    ).scalars().all()
    return {p.full_name.split()[-1].lower(): p for p in people}


async def main() -> None:
    await cleanup()
    tg = 950_000_000

    print("\n1. Схема блока на месте")
    async with session_scope() as session:
        tables = {
            row[0] for row in (
                await session.execute(
                    text(
                        "select table_name from information_schema.tables "
                        "where table_schema='public'"
                    )
                )
            ).all()
        }
        expected = {
            "agenda_items", "decisions", "documents",
            "document_access", "document_texts", "document_views", "download_tokens",
        }
        check(expected <= tables, "семь таблиц блока созданы", f"нет: {sorted(expected - tables)}")

        # Индексы по выражению autogenerate не создаёт — их проверяем явно,
        # иначе поиск тихо уедет на последовательное чтение.
        indexes = {
            row[0] for row in (
                await session.execute(text("select indexname from pg_indexes"))
            ).all()
        }
        needed = {
            "ix_decisions_search", "ix_meetings_search", "ix_tasks_search",
            "ix_document_texts_search", "ix_documents_name_trgm",
        }
        check(needed <= indexes, "поисковые индексы существуют", f"нет: {sorted(needed - indexes)}")

        # На пустой таблице планировщик всегда выберет последовательное чтение,
        # поэтому «или индекс, или Seq Scan» не доказывало бы ничего. Отключаем
        # Seq Scan и требуем именно индекс: так проверяется то, что нужно —
        # что индекс подходит под форму запроса, а не просто существует.
        await session.execute(text("set local enable_seqscan = off"))
        probes = {
            "решения": (
                "explain select id from decisions where "
                "to_tsvector('russian', title || ' ' || coalesce(details, '')) "
                "@@ plainto_tsquery('russian', 'закупки')",
                "ix_decisions_search",
            ),
            "встречи": (
                "explain select id from meetings where "
                "to_tsvector('russian', title || ' ' || coalesce(description, '')) "
                "@@ plainto_tsquery('russian', 'совещание')",
                "ix_meetings_search",
            ),
            "поручения": (
                "explain select id from tasks where "
                "to_tsvector('russian', title || ' ' || coalesce(description, '')) "
                "@@ plainto_tsquery('russian', 'отчёт')",
                "ix_tasks_search",
            ),
            "тексты документов": (
                "explain select id from document_texts where "
                "search_vector @@ plainto_tsquery('russian', 'договор')",
                "ix_document_texts_search",
            ),
        }
        for label, (sql, index_name) in probes.items():
            plan = " | ".join(
                str(row[0]) for row in (await session.execute(text(sql))).all()
            )
            check(index_name in plan, f"поиск по «{label}» опирается на индекс", plan[:120])

    async with session_scope() as session:
        await bootstrap(session)
        org = Organization(name=f"{TEST_ORG_PREFIX}Итоги", timezone="Asia/Tashkent")
        session.add(org)
        await session.flush()
        dept = Department(organization_id=org.id, name="ТЕСТ отдел")
        session.add(dept)
        await session.flush()

        chief = await person(session, org, "Тест Руководитель", RoleCode.EXECUTIVE, tg + 1)
        helper = await person(session, org, "Тест Ассистент", RoleCode.ASSISTANT, tg + 2)
        head = await person(session, org, "Тест Начальник", RoleCode.DEPT_HEAD, tg + 3, dept.id)
        worker = await person(session, org, "Тест Сотрудник", RoleCode.EMPLOYEE, tg + 4, dept.id)

        other = Organization(name=f"{TEST_ORG_PREFIX}Чужая", timezone="Asia/Tashkent")
        session.add(other)
        await session.flush()
        await person(session, other, "Тест Чужой", RoleCode.EXECUTIVE, tg + 9)

        meeting = Meeting(
            organization_id=org.id, owner_id=chief.id, title="ТЕСТ совещание",
            start_at=at(MONDAY, 10), end_at=at(MONDAY, 11),
            status=MeetingStatus.CONFIRMED, created_by=chief.id,
        )
        session.add(meeting)
        await session.flush()
        for uid, role in (
            (chief.id, ParticipantRole.ORGANIZER),
            (worker.id, ParticipantRole.REQUIRED),
            (head.id, ParticipantRole.REQUIRED),
        ):
            session.add(MeetingParticipant(
                meeting_id=meeting.id, user_id=uid, role=role, created_at=utcnow()
            ))
        await session.flush()
        meeting_id = meeting.id

    print("\n2. Повестка")
    async with session_scope() as session:
        who = await cast(session)
        meeting = await session.get(Meeting, meeting_id)

        short = await registry.add_agenda_item(
            session, meeting=meeting, actor=who["руководитель"], title="ок"
        )
        check(not short.ok, "пункт в два знака не принимается", short.reason or "")

        denied = await registry.add_agenda_item(
            session, meeting=meeting, actor=who["сотрудник"], title="ТЕСТ мой пункт"
        )
        check(not denied.ok, "сотрудник повестку не ведёт", denied.reason or "")

        for title in ("ТЕСТ закупки", "ТЕСТ склад", "ТЕСТ кадры"):
            added = await registry.add_agenda_item(
                session, meeting=meeting, actor=who["руководитель"], title=title
            )
            check(added.ok, f"пункт «{title}» добавлен", added.reason or "")

        items = await registry.agenda_of(session, meeting)
        check(
            [i.title for i in items] == ["ТЕСТ закупки", "ТЕСТ склад", "ТЕСТ кадры"],
            "порядок повестки сохранён",
            f"{[i.title for i in items]}",
        )
        check(
            [i.position for i in items] == [1, 2, 3],
            "позиции проставлены подряд",
            f"{[i.position for i in items]}",
        )

        covered = await registry.mark_covered(
            session, item=items[0], meeting=meeting, actor=who["ассистент"]
        )
        check(covered.ok, "ассистент отмечает пункт рассмотренным", covered.reason or "")
        check(items[0].covered_at is not None, "и время отметки записано")

        by_worker = await registry.mark_covered(
            session, item=items[1], meeting=meeting, actor=who["сотрудник"]
        )
        check(not by_worker.ok, "сотрудник пункты не отмечает", by_worker.reason or "")

    print("\n3. Завершение встречи")
    async with session_scope() as session:
        who = await cast(session)
        meeting = await session.get(Meeting, meeting_id)

        early = await meeting_service.finish(
            session, meeting=meeting, actor=who["руководитель"], now=at(MONDAY, 9)
        )
        check(not early.ok, "до начала встречу не завершить", early.reason or "")

        stranger = await meeting_service.finish(
            session, meeting=meeting, actor=who["сотрудник"], now=at(MONDAY, 11, 30)
        )
        check(not stranger.ok, "участник чужую встречу не завершает", stranger.reason or "")
        check(meeting.status == MeetingStatus.CONFIRMED, "и встреча всё ещё идёт")

        done = await meeting_service.finish(
            session, meeting=meeting, actor=who["руководитель"], now=at(MONDAY, 11, 30)
        )
        check(done.ok, "руководитель завершает встречу", done.reason or "")
        check(meeting.status == MeetingStatus.FINISHED, "состояние — завершена")
        check(meeting.finished_at is not None, "время завершения записано")

        again = await meeting_service.finish(
            session, meeting=meeting, actor=who["руководитель"], now=at(MONDAY, 12)
        )
        check(not again.ok, "повторное завершение ничего не меняет", again.reason or "")

        late_item = await registry.add_agenda_item(
            session, meeting=meeting, actor=who["руководитель"], title="ТЕСТ поздний пункт"
        )
        check(not late_item.ok, "в завершённую встречу пункт не добавить", late_item.reason or "")

    print("\n4. Реестр решений")
    async with session_scope() as session:
        who = await cast(session)
        meeting = await session.get(Meeting, meeting_id)
        items = await registry.agenda_of(session, meeting)

        empty = await registry.create(session, actor=who["руководитель"], title=" ")
        check(not empty.ok, "решение без формулировки не создаётся", empty.reason or "")

        by_worker = await registry.create(
            session, actor=who["сотрудник"], title="ТЕСТ решение сотрудника"
        )
        check(not by_worker.ok, "сотрудник решений не вносит", by_worker.reason or "")

        first = await registry.create(
            session, actor=who["руководитель"], title="ТЕСТ закупить стеллажи",
            details="Три штуки до конца месяца", meeting=meeting, agenda_item=items[0],
            responsible=who["сотрудник"], due_date=at(MONDAY + timedelta(days=14), 18),
        )
        check(first.ok, "решение внесено", first.reason or "")
        check(first.item.status == DecisionStatus.OPEN, "и оно в работе")
        check(first.item.meeting_id == meeting_id, "связано со встречей")

        loose = await registry.create(
            session, actor=who["руководитель"], title="ТЕСТ решение без встречи"
        )
        check(loose.ok, "решение без встречи тоже вносится", loose.reason or "")
        check(loose.item.meeting_id is None, "и живёт само по себе")

        outsider = (
            await session.execute(
                select(User).join(Organization, Organization.id == User.organization_id)
                .where(Organization.name == f"{TEST_ORG_PREFIX}Чужая")
            )
        ).scalar_one()
        cross = await registry.create(
            session, actor=outsider, title="ТЕСТ чужое решение", meeting=meeting
        )
        check(not cross.ok, "к чужой встрече решение не привязать", cross.reason or "")

        decision_id = first.item.id
        loose_id = loose.item.id

    print("\n5. Закрытие и отмена решения")
    async with session_scope() as session:
        who = await cast(session)
        decision = await session.get(Decision, decision_id)
        loose = await session.get(Decision, loose_id)

        by_worker = await registry.close(
            session, decision=decision, actor=who["сотрудник"], done=True
        )
        check(not by_worker.ok, "сотрудник решение не закрывает", by_worker.reason or "")

        by_head = await registry.close(
            session, decision=decision, actor=who["начальник"], done=True
        )
        check(not by_head.ok, "начальник отдела — тоже нет", by_head.reason or "")

        no_reason = await registry.close(
            session, decision=loose, actor=who["руководитель"], done=False, reason="  "
        )
        check(not no_reason.ok, "отмена без причины не принимается", no_reason.reason or "")

        closed = await registry.close(
            session, decision=decision, actor=who["руководитель"], done=True
        )
        check(closed.ok, "руководитель закрывает решение", closed.reason or "")
        check(decision.status == DecisionStatus.DONE, "состояние — выполнено")

        twice = await registry.close(
            session, decision=decision, actor=who["руководитель"], done=True
        )
        check(not twice.ok, "повторное закрытие ничего не меняет", twice.reason or "")

        killed = await registry.close(
            session, decision=loose, actor=who["ассистент"], done=False,
            reason="Отпало по итогам совещания",
        )
        check(killed.ok, "ассистент отменяет решение с причиной", killed.reason or "")
        check(loose.status == DecisionStatus.CANCELLED, "состояние — отменено")
        check(
            loose.cancel_reason == "Отпало по итогам совещания",
            "причина отмены сохранена",
        )

        still_there = await session.scalar(
            select(func.count(Decision.id)).where(Decision.id.in_([decision_id, loose_id]))
        )
        check(still_there == 2, "обе строки остались в реестре", f"их {still_there}")

    print("\n6. Кто какие решения видит")
    async with session_scope() as session:
        who = await cast(session)
        chief, worker, head = who["руководитель"], who["сотрудник"], who["начальник"]

        seen_by_chief = await registry.registry(
            session, user=chief, grants=await load_grants(session, chief), limit=50
        )
        check(len(seen_by_chief) == 2, "руководитель видит оба решения", f"их {len(seen_by_chief)}")

        seen_by_worker = await registry.registry(
            session, user=worker, grants=await load_grants(session, worker), limit=50
        )
        check(
            [d.id for d in seen_by_worker] == [decision_id],
            "сотрудник видит только то, где он ответственный",
            f"{[d.id for d in seen_by_worker]}",
        )

        seen_by_head = await registry.registry(
            session, user=head, grants=await load_grants(session, head), limit=50
        )
        check(
            decision_id in [d.id for d in seen_by_head],
            "начальник отдела видит решение своего сотрудника",
            f"{[d.id for d in seen_by_head]}",
        )
        check(
            loose_id not in [d.id for d in seen_by_head],
            "и не видит решение без отношения к его отделу",
        )

        outsider = (
            await session.execute(
                select(User).join(Organization, Organization.id == User.organization_id)
                .where(Organization.name == f"{TEST_ORG_PREFIX}Чужая")
            )
        ).scalar_one()
        seen_by_outsider = await registry.registry(
            session, user=outsider, grants=await load_grants(session, outsider), limit=50
        )
        check(not seen_by_outsider, "чужая организация не видит ничего", f"{len(seen_by_outsider)}")

        meeting = await session.get(Meeting, meeting_id)
        made_decisions, made_tasks = await registry.meeting_outcome(session, meeting)
        check(made_decisions == 1, "встреча знает своё решение", f"их {made_decisions}")
        check(made_tasks == 0, "поручений из неё пока нет", f"их {made_tasks}")

    print("\n7. Уборка не трогает боевые данные")
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
        left = await session.scalar(select(func.count(Decision.id)))
    check(real_after == real_before, "настоящие сотрудники не удалены", f"{real_before} → {real_after}")
    check(left == 0, "тестовые решения убраны", f"осталось {left}")

    print(f"\n{'=' * 50}\nПройдено: {passed}   Ошибок: {failed}\n{'=' * 50}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
