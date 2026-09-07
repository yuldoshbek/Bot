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
import ast
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

from app.ai import gate, models, protocol, question as ask_ai
from app.ai import report as report_words, summary, voice
from app.ai.local import Local, Mixed
from app.ai.provider import Fake, ProviderError
from app.core.config import settings
from app.core import speech
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
    TaskStatus,
    User,
    UserRole,
    UserStatus,
    VoiceNote,
    WorkingHours,
)
from app.services import decisions, digest, questions, weekly
from app.services import meetings as meeting_service
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


def tokens_of(text: str) -> set[str]:
    """Все числа в тексте. Сверка идёт по ним, а не по виду строки."""
    return set(re.findall(r"\d+", text or ""))


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
        await session.execute(
            delete(VoiceNote).where(VoiceNote.organization_id.in_(org_ids))
        )
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
    head: int = 0    # начальник отдела — область «свой отдел»
    finance: int = 0     # отдел, к которому приписаны свои
    projects: int = 0    # чужой отдел: спросивший про него получает пустоту


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
        # Начальник отдела — единственная область «свой отдел». Без него матрица
        # проверяла бы только «всё» и «своё», а промахивается обычно середина.
        head = await person("ТЕСТ Nazarov Boshliq", RoleCode.DEPT_HEAD, finance, 993_006)
        return Cast(
            org.id, chief.id, worker.id, outsider.id, 2,
            head=head.id, finance=finance.id, projects=projects.id,
        )


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
        await stage_speech(cast)
        await stage_weekly(cast)
        stage_models()
        await stage_ask(cast)
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


async def stage_speech(cast: Cast) -> None:
    print("\n13. Расшифровка речи: своя служба, без ключа и без денег")

    print("\n13.1. Оформление делает код, а не модель")
    # Разбивка по паузам: единственный настоящий признак смены мысли.
    pieces = [
        (0.0, 1.4, "birinchi fikr"),
        (1.6, 2.9, "davomi"),
        (6.0, 7.2, "ikkinchi fikr"),
    ]
    laid = speech.pretty("", pieces)
    check(laid.count("\n\n") == 1, "пауза развела абзацы", repr(laid))
    check(laid.startswith("Birinchi fikr davomi"),
          "внутри абзаца речь слитная", repr(laid))
    check("Ikkinchi fikr" in laid, "и второй абзац с заглавной", repr(laid))

    flat = speech.pretty(
        "birinchi gap. ikkinchi gap. uchinchi gap. tortinchi gap. beshinchi gap."
    )
    check(flat.count("\n\n") >= 1, "без разметки по времени режем по предложениям",
          repr(flat))
    check(flat.startswith("Birinchi"), "первая буква заглавная", flat[:20])

    # Слова не переписываются: расшифровка — свидетельство, а не черновик.
    said = "Toshkentda Karimov bilan uchrashuv bo'ldi."
    kept = speech.pretty(said)
    for word in ("Toshkentda", "Karimov", "uchrashuv"):
        check(word in kept, f"слово «{word}» не тронуто", kept)
    messy = speech.pretty("  много   пробелов   и  , знак ")
    check(messy == "Много пробелов и, знак",
          "лишние пробелы убраны, пробел перед запятой тоже", repr(messy))
    check(speech.duration(42) == "0:42" and speech.duration(725) == "12:05",
          "длительность читается", speech.duration(725))

    print("\n13.2. Своя служба — обычный поставщик за HTTP")
    from aiohttp import web

    async def answer(request: web.Request) -> web.Response:
        form = await request.post()
        got = form["audio"].file.read() if hasattr(form["audio"], "file") else b""
        return web.json_response({
            "text": "salom dunyo",
            "model": "test-uz-small",
            "seconds": 2.5,
            "pieces": [[0.0, 1.0, "salom"], [1.1, 2.0, "dunyo"], ["плохо", 1, 2]],
            "size": len(got),
        })

    server = web.Application()
    server.router.add_post("/transcribe", answer)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8899)
    await site.start()
    try:
        local = Local(url="http://127.0.0.1:8899/transcribe")
        heard = await local.transcribe(b"0123456789", model="ignored", hint="uz")
        check(heard.text == "salom dunyo", "текст пришёл", heard.text)
        check(heard.model == "test-uz-small", "и имя модели тоже", heard.model)
        check(heard.cost_usd == 0.0, "своя служба стоит ноль", str(heard.cost_usd))
        check(len(heard.pieces) == 2, "кривой отрезок отброшен, целые взяты",
              str(heard.pieces))
        check(local.free_voice, "и она объявлена бесплатной")

        # Текстов служба расшифровки не пишет — и говорит об этом прямо.
        refused = False
        try:
            await local.ask(system="s", user="u", model="m")
        except ProviderError:
            refused = True
        check(refused, "ответить текстом отказывается")

        # Смешанный поставщик: речь слушает своя, текст пишет другой.
        mixed = Mixed(voice=local, text=Fake(answers=["текст"]))
        check(mixed.free_voice, "смешанный берёт бесплатность у того, кто слушает")
        both = await mixed.transcribe(b"12345", model="m")
        written = await mixed.ask(system="s", user="u", model="m")
        check(both.text == "salom dunyo" and written.text == "текст",
              "каждый делает своё", f"{both.text} / {written.text}")
    finally:
        await runner.cleanup()

    # Служба недоступна — это отказ поставщика, а не падение.
    dead = Local(url="http://127.0.0.1:8899/transcribe")
    broke = False
    try:
        await dead.transcribe(b"1", model="m")
    except ProviderError:
        broke = True
    check(broke, "недоступная служба даёт отказ, а не исключение наружу")

    print("\n13.3. Речь слушают и без ключа, и при исчерпанном бюджете")
    settings.ai_enabled = False
    was_url = settings.stt_url
    settings.stt_url = "http://127.0.0.1:8899/transcribe"
    free = Fake(free_voice=True, transcripts=["birinchi fikr davomi"])
    gate.use(free)
    try:
        check(gate.hearing(), "своя служба слушает при выключенном ИИ")
        async with session_scope() as session:
            chief = await session.get(User, cast.chief)
            outcome = await gate.transcribe(
                session, b"audio", organization_id=cast.org, user_id=chief.id
            )
        check(outcome.worked, "расшифровка получена без ключа", outcome.reason)
        check(free.calls == 1, "и служба была вызвана", str(free.calls))

        # Потолок расхода — про платное. Бесплатное он останавливать не должен.
        async with session_scope() as session:
            session.add(AiCall(
                organization_id=cast.org, kind="test", model="m",
                prompt_version="v", started_at=utcnow(),
                cost_usd=Decimal(str(settings.ai_daily_budget_usd * 2)), ok=True,
            ))
        async with session_scope() as session:
            allowed, why = await gate.budget_left(session, cast.org)
            check(not allowed, "бюджет действительно исчерпан", why)
            spent = await gate.transcribe(
                session, b"audio", organization_id=cast.org
            )
        check(spent.reason != "budget", "но своя служба не остановлена",
              spent.reason)
        check(free.calls == 2, "её позвали", str(free.calls))

        # А платную — останавливает.
        paid = Fake(transcripts=["не должно прозвучать"])
        gate.use(paid)
        settings.ai_enabled = True
        settings.stt_url = ""
        async with session_scope() as session:
            stopped = await gate.transcribe(
                session, b"audio", organization_id=cast.org
            )
        check(stopped.reason == "budget", "платная расшифровка остановлена бюджетом",
              stopped.reason)
        check(paid.calls == 0, "и не была вызвана", str(paid.calls))
    finally:
        async with session_scope() as session:
            await session.execute(
                delete(AiCall).where(
                    AiCall.organization_id == cast.org, AiCall.kind == "test"
                )
            )
        settings.stt_url = was_url
        settings.ai_enabled = True

    print("\n13.4. Голосовое сохраняется, и дважды не расшифровывается")
    settings.stt_url = "http://127.0.0.1:8899/transcribe"
    teller = Fake(free_voice=True, transcripts=[
        "birinchi fikr keldi", "второй раз не должен прозвучать",
    ], pieces=[(0.0, 1.4, "birinchi fikr"), (5.0, 6.0, "keldi")])
    gate.use(teller)
    settings.ai_enabled = False
    try:
        async with session_scope() as session:
            chief = await session.get(User, cast.chief)
            note = await voice.remember(
                session, user=chief, file_id="AAA-file", file_unique_id="uniq-1",
                duration_seconds=17, size_bytes=4096,
            )
            first = await voice.write_down(session, note, b"audio", user=chief)
            note_id = note.id
        check(first, "расшифровка получена", first[:40])
        check("\n\n" in first, "и оформлена абзацами по паузам", repr(first))

        async with session_scope() as session:
            saved = await session.get(VoiceNote, note_id)
            check(saved.file_id == "AAA-file", "ссылка на запись сохранена",
                  saved.file_id)
            check(saved.duration_seconds == 17, "длительность сохранена")
            check(saved.transcript == first, "расшифровка легла рядом с записью")
            check(saved.model, "и модель, которой слушали", str(saved.model))

        # То же голосовое второй раз: запись та же, слушать заново незачем.
        async with session_scope() as session:
            chief = await session.get(User, cast.chief)
            again = await voice.remember(
                session, user=chief, file_id="BBB-other", file_unique_id="uniq-1",
                duration_seconds=17, size_bytes=4096,
            )
            second = await voice.write_down(session, again, b"audio", user=chief)
        check(again.id == note_id, "запись найдена, а не заведена заново",
              f"{note_id} → {again.id}")
        check(second == first, "расшифровка та же")
        check(teller.calls == 1, "и служба второй раз не звалась",
              str(teller.calls))

        async with session_scope() as session:
            total = await session.scalar(select(func.count(VoiceNote.id)).where(
                VoiceNote.organization_id == cast.org
            ))
        check(total == 1, "и запись в базе одна", str(total))

        print("\n13.5. Не расшифровали — запись всё равно осталась")
        gate.use(Fake(free_voice=True, fail=True))
        async with session_scope() as session:
            chief = await session.get(User, cast.chief)
            silent = await voice.remember(
                session, user=chief, file_id="CCC", file_unique_id="uniq-2",
                duration_seconds=5, size_bytes=100,
            )
            nothing = await voice.write_down(session, silent, b"audio", user=chief)
            silent_id = silent.id
        check(nothing == "", "текста нет", repr(nothing))
        async with session_scope() as session:
            kept_note = await session.get(VoiceNote, silent_id)
        check(kept_note is not None, "но запись сохранена")
        check(kept_note.transcript is None, "и помечена нерасшифрованной",
              str(kept_note.transcript))

        # Предел длины следует за ценой: бесплатной службе длинное не жалко.
        gate.use(Fake(free_voice=True))
        check(voice.max_seconds() == voice.FREE_MAX_SECONDS,
              "своей службе предел больше", str(voice.max_seconds()))
        gate.use(Fake())
        check(voice.max_seconds() == voice.MAX_SECONDS,
              "платной — меньше", str(voice.max_seconds()))

        await _speech_cases(cast)
    finally:
        settings.stt_url = was_url
        settings.ai_enabled = True


async def _speech_cases(cast: Cast) -> None:
    """Что бывает со службой расшифровки на самом деле."""
    from aiohttp import web

    print("\n13.6. Служба отвечает по-разному — и ни один ответ не ломает бота")
    seen: dict[str, object] = {}
    reply: dict[str, object] = {}

    async def stub(request: web.Request) -> web.Response:
        form = await request.post()
        seen["hint"] = form.get("hint", "")
        seen["bytes"] = len(form["audio"].file.read()) if hasattr(form.get("audio"), "file") else 0
        status = int(reply.get("status", 200))
        if reply.get("garbage"):
            return web.Response(status=status, text="это не json", content_type="text/plain")
        return web.json_response(reply.get("body", {}), status=status)

    server = web.Application()
    server.router.add_post("/transcribe", stub)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8898)
    await site.start()
    local = Local(url="http://127.0.0.1:8898/transcribe")
    try:
        # Служба упала.
        reply.clear(); reply.update(status=500, body={})
        failed = False
        try:
            await local.transcribe(b"12345", model="m")
        except ProviderError as error:
            failed = "500" in str(error)
        check(failed, "ответ 500 — отказ с понятной причиной", str(failed))

        # Служба ответила не по форме.
        reply.clear(); reply.update(status=200, garbage=True)
        broken = False
        try:
            await local.transcribe(b"12345", model="m")
        except ProviderError:
            broken = True
        check(broken, "ответ не по форме — тоже отказ, а не падение")

        # Подсказка о языке и лексике доходит до службы: без неё узбекские
        # имена превращаются в случайные слова.
        reply.clear()
        reply.update(status=200, body={"text": "salom", "pieces": []})
        await local.transcribe(b"0123456789", model="m", hint="проверка подсказки")
        check(seen.get("hint") == "проверка подсказки", "подсказка дошла",
              str(seen.get("hint")))
        check(seen.get("bytes") == 10, "и звук дошёл целиком", str(seen.get("bytes")))

        # Разметки по времени нет — текст всё равно оформляется, по предложениям.
        reply.clear()
        reply.update(status=200, body={
            "text": "birinchi gap. ikkinchi gap. uchinchi gap. tortinchi gap.",
            "pieces": [],
        })
        flat = await local.transcribe(b"1", model="m")
        laid = speech.pretty(flat.text, flat.pieces)
        check(not flat.pieces, "разметки нет", str(flat.pieces))
        check("\n\n" in laid, "но абзацы всё равно есть", repr(laid))

        # Пустой ответ — не успех: расшифровки нет, и говорить об этом надо прямо.
        reply.clear()
        reply.update(status=200, body={"text": "   ", "pieces": []})
        gate.use(Mixed(voice=local, text=Fake()))
        settings.stt_url = "http://127.0.0.1:8898/transcribe"
        async with session_scope() as session:
            chief = await session.get(User, cast.chief)
            silent = await gate.transcribe(
                session, b"1", organization_id=cast.org, user_id=chief.id
            )
        check(silent.reason == "empty", "пустая расшифровка успехом не считается",
              silent.reason)

        # Смешанная речь остаётся как есть: слова не переписываются.
        mixed_said = "Karimovga aytdim, потом уточним смету. Ertaga qaytamiz."
        reply.clear()
        reply.update(status=200, body={
            "text": mixed_said,
            "model": "uz-small",
            "seconds": 9.0,
            "pieces": [[0.0, 3.0, "Karimovga aytdim, потом уточним смету."],
                       [5.0, 7.0, "Ertaga qaytamiz."]],
        })
        async with session_scope() as session:
            chief = await session.get(User, cast.chief)
            note = await voice.remember(
                session, user=chief, file_id="MIX", file_unique_id="uniq-mix",
                duration_seconds=9, size_bytes=900,
            )
            written = await voice.write_down(session, note, b"1", user=chief)
            mixed_id = note.id
        check("Karimovga" in written and "уточним" in written,
              "оба языка сохранены дословно", written[:80])
        check("\n\n" in written, "и разведены по абзацам паузой", repr(written))

        async with session_scope() as session:
            saved = await session.get(VoiceNote, mixed_id)
            check(saved.model == "uz-small", "в записи стоит модель, которой слушали",
                  str(saved.model))

        # Второе, другое голосовое — своя запись, а не подмена первой.
        reply.clear()
        reply.update(status=200, body={"text": "boshqa xabar", "pieces": []})
        async with session_scope() as session:
            chief = await session.get(User, cast.chief)
            other = await voice.remember(
                session, user=chief, file_id="OTHER", file_unique_id="uniq-other",
                duration_seconds=3, size_bytes=300,
            )
            await voice.write_down(session, other, b"1", user=chief)
            other_id = other.id
        check(other_id != mixed_id, "запись отдельная", f"{mixed_id} / {other_id}")
        async with session_scope() as session:
            first = await session.get(VoiceNote, mixed_id)
            second = await session.get(VoiceNote, other_id)
        check(first.transcript != second.transcript,
              "и расшифровки у них разные", second.transcript)

        # Длинная речь без пауз не превращается в простыню: работает предел
        # длины абзаца, а не пауза — человек может говорить без остановки.
        long_pieces = [
            [
                float(i * 3), float(i * 3 + 2.6),
                f"uzun gapning {chr(97 + i)} qismi va yana bir necha soʻz shu yerda",
            ]
            for i in range(20)
        ]
        reply.clear()
        reply.update(status=200, body={
            "text": " ".join(piece[2] for piece in long_pieces),
            "pieces": long_pieces,
        })
        heard = await local.transcribe(b"1", model="m")
        laid = speech.pretty(heard.text, heard.pieces)
        gaps = {round(long_pieces[i + 1][0] - long_pieces[i][1], 1) for i in range(19)}
        check(gaps == {0.4}, "паузы между отрезками короче порога", str(gaps))
        check(laid.count("\n\n") >= 2,
              "и всё равно разбито — по длине абзаца", str(laid.count("\n\n")))
        check(max(len(block) for block in laid.split("\n\n")) < 600,
              "ни один абзац не разросся",
              str(max(len(block) for block in laid.split("\n\n"))))
        check(len(heard.pieces) == 20, "и все отрезки разобраны",
              str(len(heard.pieces)))
    finally:
        await runner.cleanup()


async def _report_of(cast: Cast, *, words=None):
    async with session_scope() as session:
        chief = await session.get(User, cast.chief)
        grants = await load_grants(session, chief)
        made = await weekly.build(session, viewer=chief, grants=grants, now=MORNING)
        intro = await words(session, made, chief) if words else ""
        return weekly.render(made, "ru", intro=intro or None), made, intro


async def stage_weekly(cast: Cast) -> None:
    print("\n14. Недельный отчёт: тренды поверх посчитанного")

    print("\n14.1. Без ИИ уходит таблица показателей")
    settings.ai_enabled = False
    fake = Fake(answers=["не должно прозвучать"])
    gate.use(fake)
    plain, made, _ = await _report_of(cast)
    with_ai, _, empty_intro = await _report_of(cast, words=report_words.words)
    check(len(made.lines) == 15, "посчитаны все пятнадцать показателей",
          str(len(made.lines)))
    check(not made.empty, "и отчёт не пуст")
    check(fake.calls == 0, "модель не звали", str(fake.calls))
    check(empty_intro == "", "вступления нет", repr(empty_intro))
    check(with_ai == plain, "письмо не изменилось ни на знак")

    # Утверждение о самой отрисовке, а не сравнение двух её вызовов.
    check("\n\n\n" not in plain, "без вступления лишнего пустого места нет")
    check(weekly.render(made, "ru", intro="") == plain, "пустое вступление ничего не добавляет")
    check("ПРОБА" in weekly.render(made, "ru", intro="ПРОБА"), "написанное — ставится")

    print("\n14.2. Сравнение — с прошлой неделей, а не с самим собой")
    # Утверждение о самих границах: сравнение периода с собой дало бы ровный
    # тренд по всем показателям и выглядело бы как «ничего не изменилось».
    check(made.before_until == made.since,
          "прошлый период кончается там, где начинается нынешний",
          f"{made.before_until} / {made.since}")
    check(made.until - made.since == made.before_until - made.before_since,
          "и длится столько же",
          f"{made.until - made.since} / {made.before_until - made.before_since}")
    check(made.before_until <= made.since, "периоды не перекрываются")

    print("\n14.3. Прогноз не сравнивается с прошлой неделей")
    forecast = [line for line in made.lines if line.key == weekly.FORECAST_KEY]
    check(forecast and forecast[0].before is None,
          "у прогноза нет прошлого значения", str(bool(forecast)))
    check(forecast and forecast[0].moved == "", "и стрелки у него нет",
          str(forecast[0].moved if forecast else "?"))
    movable = [line for line in made.lines if line.key != weekly.FORECAST_KEY]
    check(all(line.moved in ("", "↑", "↓", "→") for line in movable),
          "движение обозначено фактом, а не оценкой",
          str({line.moved for line in movable}))

    print("\n14.4. Метки берутся с того же отчёта")
    values = report_words.facts(made)
    check(values, "метки собраны", str(len(values)))
    for line in made.lines:
        if line.now.value is None:
            check(line.key not in values, f"молчащий показатель {line.key} не в метках")
        else:
            check(values.get(line.key) == line.now.shown(),
                  f"значение {line.key} взято с отчёта", str(values.get(line.key)))
    for line in made.lines:
        if line.before is not None and line.before.value is not None:
            check(values.get(f"{line.key}_was") == line.before.shown(),
                  f"прошлое значение {line.key} — тоже метка",
                  str(values.get(f"{line.key}_was")))

    print("\n14.5. Ни одного числа, которого никто не считал")
    settings.ai_enabled = True
    key = next(iter(values))
    good = "\n".join([
        f"Asosiy oʻzgarish — {{{key}}} koʻrsatkichida.",
        "Bu haftada shu yoʻnalishga qarash kerak.",
        "Qolgan koʻrsatkichlar sezilarli oʻzgarmadi.",
        "Xulosa: diqqatni bitta joyga qaratish.",
    ])
    gate.use(Fake(answers=[good]))
    worded, again, intro = await _report_of(cast, words=report_words.words)
    check(intro, "выводы написаны", intro[:60])
    check("{" not in intro, "метка в письме не осталась", intro[:80])
    seen = tokens_of(intro)
    allowed = {found for value in values.values() for found in tokens_of(value)}
    check(seen, "и числа в них есть", str(seen))
    check(seen <= allowed, "каждое число — из посчитанных", str(seen - allowed))
    body = plain.split("\n", 2)[2]
    check(worded.endswith(body), "таблица показателей осталась нетронутой")

    print("\n14.6. Негодный ответ письма не портит")
    for name, answer in (
        ("цифра от модели", "Bu hafta 7 ta muammo.\nIkkinchi.\nUchinchi."),
        ("выдуманная метка", "Koʻrsatkich {vydumka} oshdi.\nIkkinchi.\nUchinchi."),
        ("одна строка", "Hammasi yaxshi."),
        ("пустой ответ", "  "),
    ):
        gate.use(Fake(answers=[answer]))
        got, _, _ = await _report_of(cast, words=report_words.words)
        check(got == plain, f"{name}: ушла обычная таблица", got[:60])

    print("\n14.7. Старшая модель — только здесь")
    gate.use(Fake(answers=[good]))
    async with session_scope() as session:
        chief = await session.get(User, cast.chief)
        grants = await load_grants(session, chief)
        built = await weekly.build(session, viewer=chief, grants=grants, now=MORNING)
        await report_words.words(session, built, chief)
        last = (await session.execute(
            select(AiCall).where(
                AiCall.organization_id == cast.org,
                AiCall.kind == "weekly_report",
            ).order_by(AiCall.id.desc()).limit(1)
        )).scalar_one_or_none()
    check(last is not None, "вызов записан в журнал")
    check(last.model == settings.ai_model_report, "старшей моделью", str(last.model))
    check(last.prompt_version == report_words.PROMPT_VERSION, "и с версией промпта",
          str(last.prompt_version))

    # Проверка по исходнику: имя старшей модели не должно встречаться нигде,
    # кроме настроек и этого сценария. Иначе «только здесь» — просто слова.
    root = Path(__file__).resolve().parents[1] / "app"
    users = sorted(
        str(path.relative_to(root.parent))
        for path in root.rglob("*.py")
        if "ai_model_report" in path.read_text(encoding="utf-8")
    )
    check(users == ["app/ai/models.py", "app/core/config.py"],
          "имя старшей модели берётся ровно в одном месте", str(users))
    check(models.name_for("weekly_report") == settings.ai_model_report,
          "и справочник отдаёт именно её", models.name_for("weekly_report"))

    print("\n14.8. Кириллица выводится правилом")
    async with session_scope() as session:
        chief = await session.get(User, cast.chief)
        chief.locale = "uz-Cyrl"
    gate.use(Fake(answers=[good]))
    async with session_scope() as session:
        chief = await session.get(User, cast.chief)
        grants = await load_grants(session, chief)
        built = await weekly.build(session, viewer=chief, grants=grants, now=MORNING)
        cyrillic = await report_words.words(session, built, chief)
    async with session_scope() as session:
        chief = await session.get(User, cast.chief)
        chief.locale = "uz"
    check("koʻrsatkichida" not in cyrillic, "латиница не осталась", cyrillic[:80])
    check("кўрсаткичида" in cyrillic, "строка переведена правилом", cyrillic[:80])

    print("\n14.9. Раз в неделю, в понедельник утром")
    monday = datetime(2026, 9, 7, 3, 0, tzinfo=timezone.utc)   # 08:00 в Ташкенте
    check(weekly.due_now(monday, "Asia/Tashkent"), "в понедельник в 08:00 пора")
    check(not weekly.due_now(monday - timedelta(hours=1), "Asia/Tashkent"),
          "в 07:00 ещё рано")
    check(not weekly.due_now(monday + timedelta(days=1), "Asia/Tashkent"),
          "во вторник уже не время")
    check(weekly.week_key(monday, "Asia/Tashkent") !=
          weekly.week_key(monday + timedelta(days=7), "Asia/Tashkent"),
          "ключ недели меняется через неделю")
    check(weekly.week_key(monday, "Asia/Tashkent") ==
          weekly.week_key(monday + timedelta(days=3), "Asia/Tashkent"),
          "и не меняется внутри недели")

    gate.use(Fake(answers=[good, good, good]))
    async with session_scope() as session:
        sent = await weekly.send_reports(session, monday, words=report_words.words)
    check(sent >= 1, "отчёт поставлен в очередь", str(sent))
    async with session_scope() as session:
        twice = await weekly.send_reports(
            session, monday + timedelta(minutes=5), words=report_words.words
        )
    check(twice == 0, "второй проход в ту же неделю ничего не добавляет", str(twice))

    # А через неделю — добавляет. Без номера недели в ключе письмо ушло бы
    # ровно один раз за всю жизнь системы, и заметили бы это через месяц.
    gate.use(Fake(answers=[good, good, good]))
    async with session_scope() as session:
        later = await weekly.send_reports(
            session, monday + timedelta(days=7), words=report_words.words
        )
    check(later >= 1, "через неделю отчёт уходит снова", str(later))

    async with session_scope() as session:
        letter = await session.scalar(
            select(Notification.body).where(
                Notification.user_id == cast.chief,
                Notification.kind == "report.weekly",
            )
        )
    check(letter, "письмо собрано", (letter or "")[:40])
    check("📈" in (letter or ""), "и это недельный отчёт", (letter or "")[:40])


def _kinds_in_source() -> set[str]:
    """Виды вызовов, которые где-либо передаются в дверь к модели.

    Разбор по дереву, а не поиском строк: `kind=` встречается и у уведомлений,
    и совпадение по подстроке нашло бы их тоже.
    """
    root = Path(__file__).resolve().parents[1] / "app"
    found: set[str] = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in ("ask", "transcribe"):
                continue
            if not isinstance(func.value, ast.Name) or func.value.id != "gate":
                continue
            for word in node.keywords:
                if word.arg == "kind" and isinstance(word.value, ast.Constant):
                    found.add(str(word.value.value))
    # Расшифровка вида в аргументах не передаёт — она называет его сама,
    # когда спрашивает у справочника модель. Собираем и такие обращения.
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "name_for"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                found.add(str(node.args[0].value))
    return found


def stage_models() -> None:
    print("\n15. Модели согласованы между собой")

    print("\n15.1. Сценарий не знает имён моделей")
    # Имя модели выбирается по виду вызова. Написанное в сценарии имя однажды
    # поправят в одном месте и забудут в трёх.
    root = Path(__file__).resolve().parents[1] / "app"
    named = sorted(
        str(path.relative_to(root.parent))
        for path in root.rglob("*.py")
        for text in [path.read_text(encoding="utf-8")]
        if "gpt-4o" in text or "whisper-1" in text
    )
    check(named == ["app/core/config.py"],
          "имена моделей встречаются только в настройках", str(named))

    print("\n15.2. Каждый вид вызова объявлен")
    used = _kinds_in_source()
    check(used, "виды вызовов найдены в исходнике", str(sorted(used)))
    unknown = sorted(used - set(models.KINDS))
    check(not unknown, "и каждый есть в справочнике", str(unknown))
    # Обратное тоже важно: объявленный, но никем не используемый вид — это
    # либо забытый сценарий, либо опечатка в имени.
    idle = sorted(set(models.KINDS) - used)
    check(not idle, "и каждый объявленный используется", str(idle))

    print("\n15.3. Роль решает, какой моделью")
    check(models.name_for("digest") == settings.ai_model_routine,
          "сводка идёт рутинной", models.name_for("digest"))
    check(models.name_for("protocol") == settings.ai_model_routine,
          "протокол тоже", models.name_for("protocol"))
    check(models.name_for("voice_task") == settings.ai_model_routine,
          "и голосовое поручение", models.name_for("voice_task"))
    check(models.name_for("weekly_report") == settings.ai_model_report,
          "недельный отчёт — старшей", models.name_for("weekly_report"))
    check(models.name_for("voice_transcribe") == settings.ai_model_voice,
          "расшифровка — своей", models.name_for("voice_transcribe"))
    # Незнакомый вид не должен молча уйти на старшую модель: она в двадцать
    # раз дороже, и заметили бы это только по счёту.
    check(models.role_of("выдуманный_вид") == "routine",
          "неизвестный вид идёт рутинной, а не старшей",
          models.role_of("выдуманный_вид"))
    check(set(models.KINDS.values()) <= set(models.ROLES),
          "все роли из объявленного перечня", str(set(models.KINDS.values())))

    print("\n15.4. Список моделей расшифровки — общий со службой")
    shared = models.speech_models()
    check(shared, "список прочитан", str(len(shared)))
    check(models.CATALOGUE_PATH.name == "models.json",
          "и лежит в общем файле", models.CATALOGUE_PATH.name)
    service = (models.CATALOGUE_PATH.parent / "app.py").read_text(encoding="utf-8")
    check("models.json" in service,
          "служба расшифровки читает тот же файл, а не свою копию")
    check(models.CATALOGUE_PATH.exists(), "файл на месте",
          str(models.CATALOGUE_PATH))

    for alias, item in shared.items():
        check(item.repo and item.languages and item.note,
              f"у модели {alias} заполнены имя, языки и пояснение",
              f"{item.repo}/{item.languages}")

    print("\n15.5. Узбекские модели подключены")
    uzbek = {item.alias: item for item in models.for_uzbek()}
    check(set(uzbek) == {"uz-small", "uz-medium"},
          "обе узбекские модели в списке", str(sorted(uzbek)))
    check(uzbek["uz-small"].repo == "OvozifyLabs/whisper-small-uz-v1",
          "uz-small — сбалансированная по трём языкам", uzbek["uz-small"].repo)
    check(uzbek["uz-medium"].repo == "islomov/rubaistt_v2_medium",
          "uz-medium — ташкентский говор", uzbek["uz-medium"].repo)
    check(all(item.convert for item in uzbek.values()),
          "обе требуют перевода в CTranslate2 — и служба делает его сама")
    check("ct2-transformers-converter" in service,
          "перевод встроен в службу, а не оставлен человеку")
    check(all("uz" in item.languages for item in uzbek.values()),
          "и обе объявлены узбекскими")
    # Базовые модели узбекскими не считаются: иначе выбор «uz» вернул бы
    # ту, что узбекского почти не знает.
    check("small" not in uzbek, "базовая small в узбекские не попала")


# ── Вопрос своими словами ───────────────────────────────────────────────────
# Момент, в который задают вопрос. Пришпилен, как и остальные точки отсчёта:
# «за месяц», посчитанный от часов машины, проходит месяц и падает.
ASKED = datetime(2026, 9, 7, 9, 0, tzinfo=timezone.utc)


async def _asked(cast: Cast, who: int, reply: str, *, text: str = "ТЕСТ savol"):
    """Задаёт вопрос с заранее известным ответом модели."""
    gate.use(Fake(answers=[reply]))
    async with session_scope() as session:
        viewer = await session.get(User, who)
        grants = await load_grants(session, viewer)
        return await ask_ai.answer(
            session, text, viewer=viewer, grants=grants, now=ASKED
        )


async def _resolved(cast: Cast, raw: dict, who: int = 0) -> questions.Filter:
    """Разбирает присланные моделью значения от имени человека."""
    async with session_scope() as session:
        viewer = await session.get(User, who or cast.chief)
        grants = await load_grants(session, viewer)
        return await questions.resolve(session, raw, viewer=viewer, grants=grants)


async def _all_counts(org_id: int) -> tuple[int, int, int]:
    """Сколько записей в организации. Считается запросом: пропади таблица,
    счёт не выполнится вовсе, и это тоже ответ."""
    async with session_scope() as session:
        tasks = await session.scalar(
            select(func.count(Task.id)).where(Task.organization_id == org_id)
        )
        made = await session.scalar(
            select(func.count(Decision.id)).where(Decision.organization_id == org_id)
        )
        meets = await session.scalar(
            select(func.count(Meeting.id)).where(Meeting.organization_id == org_id)
        )
    return int(tasks or 0), int(made or 0), int(meets or 0)


async def stage_ask(cast: Cast) -> None:
    print("\n16. Вопрос своими словами")
    settings.ai_enabled = True
    # Журнал очищается: дальше по нему проверяется ровно одна строка вопроса.
    async with session_scope() as session:
        await session.execute(delete(AiCall).where(AiCall.organization_id == cast.org))

    _ask_payload()
    await _ask_values(cast)
    _ask_window()
    await _ask_names(cast)
    await _ask_records(cast)
    await _ask_matrix(cast)
    await _ask_foreign(cast)
    await _ask_no_writes(cast)
    await _ask_limit(cast)
    await _ask_fallback(cast)
    await _ask_overdue(cast)
    _ask_one_rule()
    await _ask_journal(cast)


def _ask_payload() -> None:
    print("\n16.1. От модели принимаются только перечисленные поля")
    check(ask_ai.payload("") == {}, "пустой ответ — пустая структура")
    check(ask_ai.payload("здравствуйте") == {}, "текст без JSON — пустая структура")
    check(ask_ai.payload("[1, 2]") == {}, "список вместо структуры отброшен")
    check(ask_ai.payload('{"kind":') == {}, "испорченный JSON отброшен")

    fenced = ask_ai.payload('```json\n{"kind":"decision"}\n```')
    check(fenced == {"kind": "decision"}, "JSON в тройных кавычках разобран", str(fenced))
    prose = ask_ai.payload('Вот условия: {"kind":"meeting"} — готово')
    check(prose == {"kind": "meeting"}, "JSON внутри пояснения разобран", str(prose))

    # Третья граница блока: модель возвращает структуру, а не запрос. Поле,
    # которого нет в перечне, не проверяется на опасность — оно не берётся.
    written = ask_ai.payload(
        '{"kind":"task","sql":"SELECT * FROM tasks","query":"drop","table":"users"}'
    )
    check(written == {"kind": "task"}, "текст запроса от модели не принят", str(written))
    wide = ask_ai.payload('{"kind":"task","limit":1000,"organization_id":7,"user_id":3}')
    check(set(wide) == {"kind"}, "предел и чужие ключи отброшены", str(sorted(wide)))
    nested = ask_ai.payload('{"kind":"task","person":{"id":3},"department":["moliya"]}')
    check(set(nested) == {"kind"}, "вложенные значения отброшены", str(sorted(nested)))

    long_name = ask_ai.payload('{"department":"' + "я" * 900 + '"}')
    check(len(long_name.get("department", "")) == ask_ai.MAX_VALUE,
          "длинное значение укорочено", str(len(long_name.get("department", ""))))

    # «Да/нет» разбирается отдельно от строк. `bool("false")` — истина,
    # и вопрос «что просрочено» получил бы ответ наоборот.
    check(ask_ai.payload('{"overdue":true}').get("overdue") is True, "просрочку просят")
    check(ask_ai.payload('{"overdue":false}').get("overdue") is False, "просрочку не просят")
    check(ask_ai.payload('{"overdue":"false"}').get("overdue") is False,
          "строка «false» прочитана как «нет», а не как «да»",
          str(ask_ai.payload('{"overdue":"false"}')))
    check("overdue" not in ask_ai.payload('{"overdue":"balki"}'),
          "невнятное «да/нет» отброшено")
    check("status" not in ask_ai.payload('{"status":true}'),
          "булево в строковом поле отброшено")


async def _ask_values(cast: Cast) -> None:
    print("\n16.2. Значения сверяются с перечнями, а не берутся на слово")
    check((await _resolved(cast, {"kind": "выдуманный"})).kind == "task",
          "незнакомый вид записей — поручения")
    check((await _resolved(cast, {"kind": "decision"})).kind == "decision",
          "знакомый вид принят")

    check((await _resolved(cast, {"kind": "task", "status": "in_progress"})).status
          == "IN_PROGRESS", "статус своего вида принят")
    check((await _resolved(cast, {"kind": "task", "status": "open"})).status == "",
          "статус чужого вида отброшен")
    # Проверка не «всегда отбрасываем»: у решения тот же «open» осмыслен.
    check((await _resolved(cast, {"kind": "decision", "status": "open"})).status
          == "OPEN", "и у своего вида тот же статус принят")

    check((await _resolved(cast, {"kind": "task", "priority": "high"})).priority
          == "HIGH", "приоритет принят")
    check((await _resolved(cast, {"kind": "task", "priority": "срочно"})).priority == "",
          "незнакомый приоритет отброшен")
    check((await _resolved(cast, {"kind": "decision", "priority": "high"})).priority == "",
          "у решения приоритета нет вовсе")

    check((await _resolved(cast, {"kind": "task", "overdue": True})).overdue is True,
          "у поручения просрочка бывает")
    check((await _resolved(cast, {"kind": "meeting", "overdue": True})).overdue is False,
          "у встречи не бывает")
    # Разбор значений — отдельная дверь, и запирается она сама. `bool("false")`
    # истинно, и «не просрочено» превратилось бы в «просрочено» ещё до запроса.
    check((await _resolved(cast, {"kind": "task", "overdue": "false"})).overdue is False,
          "строка вместо «да/нет» не становится «да» и при разборе")

    check((await _resolved(cast, {"period": "вчера"})).period == "",
          "незнакомый период отброшен")
    check((await _resolved(cast, {"period": "past_month"})).period == "past_month",
          "знакомый принят")

    bare = await _resolved(cast, {"kind": "task"})
    check(not bare.narrow, "фильтр без единого условия узким не считается")
    check((await _resolved(cast, {"kind": "task", "overdue": True})).narrow,
          "а с условием — считается")


def _ask_window() -> None:
    print("\n16.3. Границы периода считает система, а не модель")
    tashkent = "Asia/Tashkent"
    # Ташкент — UTC+5 круглый год, перевода часов нет. Местный день 7 сентября
    # начинается в 19:00 UTC шестого. Посчитано руками: сверять границы той же
    # функцией, что их строит, — значит не проверять ничего.
    day = datetime(2026, 9, 6, 19, 0, tzinfo=timezone.utc)

    since, until = questions.window("past_month", now=ASKED, timezone_name=tashkent)
    check(since == day - timedelta(days=30),
          "прошедший месяц начинается за тридцать дней до местного дня", str(since))
    check(until == day + timedelta(days=1),
          "и кончается началом завтрашнего местного дня", str(until))

    since, until = questions.window("week", now=ASKED, timezone_name=tashkent)
    check(since == day, "ближайшая неделя начинается сегодня", str(since))
    check(until == day + timedelta(days=7), "и кончается через семь дней", str(until))

    today = questions.window("today", now=ASKED, timezone_name=tashkent)
    check(today == (day, day + timedelta(days=1)), "сегодня — ровно местные сутки",
          str(today))
    check(questions.window("выдуманный", now=ASKED, timezone_name=tashkent) == (None, None),
          "незнакомый период границ не даёт")

    # Направление внутри названия — не украшение: «за месяц» смотрит назад,
    # «на месяц» вперёд, и вопрос о просрочке различает их до дня.
    back = questions.window("past_month", now=ASKED, timezone_name=tashkent)
    forward = questions.window("month", now=ASKED, timezone_name=tashkent)
    check(back[0] < ASKED < back[1], "прошедший месяц включает сегодняшний день")
    check(back[0] < forward[0], "ближайший начинается позже прошедшего")
    check(forward[1] > back[1], "и кончается позже")

    # Пояс получателя, а не сервера: в Лиссабоне тот же день начинается иначе.
    other = questions.window("today", now=ASKED, timezone_name="Europe/Lisbon")
    check(other[0] != today[0], "у другого часового пояса другая граница дня",
          f"{other[0]} = {today[0]}")


async def _ask_names(cast: Cast) -> None:
    print("\n16.4. Имя и отдел превращает система")
    named = await _resolved(cast, {"kind": "task", "person": "Karimov"})
    check(named.person_ids == [cast.worker], "названный человек найден",
          str(named.person_ids))
    check(named.possible, "и вопрос остался выполнимым")

    ghost = await _resolved(cast, {"kind": "task", "person": "Xayoliy Odam"})
    check(not ghost.possible,
          "ненайденный человек делает ответ пустым, а не снимает условие")
    check("ask.note.no_person" in ghost.notes, "и причина названа", str(ghost.notes))

    twins = await _resolved(cast, {"kind": "task", "person": "Salimov"})
    check(len(twins.person_ids) == cast.twins, "однофамильцы взяты все",
          str(twins.person_ids))
    check("ask.note.many_people" in twins.notes, "и об этом сказано вслух")

    found = await _resolved(cast, {"kind": "task", "department": "Moliya"})
    check(found.department_id == cast.finance, "отдел найден по части названия",
          str(found.department_id))

    missing = await _resolved(cast, {"kind": "task", "department": "Yoʻq boʻlim"})
    check(not missing.possible, "ненайденный отдел тоже делает ответ пустым")
    check(not missing.notes,
          "и молча: «отдела нет» и «отдел не ваш» обязаны выглядеть одинаково")

    # Шаблонный знак в названии — это знак, а не образец поиска.
    wild = await _resolved(cast, {"kind": "task", "department": "%"})
    check(not wild.possible, "название из одного «%» не выбирает первый попавшийся")

    # Названия отделов и имена пишут люди, а строка «понял так» уходит
    # в сообщение с разметкой. Угловая скобка в названии сломала бы отправку
    # целиком — человек не получил бы ответа вовсе, и не понял бы почему.
    marked = questions.describe(
        questions.Filter(
            kind="task", department_name="<b>Moliya</b>", person_names=["A & B"],
        ),
        "ru",
    )
    check("<b>" not in marked, "разметка из названия отдела в строку не попадает",
          marked)
    check("&amp;" in marked, "и амперсанд в имени экранирован", marked)

    # Отдел чужой организации не находится вовсе: граница организации стоит
    # до всех остальных, иначе «по организации» означало бы «по всем сразу».
    async with session_scope() as session:
        alien_org = Organization(name=ORG_NAME, timezone="Asia/Tashkent")
        session.add(alien_org)
        await session.flush()
        session.add(Department(organization_id=alien_org.id, name="ТЕСТ Chetdagi"))
    alien = await _resolved(cast, {"kind": "task", "department": "Chetdagi"})
    check(not alien.possible, "отдел чужой организации не находится")


async def _ask_records(cast: Cast) -> None:
    """Записи для матрицы: по одной на каждое отношение, которое проверяется.

    Статусы взяты редкие намеренно: по ним матрица отделена от всего, что
    насоздавали предыдущие проверки, и остаётся короче предела показа.
    """
    async with session_scope() as session:
        chief = await session.get(User, cast.chief)
        worker = await session.get(User, cast.worker)
        outsider = await session.get(User, cast.outsider)
        head = await session.get(User, cast.head)

        for creator, assignee, title in (
            (chief, worker, "ТЕСТ Bloklangan moliya"),
            (chief, outsider, "ТЕСТ Bloklangan loyiha"),
            (worker, worker, "ТЕСТ Bloklangan oʻzimniki"),
            (chief, head, "ТЕСТ Bloklangan boshliq"),
        ):
            task = await task_service.create_task(
                session, creator=creator, assignee=assignee, title=title,
                due_at=ASKED + timedelta(days=2),
            )
            task.status = TaskStatus.BLOCKED

        for author, responsible in (
            (cast.chief, cast.worker),
            (cast.chief, cast.outsider),
            (cast.worker, cast.worker),
        ):
            session.add(Decision(
                organization_id=cast.org, title="ТЕСТ Bekor qilingan qaror",
                author_id=author, responsible_id=responsible,
                status="CANCELLED",
            ))

        for index, owner in enumerate((cast.chief, cast.outsider, cast.head)):
            meeting = Meeting(
                organization_id=cast.org, owner_id=owner, created_by=owner,
                title="ТЕСТ Tasdiqlangan uchrashuv",
                start_at=ASKED + timedelta(days=10 + index),
                end_at=ASKED + timedelta(days=10 + index, hours=1),
                status=MeetingStatus.CONFIRMED,
            )
            session.add(meeting)
            await session.flush()
            if owner == cast.chief:
                session.add(MeetingParticipant(
                    meeting_id=meeting.id, user_id=cast.worker, created_at=ASKED,
                ))


async def _ask_matrix(cast: Cast) -> None:
    print("\n16.5. Матрица «запись × человек»: ответ совпадает с правом на карточку")
    people = (
        ("руководитель", cast.chief),
        ("начальник отдела", cast.head),
        ("сотрудник", cast.worker),
        ("сотрудник чужого отдела", cast.outsider),
    )
    seen: dict[str, tuple[frozenset, frozenset, frozenset]] = {}

    for title, who in people:
        async with session_scope() as session:
            viewer = await session.get(User, who)
            grants = await load_grants(session, viewer)

            # ── Поручения: сверка с access_for, а не с тем же visible_filter.
            rows = list((await session.execute(
                select(Task).where(
                    Task.organization_id == cast.org, Task.status == TaskStatus.BLOCKED
                )
            )).scalars().all())
            check(len(rows) <= questions.MAX_ROWS,
                  f"{title}: поручений матрицы не больше предела показа", str(len(rows)))
            expected = {
                row.id for row in rows
                if (await task_service.access_for(session, row, viewer, grants)).can_view
            }
            found = await questions.run(
                session, questions.Filter(kind="task", status="BLOCKED"),
                viewer=viewer, grants=grants, now=ASKED,
            )
            actual = {hit.id for hit in found.hits}
            check(actual == expected, f"{title}: поручения совпали с правом на карточку",
                  f"{sorted(actual)} ≠ {sorted(expected)}")
            tasks_seen = frozenset(actual)

            # ── Решения: сверка с may_read.
            rows = list((await session.execute(
                select(Decision).where(
                    Decision.organization_id == cast.org,
                    Decision.status == "CANCELLED",
                )
            )).scalars().all())
            expected = set()
            for row in rows:
                if await decisions.may_read(session, decision=row, viewer=viewer):
                    expected.add(row.id)
            found = await questions.run(
                session, questions.Filter(kind="decision", status="CANCELLED"),
                viewer=viewer, grants=grants, now=ASKED,
            )
            actual = {hit.id for hit in found.hits}
            check(actual == expected, f"{title}: решения совпали с правом на карточку",
                  f"{sorted(actual)} ≠ {sorted(expected)}")
            decisions_seen = frozenset(actual)

            # ── Встречи: сверка с may_read.
            rows = list((await session.execute(
                select(Meeting).where(
                    Meeting.organization_id == cast.org,
                    Meeting.status == MeetingStatus.CONFIRMED,
                )
            )).scalars().all())
            expected = set()
            for row in rows:
                if await meeting_service.may_read(session, meeting=row, viewer=viewer):
                    expected.add(row.id)
            found = await questions.run(
                session, questions.Filter(kind="meeting", status=MeetingStatus.CONFIRMED),
                viewer=viewer, grants=grants, now=ASKED,
            )
            actual = {hit.id for hit in found.hits}
            check(actual == expected, f"{title}: встречи совпали с правом на карточку",
                  f"{sorted(actual)} ≠ {sorted(expected)}")
            meetings_seen = frozenset(actual)

        seen[title] = (tasks_seen, decisions_seen, meetings_seen)

    # Матрица, в которой всем видно одно и то же, не проверяет прав: совпасть
    # с пустотой легко. Проверяем, что области действительно разные.
    chief_tasks = seen["руководитель"][0]
    check(len(chief_tasks) >= 4, "руководителю видны все поручения матрицы",
          str(len(chief_tasks)))
    check(seen["сотрудник"][0] < chief_tasks, "сотруднику — только часть",
          str(sorted(seen["сотрудник"][0])))
    check(seen["сотрудник чужого отдела"][0] != seen["сотрудник"][0],
          "и разным людям видно разное")
    # Середина между «всё» и «своё» — область отдела. Промахивается обычно она.
    head_tasks = seen["начальник отдела"][0]
    check(seen["сотрудник"][0] < head_tasks < chief_tasks,
          "начальнику отдела видно больше своего, но не всё",
          f"{sorted(head_tasks)} из {sorted(chief_tasks)}")


async def _ask_foreign(cast: Cast) -> None:
    print("\n16.6. Спросивший про чужой отдел получает пустоту, а не отказ")
    raw = {"kind": "task", "status": "blocked", "department": "Loyihalar"}

    async with session_scope() as session:
        viewer = await session.get(User, cast.worker)
        grants = await load_grants(session, viewer)
        item = await questions.resolve(session, raw, viewer=viewer, grants=grants)
        found = await questions.run(
            session, item, viewer=viewer, grants=grants, now=ASKED
        )
        # В чужом отделе записи есть: без этого пустой ответ ничего не доказывал бы.
        exists = await session.scalar(select(func.count(Task.id)).where(
            Task.organization_id == cast.org,
            Task.department_id == cast.projects,
            Task.status == TaskStatus.BLOCKED,
        ))
    check(int(exists or 0) > 0, "в чужом отделе записи есть", str(exists))
    check(item.possible, "вопрос выполнимый: отдел нашёлся, отказа нет")
    check(item.department_id == cast.projects, "и это именно чужой отдел")
    check(found.empty, "а ответ пуст — ни строки, ни намёка")

    # То же для начальника отдела: своя область есть, чужая — нет.
    async with session_scope() as session:
        head = await session.get(User, cast.head)
        grants = await load_grants(session, head)
        item = await questions.resolve(session, raw, viewer=head, grants=grants)
        alien = await questions.run(session, item, viewer=head, grants=grants, now=ASKED)
        item = await questions.resolve(
            session, {"kind": "task", "status": "blocked", "department": "Moliya"},
            viewer=head, grants=grants,
        )
        own = await questions.run(session, item, viewer=head, grants=grants, now=ASKED)
    check(alien.empty, "начальнику отдела чужой отдел тоже пуст")
    check(not own.empty, "а свой — нет: пустота именно от прав", str(len(own.hits)))

    # Проверка не «фильтр по отделу всегда пуст»: у руководителя он находит.
    async with session_scope() as session:
        chief = await session.get(User, cast.chief)
        grants = await load_grants(session, chief)
        item = await questions.resolve(session, raw, viewer=chief, grants=grants)
        allowed = await questions.run(
            session, item, viewer=chief, grants=grants, now=ASKED
        )
    check(not allowed.empty, "руководителю тот же вопрос отвечает записями",
          str(len(allowed.hits)))

    # Невыполнимый фильтр не выполняется вовсе. Пара сравнивается на одном
    # и том же вопросе: иначе «пусто» доказывало бы только, что записей нет.
    async with session_scope() as session:
        chief = await session.get(User, cast.chief)
        grants = await load_grants(session, chief)
        plain = questions.Filter(kind="task", status="BLOCKED")
        able = await questions.run(session, plain, viewer=chief, grants=grants, now=ASKED)
        unable = await questions.run(
            session, questions.Filter(kind="task", status="BLOCKED", possible=False),
            viewer=chief, grants=grants, now=ASKED,
        )
    check(not able.empty, "выполнимый вопрос отвечает записями", str(len(able.hits)))
    check(unable.empty, "а невыполнимый — пуст, тот же вопрос и те же права")


async def _ask_no_writes(cast: Cast) -> None:
    print("\n16.7. Вопрос ничего не создаёт и ничего не выполняет")
    before = await _all_counts(cast.org)
    for reply in (
        '{"kind":"task","sql":"DROP TABLE tasks"}',
        '{"kind":"task","department":"\'; DROP TABLE tasks; --"}',
        '{"kind":"task","status":"blocked; delete from tasks"}',
        '{"kind":"task","person":"%\' OR 1=1 --"}',
        '{"kind":"decision","status":"cancelled","person":"_"}',
    ):
        await _asked(cast, cast.chief, reply)
    after = await _all_counts(cast.org)
    check(before == after, "после вопросов записей столько же", f"{before} → {after}")
    check(after[0] > 0, "и таблицы на месте — счёт выполнился", str(after))

    # Подставленное в название условие остаётся названием: находить по нему
    # нечего, и ответ пуст, а не «все поручения».
    injected = await _asked(
        cast, cast.chief, '{"kind":"task","department":"\'; DROP TABLE tasks; --"}'
    )
    check(injected.empty, "подставленный текст запроса ничего не выбирает")


async def _ask_limit(cast: Cast) -> None:
    print("\n16.8. Сколько строк показывать, решает система")
    async with session_scope() as session:
        chief = await session.get(User, cast.chief)
        worker = await session.get(User, cast.worker)
        for number in range(questions.MAX_ROWS + 2):
            await task_service.create_task(
                session, creator=chief, assignee=worker,
                title=f"ТЕСТ Shoshilinch {number}",
                priority=Priority.CRITICAL, due_at=ASKED + timedelta(days=3),
            )

    many = await _asked(
        cast, cast.chief, '{"kind":"task","priority":"critical","limit":1000}'
    )
    check(len(many.hits) == questions.MAX_ROWS,
          "показано ровно столько, сколько решила система", str(len(many.hits)))
    check(many.more, "и сказано, что показано не всё")

    # Обратное так же важно: когда всё поместилось, «показано не всё» лишнее.
    few = await _asked(cast, cast.chief, '{"kind":"task","status":"blocked"}')
    check(not few.more, "уместившийся ответ не притворяется урезанным",
          str(len(few.hits)))
    check(0 < len(few.hits) < questions.MAX_ROWS, "и он не пуст", str(len(few.hits)))


async def _ask_fallback(cast: Cast) -> None:
    print("\n16.9. Непонятый вопрос ищет по словам, а не показывает всё")
    unread = await _asked(cast, cast.chief, "не знаю, о чём вы")
    check(unread.searched, "не разобрав, сценарий пошёл в поиск")
    check(unread.filter is None, "и фильтра не сочинил")
    check(unread.reason == "empty", "причина записана", unread.reason)

    vague = await _asked(cast, cast.chief, '{"kind":"task"}')
    check(vague.searched, "фильтр без условий ответом не считается")
    check(vague.reason == "vague", "и причина у него другая", vague.reason)

    # Выключенный ИИ: вопрос отвечает хуже, но отвечает.
    settings.ai_enabled = False
    async with session_scope() as session:
        before = await session.scalar(
            select(func.count(AiCall.id)).where(AiCall.organization_id == cast.org)
        )
    off = await _asked(
        cast, cast.chief, '{"kind":"task","status":"blocked"}', text="Bloklangan"
    )
    async with session_scope() as session:
        after = await session.scalar(
            select(func.count(AiCall.id)).where(AiCall.organization_id == cast.org)
        )
    check(off.searched, "при выключенном ИИ вопрос уходит в поиск по словам")
    check(off.reason == "off", "и причина названа", off.reason)
    check(before == after, "обращения к модели не было", f"{before} → {after}")
    check(not off.empty, "но ответ есть: поиск по словам нашёл", str(len(off.hits)))

    # Откат к поиску идёт теми же условиями видимости: чужое не показывается.
    denied = await _asked(cast, cast.outsider, "не знаю", text="Bloklangan moliya")
    check(all(hit.kind != "task" for hit in denied.hits),
          "и в поиске по словам чужое поручение не появляется",
          str([(h.kind, h.title) for h in denied.hits]))
    settings.ai_enabled = True


async def _ask_overdue(cast: Cast) -> None:
    print("\n16.11. Просрочка в ответе считается тем же правилом, что и в сводке")
    # Три поручения на один вопрос: просроченное, выполненное с прошедшим
    # сроком и вовсе без срока. Правило обязано выбрать ровно первое.
    async with session_scope() as session:
        chief = await session.get(User, cast.chief)
        worker = await session.get(User, cast.worker)
        late = await task_service.create_task(
            session, creator=chief, assignee=worker, title="ТЕСТ Kechikkan",
            priority=Priority.LOW, due_at=ASKED - timedelta(days=3),
        )
        done = await task_service.create_task(
            session, creator=chief, assignee=worker, title="ТЕСТ Bajarilgan",
            priority=Priority.LOW, due_at=ASKED - timedelta(days=3),
        )
        done.status = TaskStatus.DONE
        await task_service.create_task(
            session, creator=chief, assignee=worker, title="ТЕСТ Muddatsiz",
            priority=Priority.LOW,
        )
        late_id, done_id = late.id, done.id

    answer = await _asked(
        cast, cast.chief, '{"kind":"task","overdue":true,"priority":"low"}'
    )
    found = {hit.id for hit in answer.hits}
    check(found == {late_id}, "просрочено ровно одно из трёх",
          f"{sorted(found)} вместо [{late_id}]")
    check(done_id not in found,
          "выполненное с прошедшим сроком просроченным не считается")
    check(len(answer.hits) == 1, "и поручение без срока не просрочено никогда",
          str(len(answer.hits)))

    # Обратное: без просрочки тот же вопрос находит все три.
    plain = await _asked(cast, cast.chief, '{"kind":"task","priority":"low"}')
    check(len(plain.hits) == 3, "без условия просрочки видны все три",
          str(len(plain.hits)))


def _ask_one_rule() -> None:
    print("\n16.12. Правило просрочки описано в одном месте")
    # Вопрос стал третьим, кто спрашивает «что просрочено», — после сводки
    # и контроля сроков. Пока описаний было два, они совпадали случайно;
    # третья копия разошлась бы, и первым признаком стало бы расхождение
    # чисел в сводке и в ответе на вопрос — то есть недоверие ко всему.
    root = Path(__file__).resolve().parents[1] / "app"
    def files_with(needle: str) -> list[str]:
        return sorted(
            str(path.relative_to(root.parent))
            for path in root.rglob("*.py")
            if needle in path.read_text(encoding="utf-8")
        )

    check(files_with("PENDING_STATUSES = (") == ["app/services/tasks.py"],
          "статусы ожидания перечислены один раз",
          str(files_with("PENDING_STATUSES = (")))
    check(files_with("Decision.due_date <") == ["app/services/decisions.py"],
          "и просрочка решения описана один раз",
          str(files_with("Decision.due_date <")))
    # Пользуются им при этом несколько мест — иначе описание одно потому,
    # что никому не нужно, а не потому что общее.
    uses = sum(
        path.read_text(encoding="utf-8").count("overdue_filter(now)")
        for path in root.rglob("*.py")
    )
    check(uses >= 4, "и общим правилом пользуются сводка, реестр и вопрос", str(uses))


async def _ask_journal(cast: Cast) -> None:
    print("\n16.10. Вопрос записан в журнал")
    async with session_scope() as session:
        await session.execute(delete(AiCall).where(AiCall.organization_id == cast.org))
    await _asked(cast, cast.chief, '{"kind":"task","status":"blocked"}')
    async with session_scope() as session:
        row = (await session.execute(
            select(AiCall).where(AiCall.organization_id == cast.org)
        )).scalars().one()
    check(row.kind == "ask", "вид вызова записан", row.kind)
    check(row.prompt_version == "ask-1", "версия промпта записана", row.prompt_version)
    check(row.model == settings.ai_model_routine, "вопрос идёт рутинной моделью",
          row.model)
    check(row.user_id == cast.chief, "и видно, кто спросил", str(row.user_id))
    check(row.ok and row.finished_at is not None, "вызов записан завершённым")
    # Подтверждать нечего: вопрос ничего не предлагает записать. Отметка
    # «не подтверждено» означала бы отказ человека, которого не было.
    check(row.confirmed is None, "подтверждения у вопроса нет и в журнале",
          str(row.confirmed))


if __name__ == "__main__":
    asyncio.run(main())
