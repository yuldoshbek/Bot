"""Проверка каркаса ИИ: четыре границы из архитектуры.

    docker compose -f docker-compose.dev.yml \
      run --rm --no-deps migrate python scripts/smoke_ai.py

Проверяется не «работает ли модель» — модели здесь нет вовсе, и это главное
свойство набора: **все сценарии идут на подставном поставщике**. Понадобилась
бы хоть одному настоящая сеть — значит, слой подмены дырявый.

Границы, ради которых блок затевался:

1. ИИ не пишет в базу — после вызова в таблицах поручений и решений пусто.
2. Бюджет с жёстким потолком — при исчерпании вызова не происходит вовсе.
3. Расход записан до ответа — иначе обрыв не учитывается и потолок обходится.
4. Выключенный ИИ не ломает ничего — и это проверяется первым.
"""
import asyncio
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, func, select

from app.ai import gate, protocol, summary, voice
from app.ai.provider import Fake
from app.core.config import settings
from app.core.dates import parse_due
from app.core.db import session_scope
from app.core.timeutil import utcnow
from app.models import (
    AgendaItem,
    AiCall,
    AuditLog,
    Decision,
    Department,
    Meeting,
    MeetingParticipant,
    MeetingStatus,
    Notification,
    Organization,
    Priority,
    RoleCode,
    Task,
    TaskComment,
    TaskEvent,
    TaskExtension,
    User,
    UserRole,
    UserStatus,
    WorkingHours,
)
from app.services import decisions, digest
from app.services import tasks as task_service
from app.services.bootstrap import bootstrap, ensure_default_working_hours, grant_role
from app.services.rbac import load_grants
from app.services.tasks import TaskError, allowed_assignees, may_assign_to

ORG_NAME = "ТЕСТ ИИ"

# Точка отсчёта для сроков. Пришпилена намеренно: срок, посчитанный от часов
# машины и сверенный с настоящим «завтра», проходит один день и падает на
# следующий — такую проверку уже ловили в этом проекте.
NOW = datetime(2026, 9, 7, 9, 0)

# 07:30 по Ташкенту в тот же день — время, в которое уходит сводка.
# В UTC, как её подаёт фоновый цикл: местное время здесь скрыло бы ошибку
# «дата сервера вместо даты получателя».
MORNING = datetime(2026, 9, 7, 2, 30, tzinfo=timezone.utc)

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
    """Убирает за собой. Только по organization_id — чужого не трогаем никогда."""
    async with session_scope() as session:
        org_ids = list((await session.execute(
            select(Organization.id).where(Organization.name == ORG_NAME)
        )).scalars().all())
        if not org_ids:
            return
        user_ids = list((await session.execute(
            select(User.id).where(User.organization_id.in_(org_ids))
        )).scalars().all())
        task_ids = list((await session.execute(
            select(Task.id).where(Task.organization_id.in_(org_ids))
        )).scalars().all())

        await session.execute(delete(AiCall).where(AiCall.organization_id.in_(org_ids)))
        if task_ids:
            for model in (TaskEvent, TaskComment, TaskExtension):
                await session.execute(delete(model).where(model.task_id.in_(task_ids)))
            await session.execute(delete(Task).where(Task.id.in_(task_ids)))
        await session.execute(
            delete(Decision).where(Decision.organization_id.in_(org_ids))
        )
        meeting_ids = list((await session.execute(
            select(Meeting.id).where(Meeting.organization_id.in_(org_ids))
        )).scalars().all())
        if meeting_ids:
            await session.execute(
                delete(AgendaItem).where(AgendaItem.meeting_id.in_(meeting_ids))
            )
            await session.execute(delete(MeetingParticipant).where(
                MeetingParticipant.meeting_id.in_(meeting_ids)
            ))
            await session.execute(delete(Meeting).where(Meeting.id.in_(meeting_ids)))
        if user_ids:
            for model in (UserRole, WorkingHours, Notification):
                await session.execute(delete(model).where(model.user_id.in_(user_ids)))
            await session.execute(delete(AuditLog).where(AuditLog.actor_id.in_(user_ids)))
            await session.execute(delete(User).where(User.id.in_(user_ids)))
        await session.execute(
            delete(Department).where(Department.organization_id.in_(org_ids))
        )
        await session.execute(delete(Organization).where(Organization.id.in_(org_ids)))


@dataclass(slots=True)
class Cast:
    """Кто участвует в проверках. Роли настоящие: голосовое поручение обязано
    упереться в те же права, что и набранное руками."""

    org: int
    chief: int       # руководитель — поручает кому угодно
    worker: int      # сотрудник — область «только свои»
    outsider: int    # сотрудник другого отдела
    twins: int       # два однофамильца: имя, по которому нельзя выбрать


async def seed() -> Cast:
    async with session_scope() as session:
        await bootstrap(session)
        org = Organization(name=ORG_NAME, timezone="Asia/Tashkent")
        session.add(org)
        await session.flush()

        finance = Department(organization_id=org.id, name="ТЕСТ Moliya")
        projects = Department(organization_id=org.id, name="ТЕСТ Loyihalar")
        session.add_all([finance, projects])
        await session.flush()

        async def person(name, role, department=None, tg=0) -> User:
            item = User(
                organization_id=org.id, telegram_user_id=tg, full_name=name,
                status=UserStatus.ACTIVE, timezone="Asia/Tashkent", locale="uz",
                department_id=department.id if department else None,
            )
            session.add(item)
            await session.flush()
            await ensure_default_working_hours(session, item)
            await grant_role(session, item, role)
            return item

        chief = await person("ТЕСТ Rahimov Rahbar", RoleCode.EXECUTIVE, tg=993_001)
        worker = await person("ТЕСТ Karimov Ijrochi", RoleCode.EMPLOYEE, finance, 993_002)
        outsider = await person("ТЕСТ Yusupov Boshqa", RoleCode.EMPLOYEE, projects, 993_003)
        # Однофамильцы: по фамилии выбрать нельзя, и система обязана это сказать,
        # а не взять первого попавшегося.
        await person("ТЕСТ Salimov Bir", RoleCode.EMPLOYEE, finance, 993_004)
        await person("ТЕСТ Salimov Ikki", RoleCode.EMPLOYEE, finance, 993_005)
        return Cast(org.id, chief.id, worker.id, outsider.id, 2)


async def main() -> None:
    await cleanup()
    cast = await seed()
    org_id, user_id = cast.org, cast.chief
    was_enabled = settings.ai_enabled
    try:
        await stage_off(org_id, user_id)
        await stage_journal(org_id, user_id)
        await stage_budget(org_id, user_id)
        await stage_no_writes(org_id, user_id)
        await stage_provider(org_id, user_id)
        stage_payload()
        await stage_draft(cast)
        await stage_due(cast)
        await stage_confirm(cast)
        await stage_rights(cast)
        await stage_digest(cast)
        await stage_protocol(cast)
    finally:
        settings.ai_enabled = was_enabled
        gate.use(Fake())

    await cleanup()
    async with session_scope() as session:
        left = await session.scalar(
            select(Organization.id).where(Organization.name == ORG_NAME)
        )
    check(left is None, "тестовая организация убрана")

    print(f"\n{'=' * 50}\nПройдено: {passed}   Ошибок: {failed}\n{'=' * 50}")
    sys.exit(1 if failed else 0)


async def stage_off(org_id: int, user_id: int) -> None:
    print("\n1. Выключенный ИИ не обращается никуда")
    settings.ai_enabled = False
    fake = Fake(answers=["не должно прозвучать"])
    gate.use(fake)

    async with session_scope() as session:
        outcome = await gate.ask(
            session, organization_id=org_id, kind="test",
            system="s", user="u", prompt_version="v1",
        )
    check(not outcome.worked, "ответа нет", outcome.text[:40])
    check(outcome.reason == "off", "причина названа: выключен", outcome.reason)
    # Главное: обращения не было вовсе, а не было и проигнорировано.
    check(fake.calls == 0, "и поставщика никто не звал", f"обращений: {fake.calls}")

    async with session_scope() as session:
        rows = await session.scalar(
            select(func.count(AiCall.id)).where(AiCall.organization_id == org_id)
        )
    check(rows == 0, "в журнале ничего не появилось", str(rows))


async def stage_journal(org_id: int, user_id: int) -> None:
    print("\n2. Журнал: шесть полей на каждый вызов")
    settings.ai_enabled = True
    gate.use(Fake(answers=["Три встречи, две просрочки."]))

    async with session_scope() as session:
        outcome = await gate.ask(
            session, organization_id=org_id, kind="digest",
            system="ты помощник", user="цифры", prompt_version="digest-1",
            user_id=user_id, needs_confirmation=True,
        )
        check(outcome.worked, "ответ получен", outcome.reason)
        call = await session.get(AiCall, outcome.call_id)
        check(call is not None, "вызов записан в журнал")
        check(call.kind == "digest", "тип записан", call.kind)
        check(call.model == settings.ai_model_routine, "модель записана", call.model)
        check(call.prompt_version == "digest-1", "версия промпта записана",
              call.prompt_version)
        check(call.user_id == user_id, "инициатор записан", str(call.user_id))
        check(call.ok is True, "исход записан")
        check(call.finished_at is not None, "время завершения записано")
        # Подтверждения ещё не было: не None (требовалось) и не True.
        check(call.confirmed is False, "подтверждение ещё не получено",
              str(call.confirmed))

        await gate.mark_confirmed(session, outcome.call_id, confirmed=True)
        await session.refresh(call)
        check(call.confirmed is True, "и после подтверждения отмечено")

    # Сводке и отчёту подтверждение не требуется — там остаётся None.
    async with session_scope() as session:
        gate.use(Fake(answers=["итог"]))
        second = await gate.ask(
            session, organization_id=org_id, kind="weekly_report",
            system="s", user="u", prompt_version="report-1",
        )
        call = await session.get(AiCall, second.call_id)
        check(call.confirmed is None,
              "где подтверждение не нужно — отметки нет", str(call.confirmed))


async def stage_budget(org_id: int, user_id: int) -> None:
    print("\n3. Бюджет останавливает до обращения")
    settings.ai_enabled = True

    # Расход у самого предела. Число берётся из настройки, но предел проверяется
    # не им: ниже отдельно сверяется, что настройка вообще разумна.
    async with session_scope() as session:
        session.add(AiCall(
            organization_id=org_id, kind="test", model="m", prompt_version="v",
            cost_usd=Decimal(str(settings.ai_daily_budget_usd)), ok=True,
            started_at=utcnow() - timedelta(hours=1), finished_at=utcnow(),
        ))

    check(0 < settings.ai_daily_budget_usd <= 100,
          f"дневной предел разумен: ${settings.ai_daily_budget_usd}")
    check(settings.ai_daily_budget_usd <= settings.ai_monthly_budget_usd,
          "дневной предел не больше месячного")

    fake = Fake(answers=["не должно прозвучать"])
    gate.use(fake)
    async with session_scope() as session:
        outcome = await gate.ask(
            session, organization_id=org_id, kind="test",
            system="s", user="u", prompt_version="v1",
        )
    check(outcome.reason == "budget", "вызов остановлен по бюджету", outcome.reason)
    check(fake.calls == 0, "и поставщика не звали", f"обращений: {fake.calls}")

    # Отказ по бюджету не должен сам плодить строки в журнале.
    async with session_scope() as session:
        rows = await session.scalar(
            select(func.count(AiCall.id)).where(
                AiCall.organization_id == org_id, AiCall.kind == "test"
            )
        )
    # Единственная строка с типом test — та, что мы завели руками. Отказ
    # по бюджету своей строки не добавил.
    check(rows == 1, "отказ не записан как вызов", f"строк типа test: {rows}")

    # Месячный предел ловит то, что дневной пропускает: ровный расход,
    # который каждый день укладывается в дневную норму, но за месяц выходит
    # за месячную. Без отдельной проверки такой случай не встречается вовсе.
    async with session_scope() as session:
        await session.execute(
            delete(AiCall).where(AiCall.organization_id == org_id, AiCall.kind == "test")
        )
        # Расход за день берётся чуть ниже дневного предела, а число дней —
        # столько, чтобы перевалить месячный. Ровно 28 дней укладываются
        # в тридцатидневное окно и не задевают последние сутки.
        DAYS = 28
        per_day = settings.ai_daily_budget_usd * 0.9
        reachable = per_day * DAYS
        check(
            reachable > settings.ai_monthly_budget_usd,
            "месячный предел достижим при дневном расходе ниже дневного предела",
            f"за {DAYS} дней по ${per_day:.2f} = ${reachable:.2f}, "
            f"месячный предел ${settings.ai_monthly_budget_usd}",
        )
        for day in range(2, DAYS + 2):
            session.add(AiCall(
                organization_id=org_id, kind="test", model="m", prompt_version="v",
                cost_usd=Decimal(str(per_day)), ok=True,
                started_at=utcnow() - timedelta(days=day),
                finished_at=utcnow() - timedelta(days=day),
            ))

    async with session_scope() as session:
        today = await gate.spent(session, org_id, since=utcnow() - timedelta(days=1))
        allowed, why = await gate.budget_left(session, org_id)
    check(today < settings.ai_daily_budget_usd,
          f"за сутки потрачено меньше дневного предела: ${today:.2f}")
    check(not allowed and why == "месячный предел",
          "но месячный предел исчерпан и ИИ остановлен", f"{allowed}, {why}")

    fake_month = Fake(answers=["не должно прозвучать"])
    gate.use(fake_month)
    async with session_scope() as session:
        outcome = await gate.ask(
            session, organization_id=org_id, kind="test",
            system="s", user="u", prompt_version="v1",
        )
    check(outcome.reason == "budget", "вызов остановлен и по месячному пределу",
          outcome.reason)
    check(fake_month.calls == 0, "и поставщика снова не звали",
          f"обращений: {fake_month.calls}")

    # Чужая организация своим расходом не связана.
    async with session_scope() as session:
        other = Organization(name=ORG_NAME, timezone="Asia/Tashkent")
        session.add(other)
        await session.flush()
        allowed, _ = await gate.budget_left(session, other.id)
        check(allowed, "расход одной организации не закрывает ИИ другой")

    # Освобождаем предел для следующих разделов.
    async with session_scope() as session:
        await session.execute(
            delete(AiCall).where(AiCall.organization_id == org_id, AiCall.kind == "test")
        )


async def stage_no_writes(org_id: int, user_id: int) -> None:
    print("\n4. ИИ не пишет в базу")
    settings.ai_enabled = True
    # Ответ, который очень похож на команду завести поручение и решение.
    gate.use(Fake(answers=[
        "Создать поручение: подготовить смету, исполнитель Иванов, срок пятница. "
        "Записать решение: закупку одобрить."
    ]))

    async with session_scope() as session:
        before_tasks = await session.scalar(
            select(func.count(Task.id)).where(Task.organization_id == org_id)
        )
        before_decisions = await session.scalar(
            select(func.count(Decision.id)).where(Decision.organization_id == org_id)
        )
        outcome = await gate.ask(
            session, organization_id=org_id, kind="voice_task",
            system="s", user="наговорённое", prompt_version="voice-1",
            user_id=user_id, needs_confirmation=True,
        )
        check(outcome.worked, "модель ответила", outcome.reason)
        after_tasks = await session.scalar(
            select(func.count(Task.id)).where(Task.organization_id == org_id)
        )
        after_decisions = await session.scalar(
            select(func.count(Decision.id)).where(Decision.organization_id == org_id)
        )
    check(after_tasks == before_tasks,
          "поручений не прибавилось", f"{before_tasks} → {after_tasks}")
    check(after_decisions == before_decisions,
          "и решений тоже", f"{before_decisions} → {after_decisions}")


async def stage_provider(org_id: int, user_id: int) -> None:
    print("\n5. Отказ поставщика не ломает вызывающего")
    settings.ai_enabled = True
    gate.use(Fake(fail=True))

    async with session_scope() as session:
        outcome = await gate.ask(
            session, organization_id=org_id, kind="test",
            system="s", user="u", prompt_version="v1",
        )
        check(not outcome.worked, "ответа нет")
        check(outcome.reason == "provider", "причина названа: поставщик",
              outcome.reason)
        # Строка обязана остаться: обращение было, оно оплачено.
        call = await session.get(AiCall, outcome.call_id)
        check(call is not None, "но вызов записан — обращение было оплачено")
        check(call.ok is False and call.error, "и отказ записан с причиной",
              str(call.error)[:40])

    # Пустой ответ — не успех.
    gate.use(Fake(answers=["   "]))
    async with session_scope() as session:
        outcome = await gate.ask(
            session, organization_id=org_id, kind="test",
            system="s", user="u", prompt_version="v1",
        )
    check(outcome.reason == "empty", "пустой ответ не считается успехом",
          outcome.reason)

    # Слой подмены: ни один сценарий этого набора не ходил в сеть.
    check(gate.current().name == "fake",
          "все проверки прошли на подставном поставщике", gate.current().name)


async def count_tasks(org_id: int) -> int:
    async with session_scope() as session:
        return await session.scalar(
            select(func.count(Task.id)).where(Task.organization_id == org_id)
        )


def stage_payload() -> None:
    print("\n6. Ответ модели разбирается по заранее заданным полям")
    # Поля перечислены в коде. Всё, что модель придумала сверх списка,
    # не должно доехать никуда: это и есть «структура, а не текст запроса».
    clean = voice.payload(
        '{"title":"Смету","assignee":"Karimov","due":"ertaga","priority":"high"}'
    )
    check(clean.get("title") == "Смету", "название разобрано", str(clean))
    check(clean.get("due") == "ertaga", "фрагмент срока разобран", str(clean))

    fenced = voice.payload('Готово:\n```json\n{"title":"А","due":"завтра"}\n```\nвсё')
    check(fenced == {"title": "А", "due": "завтра"},
          "JSON достаётся из разметки и болтовни вокруг", str(fenced))

    extra = voice.payload('{"title":"А","secret":"1","sql":"DROP TABLE tasks"}')
    check(extra == {"title": "А"}, "поля вне списка отброшены", str(extra))

    check(voice.payload("совсем не json") == {},
          "ответ не по форме — пустота, а не исключение")
    check(voice.payload('["a","b"]') == {}, "не словарь — пустота")
    check(voice.payload("") == {}, "пустой ответ — пустота")

    long = voice.payload('{"title":"' + "я" * 900 + '"}')
    check(len(long.get("title", "")) <= 400, "слишком длинное поле обрезано",
          str(len(long.get("title", ""))))

    # Приоритет — из перечня, а не из фантазии: незнакомое слово даёт обычный.
    check(voice.PRIORITIES.get("critical") == Priority.CRITICAL,
          "перечень приоритетов на месте")
    check("выдумка" not in voice.PRIORITIES, "выдуманного приоритета в перечне нет")


async def stage_draft(cast: Cast) -> None:
    print("\n7. Черновик не создаёт поручения")
    settings.ai_enabled = True
    gate.use(Fake(answers=[
        '{"title":"Smeta tayyorlash","assignee":"Karimov",'
        '"due":"ertaga","priority":"high"}'
    ]))
    spoken = "Karimovga smeta tayyorlashni topshir, ertaga"

    before = await count_tasks(cast.org)
    async with session_scope() as session:
        chief = await session.get(User, cast.chief)
        grants = await load_grants(session, chief)
        draft = await voice.draft(
            session, spoken, creator=chief, grants=grants, now=NOW
        )
        call = await session.get(AiCall, draft.call_id)
        check(call is not None and call.kind == "voice_task",
              "вызов записан как голосовое поручение")
        check(call.prompt_version == voice.PROMPT_VERSION,
              "версия промпта записана", str(call.prompt_version))
        check(call.confirmed is False,
              "подтверждения ещё не было", str(call.confirmed))
    after = await count_tasks(cast.org)

    check(after == before, "поручений не прибавилось", f"{before} → {after}")
    check(draft.title == "Smeta tayyorlash", "название взято из разбора", draft.title)
    check(draft.assignee_id == cast.worker, "исполнителя нашла система",
          str(draft.assignee_id))
    check(draft.priority == Priority.HIGH, "приоритет разобран", str(draft.priority))
    check(draft.ready, "черновик готов к подтверждению", str(draft.notes))

    print("\n7.1. Названного человека ищет система, а не модель")
    gate.use(Fake(answers=['{"title":"Ish","assignee":"Salimov"}']))
    async with session_scope() as session:
        chief = await session.get(User, cast.chief)
        grants = await load_grants(session, chief)
        many = await voice.draft(
            session, "Salimovga topshir", creator=chief, grants=grants, now=NOW
        )
    check(many.assignee_id is None, "по однофамильцам никто не выбран",
          str(many.assignee_id))
    check("voice.note.assignee_many" in many.notes, "и сказано почему",
          str(many.notes))
    check(not many.ready, "такой черновик к записи не готов")

    gate.use(Fake(answers=['{"title":"Ish","assignee":"Petrov"}']))
    async with session_scope() as session:
        chief = await session.get(User, cast.chief)
        grants = await load_grants(session, chief)
        unknown = await voice.draft(
            session, "Petrovga topshir", creator=chief, grants=grants, now=NOW
        )
    check(unknown.assignee_id is None, "выдуманного человека система не завела")
    check("voice.note.assignee_unknown" in unknown.notes, "и сказано, что не нашла",
          str(unknown.notes))

    print("\n7.2. Выключенный ИИ не ломает разбор — он его упрощает")
    settings.ai_enabled = False
    fake = Fake(answers=['{"title":"не должно прозвучать"}'])
    gate.use(fake)
    async with session_scope() as session:
        chief = await session.get(User, cast.chief)
        grants = await load_grants(session, chief)
        plain = await voice.draft(
            session, "Ertaga hisobot tayyorlash", creator=chief, grants=grants, now=NOW
        )
    check(fake.calls == 0, "модель не звали", str(fake.calls))
    check(plain.title == "Ertaga hisobot tayyorlash", "речь стала названием как есть",
          plain.title)
    check("voice.note.raw" in plain.notes, "и об этом сказано", str(plain.notes))
    # Главное: срок посчитан всё равно — считает его система, а не модель.
    check(plain.due_at == parse_due("ertaga", "Asia/Tashkent", now=NOW),
          "срок посчитан без ИИ", str(plain.due_at))
    settings.ai_enabled = True


async def stage_due(cast: Cast) -> None:
    print("\n8. Срок считает parse_due, а не модель")
    settings.ai_enabled = True

    async def draft_of(answer: str, spoken: str):
        gate.use(Fake(answers=[answer]))
        async with session_scope() as session:
            chief = await session.get(User, cast.chief)
            grants = await load_grants(session, chief)
            return await voice.draft(
                session, spoken, creator=chief, grants=grants, now=NOW
            )

    # Модель подсовывает вычисленную дату вместо фрагмента речи. Взять её
    # значило бы завести второе описание сроков — и вот оно уже врёт.
    lying = await draft_of(
        '{"title":"Hisobot","assignee":"Karimov","due":"2026-12-31"}',
        "Karimovga hisobot, ertaga",
    )
    check(lying.due_at == parse_due("ertaga", "Asia/Tashkent", now=NOW),
          "срок взят из речи, а не из ответа модели", str(lying.due_at))
    check(lying.due_at.month == 9, "декабрь из ответа модели не попал в срок",
          str(lying.due_at))

    # Честный ответ: фрагмент речи. Результат обязан совпасть с тем, что дал бы
    # тот же разбор на том же тексте — это и есть «одна функция на два входа».
    for phrase, spoken in (
        ("ertaga", "Karimovga hisobot, ertaga"),
        ("juma gacha", "Karimovga hisobot, juma gacha"),
        ("через три дня", "Каримову отчёт, через три дня"),
    ):
        item = await draft_of(
            '{"title":"Hisobot","assignee":"Karimov","due":"%s"}' % phrase, spoken
        )
        expected = parse_due(phrase, "Asia/Tashkent", now=NOW)
        check(item.due_at == expected, f"«{phrase}»: срок совпал с parse_due",
              f"{item.due_at} vs {expected}")

    # Сверка с посчитанной датой, а не с той же функцией. Сравнение
    # `parse_due` с `parse_due` молчало бы даже тогда, когда разбор перестал
    # понимать срок вовсе: обе стороны дали бы None и сошлись.
    tomorrow = parse_due("ertaga", "Asia/Tashkent", now=NOW)
    check(tomorrow is not None and tomorrow.date() == (NOW + timedelta(days=1)).date(),
          "«ertaga» — это завтра, а не пустота", str(tomorrow))

    # Числительные словами: продиктованный срок звучит словами почти всегда,
    # и расшифровка записывает их словами же.
    three = parse_due("через три дня", "Asia/Tashkent", now=NOW)
    check(three is not None and three.date() == (NOW + timedelta(days=3)).date(),
          "«через три дня» разобрано в дату", str(three))
    check(parse_due("uch kundan keyin", "Asia/Tashkent", now=NOW) == three,
          "и по-узбекски — та же дата")
    check(parse_due("икки ҳафта ичида", "Asia/Tashkent", now=NOW) is not None,
          "и кириллицей тоже")

    # Срок не назван вовсе — это не ошибка, а поручение без срока.
    none = await draft_of('{"title":"Hisobot","assignee":"Karimov"}', "Karimovga hisobot")
    check(none.due_at is None, "без срока — без срока", str(none.due_at))
    check("voice.note.no_due" in none.notes, "и об этом сказано", str(none.notes))
    check(none.ready, "но записать такое поручение можно")


async def stage_confirm(cast: Cast) -> None:
    print("\n9. Подтверждение создаёт ровно одно, отказ — ни одного")
    settings.ai_enabled = True
    answer = ('{"title":"Smeta tayyorlash","assignee":"Karimov",'
              '"due":"ertaga","priority":"normal"}')
    spoken = "Karimovga smeta tayyorlashni topshir, ertaga"

    print("\n9.1. Отказ")
    gate.use(Fake(answers=[answer]))
    before = await count_tasks(cast.org)
    async with session_scope() as session:
        chief = await session.get(User, cast.chief)
        grants = await load_grants(session, chief)
        draft = await voice.draft(session, spoken, creator=chief, grants=grants, now=NOW)
        await voice.decline(session, draft)
        call = await session.get(AiCall, draft.call_id)
        check(call.confirmed is False, "в журнале записан отказ", str(call.confirmed))
    after = await count_tasks(cast.org)
    check(after == before, "после отказа не создано ничего", f"{before} → {after}")

    print("\n9.2. Подтверждение")
    gate.use(Fake(answers=[answer]))
    async with session_scope() as session:
        chief = await session.get(User, cast.chief)
        grants = await load_grants(session, chief)
        draft = await voice.draft(session, spoken, creator=chief, grants=grants, now=NOW)
        task = await voice.confirm(session, draft, creator=chief, grants=grants)
        task_id, call_id = task.id, draft.call_id
    created = await count_tasks(cast.org)
    check(created == before + 1, "создано ровно одно поручение",
          f"{before} → {created}")

    async with session_scope() as session:
        task = await session.get(Task, task_id)
        call = await session.get(AiCall, call_id)
        check(task.assignee_id == cast.worker, "исполнитель тот, кого выбрала система")
        check(task.creator_id == cast.chief, "автор — человек, а не модель")
        check(task.due_at == parse_due("ertaga", "Asia/Tashkent", now=NOW),
              "срок тот же, что был в черновике", str(task.due_at))
        # Расшифровка остаётся в описании: спорить о формулировке потом
        # будут с ней, а не с пересказом модели.
        check(task.description == spoken, "расшифровка сохранена в описании",
              str(task.description))
        check(call.confirmed is True, "в журнале записано подтверждение",
              str(call.confirmed))


async def stage_rights(cast: Cast) -> None:
    print("\n10. Чужого исполнителя отвергает система, а не модель")
    settings.ai_enabled = True

    # Рядовой сотрудник: право task.create есть, область — «только свои».
    gate.use(Fake(answers=['{"title":"Ish qilish","assignee":"Yusupov"}']))
    async with session_scope() as session:
        worker = await session.get(User, cast.worker)
        grants = await load_grants(session, worker)
        denied = await voice.draft(
            session, "Yusupovga topshir", creator=worker, grants=grants, now=NOW
        )
    check(denied.assignee_id is None, "названный человек не подставлен",
          str(denied.assignee_id))
    check("voice.note.assignee_denied" in denied.notes, "причина названа",
          str(denied.notes))
    check(not denied.ready, "и черновик к записи не готов")

    # Проверка не «всегда нет»: себе тот же человек поручить вправе.
    gate.use(Fake(answers=['{"title":"Ish qilish","assignee":"Karimov"}']))
    async with session_scope() as session:
        worker = await session.get(User, cast.worker)
        grants = await load_grants(session, worker)
        allowed = await voice.draft(
            session, "Karimovga topshir", creator=worker, grants=grants, now=NOW
        )
    check(allowed.assignee_id == cast.worker,
          "себе поручить можно — отказ не безусловный", str(allowed.assignee_id))

    print("\n10.2. Список и поштучная проверка — одно правило с двух сторон")
    # Голосовой черновик предлагает выбрать из списка, а записывает после
    # поштучной проверки. Разойдись они — в списке оказался бы человек,
    # которому поручить всё равно не дадут, и кнопка отказывала бы после нажатия.
    async with session_scope() as session:
        everyone = list((await session.execute(
            select(User).where(
                User.organization_id == cast.org, User.status == UserStatus.ACTIVE
            )
        )).scalars().all())
        for who, title in ((cast.chief, "руководителя"), (cast.worker, "сотрудника")):
            actor = await session.get(User, who)
            grants = await load_grants(session, actor)
            listed = {
                person.id
                for person in await allowed_assignees(session, actor=actor, grants=grants)
            }
            mismatch = [
                person.full_name
                for person in everyone
                if await may_assign_to(
                    session, actor=actor, grants=grants, assignee=person
                ) != (person.id in listed)
            ]
            check(not mismatch, f"у {title} список совпал с проверкой по одному",
                  str(mismatch))
            check(listed, f"и он не пуст у {title}", str(len(listed)))

    print("\n10.1. Право проверяется ещё раз при записи")
    # Между показом черновика и нажатием кнопки проходит время. Черновик живёт
    # вне базы, и защищает данные именно вторая проверка — при записи.
    before = await count_tasks(cast.org)
    denied.assignee_id = cast.outsider
    denied.title = "Подложенный исполнитель"
    refused = False
    async with session_scope() as session:
        worker = await session.get(User, cast.worker)
        grants = await load_grants(session, worker)
        try:
            await voice.confirm(session, denied, creator=worker, grants=grants)
        except TaskError:
            refused = True
    check(refused, "запись отвергнута проверкой права")
    after = await count_tasks(cast.org)
    check(after == before, "и поручения не появилось", f"{before} → {after}")

    # Тот же путь для кнопки выбора: список собран, но право проверяется снова.
    async with session_scope() as session:
        worker = await session.get(User, cast.worker)
        grants = await load_grants(session, worker)
        taken = await voice.pick(
            session, denied, person_id=cast.outsider, creator=worker, grants=grants
        )
        mine = await voice.pick(
            session, denied, person_id=cast.worker, creator=worker, grants=grants
        )
    check(not taken, "чужого из списка выбрать нельзя")
    check(mine and denied.assignee_id == cast.worker, "своего — можно")
    check(not [n for n in denied.notes if n.startswith("voice.note.assignee")],
          "и пояснение про исполнителя убрано", str(denied.notes))


async def _digest_of(cast: Cast, *, accents=None) -> tuple[str, object]:
    """Собирает сводку руководителя. Без `accents` — та же, что была до ИИ."""
    async with session_scope() as session:
        chief = await session.get(User, cast.chief)
        text, board = await digest.build_for(
            session, viewer=chief, now=MORNING, accents=accents
        )
    return text or "", board


async def stage_digest(cast: Cast) -> None:
    print("\n11. Утренняя сводка словами: цифры подставляет система")

    # День должен быть непустым: сводка «сегодня ничего» не рассылается вовсе.
    async with session_scope() as session:
        chief = await session.get(User, cast.chief)
        worker = await session.get(User, cast.worker)
        grants = await load_grants(session, chief)
        for title in ("ТЕСТ Просроченное первое", "ТЕСТ Просроченное второе"):
            await task_service.create_task(
                session, creator=chief, assignee=worker, title=title,
                due_at=MORNING - timedelta(days=3),
            )
        check(bool(grants), "права руководителя загружены")

    print("\n11.1. Выключенный ИИ даёт нынешнюю сводку слово в слово")
    settings.ai_enabled = False
    fake = Fake(answers=["не должно прозвучать"])
    gate.use(fake)
    plain, board = await _digest_of(cast)
    with_ai, _ = await _digest_of(cast, accents=summary.accents)
    check(plain, "сводка собралась", plain[:50])
    check(with_ai == plain, "письмо не изменилось ни на знак")
    check(fake.calls == 0, "и модель никто не звал", str(fake.calls))

    print("\n11.2. Метки берутся с того же экрана, что увидит человек")
    values = summary.facts(board)
    check(values.get("overdue") == str(board.overdue_total),
          "просрочки взяты с экрана", str(values.get("overdue")))
    name, count = board.overdue_by_department[0]
    check(values.get("overdue_top_count") == str(count),
          "и разбивка по отделу тоже", str(values.get("overdue_top_count")))
    check(values.get("overdue_top") == name, "название отдела не переписано",
          str(values.get("overdue_top")))
    check(all(token in summary.MEANING for token in values),
          "у каждой метки есть описание", str(set(values) - set(summary.MEANING)))
    # Чего сегодня нет, о том модель и не узнает: перечисленный ноль
    # она непременно упомянет.
    check("meetings" not in values or board.meetings_today,
          "нулевые значения в метки не попадают", str(values))

    print("\n11.3. Годный ответ: числа подставлены, метки не остались")
    settings.ai_enabled = True
    good = (
        "Bugun asosiy narsa — muddati oʻtgan topshiriqlar, ularning soni {overdue}.\n"
        "Eng koʻpi {overdue_top} boʻlimida: {overdue_top_count}.\n"
        "Kunni shulardan boshlagan maʼqul.\n"
        "Qolgan ishlar kutib tura oladi."
    )
    gate.use(Fake(answers=[good]))
    worded, _ = await _digest_of(cast, accents=summary.accents)
    check(worded != plain, "вступление появилось")
    check("{overdue}" not in worded and "{overdue_top}" not in worded,
          "метка в письме не осталась", worded[:200])
    check(str(board.overdue_total) in worded, "число просрочек подставлено")

    # Каждая цифра во вступлении — из посчитанного. Сверка идёт по строкам,
    # которых нет в обычной сводке: именно они и есть вступление.
    known = set(plain.splitlines())
    extra = [line for line in worded.splitlines() if line and line not in known]
    seen = {found for line in extra for found in re.findall(r"\d+", line)}
    allowed = {found for value in values.values() for found in re.findall(r"\d+", value)}
    check(seen, "во вступлении есть числа", str(extra)[:80])
    check(seen <= allowed, "и ни одного, которого никто не считал",
          str(seen - allowed))
    # Сама сводка при этом осталась целиком: вступление — добавка, а не замена.
    body = plain.split("\n", 2)[2]
    check(worded.endswith(body), "сводка с цифрами осталась нетронутой")

    print("\n11.4. Цифру от модели письмо не принимает")
    for name, answer in (
        ("цифра в ответе",
         "Bugun 7 ta topshiriq muddati oʻtgan.\nIkkinchi qator.\nUchinchi qator."),
        ("выдуманная метка",
         "Bugun {invented} ta ish bor.\nIkkinchi qator.\nUchinchi qator."),
        ("метка про то, чего сегодня нет",
         "Bugun {meetings} ta uchrashuv.\nIkkinchi qator.\nUchinchi qator."),
        ("одна строка вместо нескольких", "Hammasi yaxshi."),
        ("двадцать строк", "\n".join(f"Qator {chr(97 + i)}" for i in range(20))),
        ("пустой ответ", "   "),
    ):
        gate.use(Fake(answers=[answer]))
        got, _ = await _digest_of(cast, accents=summary.accents)
        check(got == plain, f"{name}: ушла обычная сводка", got[:70])

    print("\n11.5. Разметку от модели письмо не пропускает")
    gate.use(Fake(answers=[
        "<b>Diqqat</b> — {overdue} topshiriq.\nIkkinchi qator.\nUchinchi qator."
    ]))
    marked, _ = await _digest_of(cast, accents=summary.accents)
    check("&lt;b&gt;Diqqat&lt;/b&gt;" in marked, "теги экранированы",
          marked[:200])
    check("<b>Diqqat</b>" not in marked, "и в письмо как разметка не попали")

    print("\n11.6. Кириллица выводится правилом, а не просится у модели")
    async with session_scope() as session:
        chief = await session.get(User, cast.chief)
        chief.locale = "uz-Cyrl"
    gate.use(Fake(answers=[good]))
    cyrillic, _ = await _digest_of(cast, accents=summary.accents)
    async with session_scope() as session:
        chief = await session.get(User, cast.chief)
        chief.locale = "uz"
    check("boshlagan" not in cyrillic, "латиница из ответа модели не осталась",
          cyrillic[:200])
    check("бошлаган" in cyrillic, "строка переведена в кириллицу правилом",
          cyrillic[:200])
    check(str(board.overdue_total) in cyrillic,
          "и подстановка пережила смену письменности")

    print("\n11.7. Отказ модели не отменяет письма")
    gate.use(Fake(fail=True))
    broken, _ = await _digest_of(cast, accents=summary.accents)
    check(broken == plain, "сводка ушла прежней", broken[:70])

    async with session_scope() as session:
        last = (await session.execute(
            select(AiCall).where(
                AiCall.organization_id == cast.org, AiCall.kind == "digest"
            ).order_by(AiCall.id.desc()).limit(1)
        )).scalar_one_or_none()
        check(last is not None, "вызов записан в журнал")
        check(last.prompt_version == summary.PROMPT_VERSION,
              "с версией промпта сводки", str(last.prompt_version))
        # Сводку никто не подтверждает: подтверждения здесь не требуется,
        # и это не то же самое, что отказ.
        check(last.confirmed is None, "подтверждения не требовалось",
              str(last.confirmed))

    print("\n11.8. Разбор ответа отдельно от письма")
    ok = summary.usable("Bir qator.\nIkkinchi.\nUchinchi.", {})
    check(ok is not None, "три строки без меток годятся", str(ok))
    check(summary.usable("Bir 5 qator.\nIkki.\nUch.", {}) is None,
          "цифра делает ответ негодным")
    check(summary.usable("Bir {x}.\nIkki.\nUch.", {}) is None,
          "неизвестная метка делает ответ негодным")
    check(summary.usable("a" * 2000 + "\nIkki.\nUch.", {}) is None,
          "слишком длинный ответ негоден")
    # Вступление — добавка, а не часть экрана. Утверждение о самой отрисовке,
    # а не сравнение двух её вызовов между собой: экран, который всегда вставлял
    # бы пустое место, сравнение двух вызовов пропустило бы молча.
    from app.services import dashboard as board_render

    bare = board_render.render(board, locale="ru")
    check("\n\n\n" not in bare, "без вступления лишнего пустого места нет")
    check(board_render.render(board, locale="ru", intro=None) == bare,
          "отсутствующее вступление ничего не меняет")
    check(board_render.render(board, locale="ru", intro="") == bare,
          "и пустое тоже")
    check("ПРОБА" in board_render.render(board, locale="ru", intro="ПРОБА"),
          "а написанное — ставится")

    check(summary.fill("Bor {overdue} ta", {"overdue": "9"}) == "Bor 9 ta",
          "подстановка на месте")
    check(summary.fill("{who}", {"who": "<b>"}) == "&lt;b&gt;",
          "значение тоже экранируется")
    settings.ai_enabled = True


async def _meeting_with_agenda(cast: Cast) -> tuple[int, list[int]]:
    """Прошедшая встреча с повесткой из трёх пунктов. Один уже с решением."""
    async with session_scope() as session:
        chief = await session.get(User, cast.chief)
        meeting = Meeting(
            organization_id=cast.org, owner_id=cast.chief, created_by=cast.chief,
            title="ТЕСТ Haftalik yigʻilish",
            start_at=MORNING - timedelta(hours=3),
            end_at=MORNING - timedelta(hours=2),
            status=MeetingStatus.FINISHED,
        )
        session.add(meeting)
        await session.flush()
        for who in (cast.chief, cast.worker):
            session.add(MeetingParticipant(
                meeting_id=meeting.id, user_id=who, created_at=MORNING,
            ))
        points = []
        for number, title in enumerate(
            ("Byudjet holati", "Yangi ombor", "Xodimlar rejasi"), start=1
        ):
            point = AgendaItem(
                meeting_id=meeting.id, position=number, title=title,
                created_by=cast.chief,
            )
            session.add(point)
            await session.flush()
            points.append(point.id)

        # По третьему пункту решение уже записано: второй раз его предлагать
        # нельзя, и это отдельная проверка.
        done = await session.get(AgendaItem, points[2])
        await decisions.create(
            session, actor=chief, title="Xodimlar rejasi tasdiqlandi",
            meeting=meeting, agenda_item=done,
        )
        return meeting.id, points


async def _counts(org_id: int) -> tuple[int, int]:
    async with session_scope() as session:
        made = await session.scalar(
            select(func.count(Decision.id)).where(Decision.organization_id == org_id)
        )
        given = await session.scalar(
            select(func.count(Task.id)).where(Task.organization_id == org_id)
        )
    return int(made or 0), int(given or 0)


async def _build(cast: Cast, meeting_id: int):
    async with session_scope() as session:
        chief = await session.get(User, cast.chief)
        meeting = await session.get(Meeting, meeting_id)
        grants = await load_grants(session, chief)
        return await protocol.build(
            session, meeting=meeting, actor=chief, grants=grants, now=NOW
        )


async def stage_protocol(cast: Cast) -> None:
    print("\n12. Протокол встречи: подтверждение по одному")
    meeting_id, points = await _meeting_with_agenda(cast)

    print("\n12.1. Без ИИ черновик всё равно есть — из повестки")
    settings.ai_enabled = False
    fake = Fake(answers=["не должно прозвучать"])
    gate.use(fake)
    before = await _counts(cast.org)
    plain = await _build(cast, meeting_id)
    check(fake.calls == 0, "модель не звали", str(fake.calls))
    check(len(plain.items) == 2, "предложены два пункта без решения",
          str([item.title for item in plain.items]))
    check(all(item.kind == "decision" for item in plain.items),
          "и оба — решения")
    check(plain.items[0].title == "Byudjet holati",
          "название взято из повестки", plain.items[0].title)
    check(all(item.agenda_item_id in points[:2] for item in plain.items),
          "пункт с готовым решением второй раз не предложен",
          str([item.agenda_number for item in plain.items]))
    check(await _counts(cast.org) == before, "и в реестре не появилось ничего")

    print("\n12.2. Модель уточняет формулировки, но не выдумывает пунктов")
    settings.ai_enabled = True
    answer = json.dumps({"items": [
        {"agenda": 1, "kind": "decision",
         "title": "Byudjet oʻzgarishsiz qoldirilsin"},
        {"agenda": 2, "kind": "decision", "title": "Ombor qurilishi boshlansin"},
        {"agenda": 2, "kind": "task", "title": "Ombor smetasini tayyorlash",
         "responsible": "Karimov", "due": "juma gacha"},
        {"agenda": 3, "kind": "decision", "title": "Ikkinchi qaror"},
        {"agenda": 9, "kind": "decision", "title": "Boshqa yigʻilishdan"},
        {"agenda": 1, "kind": "выдумка", "title": "Notoʻgʻri tur"},
    ]}, ensure_ascii=False)
    gate.use(Fake(answers=[answer]))
    before = await _counts(cast.org)
    draft = await _build(cast, meeting_id)
    titles = [item.title for item in draft.items]

    check(draft.items[0].title == "Byudjet oʻzgarishsiz qoldirilsin",
          "формулировка модели заменила название пункта", draft.items[0].title)
    check("Boshqa yigʻilishdan" not in titles,
          "пункт с чужим номером повестки отброшен", str(titles))
    check("Ikkinchi qaror" not in titles,
          "второе решение по закрытому пункту не предложено", str(titles))
    check("Notoʻgʻri tur" not in titles, "неизвестный вид записи отброшен",
          str(titles))
    task_items = [item for item in draft.items if item.kind == "task"]
    check(len(task_items) == 1, "предложено одно поручение", str(len(task_items)))
    check(task_items[0].responsible_id == cast.worker,
          "исполнителя нашла система", str(task_items[0].responsible_id))
    check(task_items[0].due_at == parse_due("juma gacha", "Asia/Tashkent", now=NOW),
          "срок посчитан тем же разбором", str(task_items[0].due_at))
    check(await _counts(cast.org) == before,
          "и до подтверждения в реестре по-прежнему пусто")

    print("\n12.3. Подтверждение одного пункта создаёт ровно один")
    async with session_scope() as session:
        chief = await session.get(User, cast.chief)
        meeting = await session.get(Meeting, meeting_id)
        grants = await load_grants(session, chief)
        ok, reason = await protocol.take(
            session, draft, 0, meeting=meeting, actor=chief, grants=grants
        )
        check(ok, "пункт записан", reason)
    made, given = await _counts(cast.org)
    check((made, given) == (before[0] + 1, before[1]),
          "прибавилось ровно одно решение", f"{before} → {(made, given)}")
    check(draft.items[0].state == "taken", "пункт отмечен внесённым")
    check(all(item.state == "new" for item in draft.items[1:]),
          "остальные остались непринятыми — «принять всё» здесь нет",
          str([item.state for item in draft.items]))

    async with session_scope() as session:
        written = await session.get(Decision, draft.items[0].created_id)
        check(written is not None, "решение нашлось в реестре")
        check(written.meeting_id == meeting_id, "и привязано к встрече")
        check(written.agenda_item_id == points[0], "и к пункту повестки")
        check(written.title == "Byudjet oʻzgarishsiz qoldirilsin",
              "с той формулировкой, что человек видел", written.title)

    print("\n12.4. Повторное нажатие второй записи не делает")
    async with session_scope() as session:
        chief = await session.get(User, cast.chief)
        meeting = await session.get(Meeting, meeting_id)
        grants = await load_grants(session, chief)
        again, reason = await protocol.take(
            session, draft, 0, meeting=meeting, actor=chief, grants=grants
        )
    check(not again, "второй раз тот же пункт не записан")
    check(reason == "protocol.err.already", "и причина названа", reason)
    check(await _counts(cast.org) == (made, given), "в реестре ничего не прибавилось")

    print("\n12.5. Отклонённый пункт не остаётся нигде")
    async with session_scope() as session:
        chief = await session.get(User, cast.chief)
        meeting = await session.get(Meeting, meeting_id)
        grants = await load_grants(session, chief)
        dropped = await protocol.drop(session, draft, 1)
        check(dropped, "пункт отклонён")
        after_drop = await protocol.take(
            session, draft, 1, meeting=meeting, actor=chief, grants=grants
        )
    check(draft.items[1].state == "dropped", "отметка стоит",
          draft.items[1].state)
    check(draft.items[1].created_id is None, "записи ему не соответствует")
    check(not after_drop[0], "отклонённый пункт записать нельзя", after_drop[1])
    check(await _counts(cast.org) == (made, given), "и реестр не изменился")

    print("\n12.6. Право поручать проверяет система, а не модель")
    gate.use(Fake(answers=[json.dumps({"items": [
        {"agenda": 1, "kind": "task", "title": "Yusupovga topshiriq",
         "responsible": "Yusupov"},
    ]}, ensure_ascii=False)]))
    async with session_scope() as session:
        worker = await session.get(User, cast.worker)
        meeting = await session.get(Meeting, meeting_id)
        grants = await load_grants(session, worker)
        low = await protocol.build(
            session, meeting=meeting, actor=worker, grants=grants, now=NOW
        )
        outsider_items = [
            item for item in low.items
            if item.kind == "task" and item.heard_name == "Yusupov"
        ]
        check(outsider_items, "поручение предложено", str(len(low.items)))
        check(outsider_items[0].responsible_id is None,
              "но исполнитель не подставлен",
              str(outsider_items[0].responsible_id))
        check("protocol.note.cannot_assign" in outsider_items[0].notes,
              "и причина названа", str(outsider_items[0].notes))

        index = low.items.index(outsider_items[0])
        refused = await protocol.take(
            session, low, index, meeting=meeting, actor=worker, grants=grants
        )
    check(not refused[0], "записать такое поручение нельзя", refused[1])
    check(await _counts(cast.org) == (made, given), "и реестр не изменился")

    # Право проверяется ещё раз при записи. Черновик живёт вне базы, и первая
    # проверка — для показа; защищает данные вторая. Подкладываем в черновик
    # исполнителя, которого туда не пустили.
    async with session_scope() as session:
        worker = await session.get(User, cast.worker)
        meeting = await session.get(Meeting, meeting_id)
        grants = await load_grants(session, worker)
        low.items[index].responsible_id = cast.outsider
        low.items[index].state = "new"
        forced = await protocol.take(
            session, low, index, meeting=meeting, actor=worker, grants=grants
        )
    check(not forced[0], "подложенный исполнитель запись не проводит", forced[1])
    check(forced[1] == "protocol.err.cannot_assign", "и отказ по праву",
          forced[1])
    check(await _counts(cast.org) == (made, given), "поручения не появилось")

    print("\n12.7. Журнал знает, чем кончилось предложение")
    async with session_scope() as session:
        taken_call = await session.get(AiCall, draft.call_id)
        check(taken_call.kind == "protocol", "вызов записан как протокол",
              str(taken_call.kind))
        check(taken_call.prompt_version == protocol.PROMPT_VERSION,
              "с версией промпта", str(taken_call.prompt_version))
        check(taken_call.confirmed is True,
              "принятый пункт отмечен подтверждением", str(taken_call.confirmed))

    # Черновик, из которого отклонили всё, — это отказ, а не молчание.
    gate.use(Fake(answers=[json.dumps({"items": [
        {"agenda": 2, "kind": "decision", "title": "Bekor qilinadigan qaror"},
    ]}, ensure_ascii=False)]))
    refusal = await _build(cast, meeting_id)
    async with session_scope() as session:
        for index in range(len(refusal.items)):
            await protocol.drop(session, refusal, index)
        call = await session.get(AiCall, refusal.call_id)
        check(call.confirmed is False, "отклонённый целиком — отмечен отказом",
              str(call.confirmed))
    check(await _counts(cast.org) == (made, given), "и в реестре по-прежнему пусто")

    print("\n12.8. Встреча без повестки протокола не даёт")
    async with session_scope() as session:
        chief = await session.get(User, cast.chief)
        grants = await load_grants(session, chief)
        bare = Meeting(
            organization_id=cast.org, owner_id=cast.chief, created_by=cast.chief,
            title="ТЕСТ Kun tartibisiz",
            start_at=MORNING - timedelta(hours=5),
            end_at=MORNING - timedelta(hours=4),
            status=MeetingStatus.FINISHED,
        )
        session.add(bare)
        await session.flush()
        empty = await protocol.build(
            session, meeting=bare, actor=chief, grants=grants, now=NOW
        )
    check(not empty.items, "предложений нет", str(len(empty.items)))
    check(empty.call_id is None, "и модель не звали вовсе", str(empty.call_id))

    print("\n12.9. Повестка вся закрыта — предлагать нечего, и модель молчит")
    gate.use(Fake(answers=["не должно прозвучать"]))
    async with session_scope() as session:
        chief = await session.get(User, cast.chief)
        grants = await load_grants(session, chief)
        closed = Meeting(
            organization_id=cast.org, owner_id=cast.chief, created_by=cast.chief,
            title="ТЕСТ Hammasi hal",
            start_at=MORNING - timedelta(hours=7),
            end_at=MORNING - timedelta(hours=6),
            status=MeetingStatus.FINISHED,
        )
        session.add(closed)
        await session.flush()
        session.add(MeetingParticipant(
            meeting_id=closed.id, user_id=cast.chief, created_at=MORNING
        ))
        point = AgendaItem(
            meeting_id=closed.id, position=1, title="Yagona band",
            created_by=cast.chief,
        )
        session.add(point)
        await session.flush()
        await decisions.create(
            session, actor=chief, title="Yagona band boʻyicha qaror",
            meeting=closed, agenda_item=point,
        )
        full = await protocol.build(
            session, meeting=closed, actor=chief, grants=grants, now=NOW
        )
    check(not full.items, "предложений нет", str(len(full.items)))
    check(full.call_id is None, "и модель не звали", str(full.call_id))


if __name__ == "__main__":
    asyncio.run(main())
