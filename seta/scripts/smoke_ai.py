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
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, func, select

from app.ai import gate
from app.ai.provider import Fake
from app.core.config import settings
from app.core.db import session_scope
from app.core.timeutil import utcnow
from app.models import AiCall, Decision, Organization, Task, User, UserStatus

ORG_NAME = "ТЕСТ ИИ"

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
        org_ids = list((await session.execute(
            select(Organization.id).where(Organization.name == ORG_NAME)
        )).scalars().all())
        if not org_ids:
            return
        # Только по organization_id: чужие записи не трогаем никогда.
        await session.execute(delete(AiCall).where(AiCall.organization_id.in_(org_ids)))
        await session.execute(delete(Task).where(Task.organization_id.in_(org_ids)))
        await session.execute(
            delete(Decision).where(Decision.organization_id.in_(org_ids))
        )
        await session.execute(delete(User).where(User.organization_id.in_(org_ids)))
        await session.execute(
            delete(Organization).where(Organization.id.in_(org_ids))
        )


async def seed() -> tuple[int, int]:
    async with session_scope() as session:
        org = Organization(name=ORG_NAME, timezone="Asia/Tashkent")
        session.add(org)
        await session.flush()
        person = User(
            organization_id=org.id, telegram_user_id=993_001,
            full_name="ТЕСТ Rahbar", status=UserStatus.ACTIVE,
            timezone="Asia/Tashkent", locale="uz",
        )
        session.add(person)
        await session.flush()
        return org.id, person.id


async def main() -> None:
    await cleanup()
    org_id, user_id = await seed()
    was_enabled = settings.ai_enabled
    try:
        await stage_off(org_id, user_id)
        await stage_journal(org_id, user_id)
        await stage_budget(org_id, user_id)
        await stage_no_writes(org_id, user_id)
        await stage_provider(org_id, user_id)
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


if __name__ == "__main__":
    asyncio.run(main())
