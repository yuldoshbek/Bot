"""Нагрузочная и злонамеренная проверка бота.

Прогоняет настоящие Telegram-обновления через настоящий диспетчер, настоящие
middleware и настоящую базу. Наружу ничего не уходит: сетевой слой подменён,
все ответы бота перехватываются и проверяются.

Проверяется не то, что система работает, когда её используют правильно,
а то, что она не ломается, когда по ней бьют:

  - одна и та же кнопка нажата десять раз подряд;
  - десять нажатий одновременно (гонка за один объект);
  - чужой человек жмёт кнопки чужого поручения;
  - подставленный чужой идентификатор в callback;
  - мусорный ввод: HTML, эмодзи, 5000 символов, кривые даты;
  - скорость ответа под нагрузкой.

    docker compose -f docker-compose.yml -f docker-compose.dev.yml \
        run --rm --no-deps migrate python scripts/stress_test.py
"""
import asyncio
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import TelegramMethod
from aiogram.types import Update
from sqlalchemy import delete, func, select

from app.bot.handlers import admin, availability, menu, start, tasks
from app.bot.middlewares.auth import AuthMiddleware
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
    User,
    UserRole,
    UserStatus,
    WorkingHours,
)
from app.services.bootstrap import bootstrap, ensure_default_working_hours, grant_role
from app.services.tasks import create_task

TEST_ORG = "ТЕСТ Нагрузка"
TG = {"admin": 920_000_001, "chief": 920_000_002, "head": 920_000_003,
      "worker": 920_000_004, "stranger": 920_000_005, "newbie": 920_000_006}

passed = 0
failed = 0
findings: list[str] = []


def check(condition: bool, title: str, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  OK   {title}")
    else:
        failed += 1
        print(f"  FAIL {title} {detail}")
        findings.append(f"{title} — {detail}")


# Теги, которые Telegram понимает в режиме HTML. Всё остальное он отвергает
# целиком: сообщение не доходит ни до кого.
ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "a", "code", "pre", "span", "tg-spoiler", "blockquote", "tg-emoji", "br",
}
TAG_RE = re.compile(r"<\s*(/?)\s*([a-zA-Z0-9-]+)[^>]*>")


def check_telegram_html(text: str) -> str | None:
    """Повторяет строгость Telegram: чужой тег или незакрытый — сообщение не уйдёт."""
    stack: list[str] = []
    for closing, tag in TAG_RE.findall(text):
        tag = tag.lower()
        if tag not in ALLOWED_TAGS:
            return f"неподдерживаемый тег <{tag}> — Telegram откажет в отправке"
        if tag == "br":
            continue
        if closing:
            if not stack or stack[-1] != tag:
                return f"неверно закрыт тег <{tag}>"
            stack.pop()
        else:
            stack.append(tag)
    if stack:
        return f"незакрытый тег <{stack[-1]}>"
    return None


# ─────────────────────────  ПОДДЕЛЬНАЯ СЕТЬ  ─────────────────────────
class CapturingSession(BaseSession):
    """Вместо обращения к Telegram складывает вызовы в список.

    Возвращает правдоподобные ответы, чтобы обработчики шли по обычному пути.
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.errors: list[str] = []
        self.message_id = 1000

    async def close(self) -> None:
        return None

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        yield b""

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None):
        name = type(method).__name__
        data = method.model_dump(exclude_none=True)
        self.calls.append((name, data))

        if name in ("AnswerCallbackQuery", "DeleteWebhook", "SetWebhook",
                    "EditMessageReplyMarkup"):
            return True
        if name == "GetMe":
            return _user_payload(1, "seta_test_bot", is_bot=True)
        if name in ("SendMessage", "EditMessageText"):
            self.message_id += 1
            chat_id = data.get("chat_id", 0)
            text = data.get("text", "")
            if len(text) > 4096:
                self.errors.append(f"{name}: текст {len(text)} символов — Telegram отклонит")
            problem = check_telegram_html(text)
            if problem:
                self.errors.append(f"{name}: {problem}")
            return _message_payload(self.message_id, chat_id, text)
        return True


def _user_payload(user_id: int, username: str, is_bot: bool = False) -> dict:
    return {"id": user_id, "is_bot": is_bot, "first_name": username, "username": username}


def _message_payload(message_id: int, chat_id: int, text: str) -> dict:
    return {
        "message_id": message_id,
        "date": int(datetime.now(tz=timezone.utc).timestamp()),
        "chat": {"id": chat_id, "type": "private"},
        "from": _user_payload(1, "seta_test_bot", is_bot=True),
        "text": text,
    }


def make_bot() -> tuple[Bot, CapturingSession]:
    session = CapturingSession()
    bot = Bot(
        token="123456:AAHtestTOKENtestTOKENtestTOKENtestT",
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    return bot, session


def make_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.middleware(AuthMiddleware())
    dp.callback_query.middleware(AuthMiddleware())
    for module in (start, availability, admin, tasks, menu):
        dp.include_router(module.router)
    return dp


_update_id = 0


def text_update(bot: Bot, telegram_id: int, text: str) -> Update:
    global _update_id
    _update_id += 1
    return Update.model_validate(
        {
            "update_id": _update_id,
            "message": {
                "message_id": _update_id,
                "date": int(datetime.now(tz=timezone.utc).timestamp()),
                "chat": {"id": telegram_id, "type": "private"},
                "from": _user_payload(telegram_id, f"user{telegram_id}"),
                "text": text,
            },
        },
        context={"bot": bot},
    )


def contact_update(bot: Bot, telegram_id: int, phone: str) -> Update:
    global _update_id
    _update_id += 1
    return Update.model_validate(
        {
            "update_id": _update_id,
            "message": {
                "message_id": _update_id,
                "date": int(datetime.now(tz=timezone.utc).timestamp()),
                "chat": {"id": telegram_id, "type": "private"},
                "from": _user_payload(telegram_id, f"user{telegram_id}"),
                "contact": {"phone_number": phone, "first_name": "Тест", "user_id": telegram_id},
            },
        },
        context={"bot": bot},
    )


def callback_update(bot: Bot, telegram_id: int, data: str) -> Update:
    global _update_id
    _update_id += 1
    return Update.model_validate(
        {
            "update_id": _update_id,
            "callback_query": {
                "id": str(_update_id),
                "from": _user_payload(telegram_id, f"user{telegram_id}"),
                "chat_instance": "test",
                "data": data,
                "message": {
                    "message_id": _update_id,
                    "date": int(datetime.now(tz=timezone.utc).timestamp()),
                    "chat": {"id": telegram_id, "type": "private"},
                    "from": _user_payload(1, "seta_test_bot", is_bot=True),
                    "text": "предыдущее сообщение",
                },
            },
        },
        context={"bot": bot},
    )


async def feed(dp: Dispatcher, bot: Bot, update: Update) -> str | None:
    """Прогоняет обновление. Возвращает текст ошибки, если обработчик упал."""
    try:
        await dp.feed_update(bot, update)
        return None
    except Exception as error:  # noqa: BLE001 - ловим всё, это и есть проверка
        return f"{type(error).__name__}: {error}"


# ─────────────────────────  ДАННЫЕ  ─────────────────────────
async def cleanup() -> None:
    async with session_scope() as session:
        org_ids = [
            row[0] for row in (
                await session.execute(
                    select(Organization.id).where(Organization.name.like("ТЕСТ %"))
                )
            ).all()
        ]
        extra = [
            row[0] for row in (
                await session.execute(
                    select(User.id).where(User.telegram_user_id.in_(list(TG.values())))
                )
            ).all()
        ]
        user_ids = extra
        if org_ids:
            user_ids += [
                row[0] for row in (
                    await session.execute(select(User.id).where(User.organization_id.in_(org_ids)))
                ).all()
            ]
        user_ids = list(set(user_ids))

        if user_ids:
            task_ids = [
                row[0] for row in (
                    await session.execute(
                        select(Task.id).where(
                            (Task.creator_id.in_(user_ids)) | (Task.assignee_id.in_(user_ids))
                        )
                    )
                ).all()
            ]
            if task_ids:
                for model in (TaskEvent, TaskComment, TaskExtension):
                    await session.execute(delete(model).where(model.task_id.in_(task_ids)))
                await session.execute(delete(Task).where(Task.id.in_(task_ids)))
            for model in (UserRole, WorkingHours, Notification):
                await session.execute(delete(model).where(model.user_id.in_(user_ids)))
            await session.execute(delete(AuditLog).where(AuditLog.actor_id.in_(user_ids)))
            await session.execute(delete(User).where(User.id.in_(user_ids)))
        if org_ids:
            await session.execute(delete(Department).where(Department.organization_id.in_(org_ids)))
            await session.execute(delete(Organization).where(Organization.id.in_(org_ids)))


async def seed() -> dict[str, int]:
    async with session_scope() as session:
        await bootstrap(session)
        org = Organization(name=TEST_ORG, timezone="Asia/Tashkent")
        session.add(org)
        await session.flush()

        department = Department(organization_id=org.id, name="ТЕСТ Отдел")
        session.add(department)
        await session.flush()

        ids: dict[str, int] = {"org": org.id, "department": department.id}
        for key, role in (
            ("admin", RoleCode.ADMIN), ("chief", RoleCode.EXECUTIVE),
            ("head", RoleCode.DEPT_HEAD), ("worker", RoleCode.EMPLOYEE),
            ("stranger", RoleCode.EMPLOYEE),
        ):
            # У одного сотрудника имя с угловыми скобками: любой экран, где
            # показывается ФИО, обязан пережить это без поломки разметки.
            name = f"ТЕСТ <{key}> и <b>жирный</b>" if key == "worker" else f"ТЕСТ {key}"
            person = User(
                organization_id=org.id,
                telegram_user_id=TG[key],
                full_name=name,
                status=UserStatus.ACTIVE,
                department_id=department.id if key in ("head", "worker") else None,
                timezone="Asia/Tashkent",
                locale="ru",
            )
            session.add(person)
            await session.flush()
            await ensure_default_working_hours(session, person)
            await grant_role(session, person, role)
            ids[key] = person.id
        return ids


# ─────────────────────────  ПРОВЕРКИ  ─────────────────────────
async def main() -> None:
    await cleanup()
    ids = await seed()
    bot, net = make_bot()
    dp = make_dispatcher()
    timings: list[float] = []

    async def hit(update: Update) -> str | None:
        started = time.perf_counter()
        error = await feed(dp, bot, update)
        timings.append(time.perf_counter() - started)
        return error

    print("\n1. Десять нажатий /start подряд")
    errors = [await hit(text_update(bot, TG["worker"], "/start")) for _ in range(10)]
    check(not any(errors), "десять /start не уронили бота", str(next((e for e in errors if e), "")))
    async with session_scope() as session:
        count = await session.scalar(
            select(func.count(User.id)).where(User.telegram_user_id == TG["worker"])
        )
    check(count == 1, "повторные /start не создали второго пользователя", f"найдено {count}")

    print("\n2. Десять нажатий «Мои поручения» подряд")
    errors = [await hit(text_update(bot, TG["worker"], "📋 Мои поручения")) for _ in range(10)]
    check(not any(errors), "список выдержал десять открытий", str(next((e for e in errors if e), "")))

    print("\n3. Десять переключений фильтра — одно и то же сообщение правится подряд")
    errors = [await hit(callback_update(bot, TG["worker"], "tl:active")) for _ in range(10)]
    check(not any(errors), "повторная правка того же сообщения не уронила обработчик",
          str(next((e for e in errors if e), "")))

    print("\n4. Гонка: десять одновременных «Принять» на одном поручении")
    async with session_scope() as session:
        head = await session.get(User, ids["head"])
        worker = await session.get(User, ids["worker"])
        task = await create_task(
            session, creator=head, assignee=worker, title="Гонка за приём",
            priority=Priority.NORMAL,
        )
        race_id = task.id

    results = await asyncio.gather(
        *[feed(dp, bot, callback_update(bot, TG["worker"], f"t:accept:{race_id}")) for _ in range(10)]
    )
    check(not any(results), "одновременные нажатия не вызвали исключений",
          str(next((r for r in results if r), "")))
    async with session_scope() as session:
        accepted_events = await session.scalar(
            select(func.count(TaskEvent.id)).where(
                TaskEvent.task_id == race_id, TaskEvent.kind == "ACCEPTED"
            )
        )
        status = await session.scalar(select(Task.status).where(Task.id == race_id))
    check(status == TaskStatus.ACKNOWLEDGED, "статус изменился корректно", f"статус {status}")
    check(accepted_events == 1, "приём записан в историю один раз, а не десять",
          f"записей: {accepted_events}")

    print("\n5. Десять «Выполнено» подряд по одному поручению")
    errors = [await hit(callback_update(bot, TG["worker"], f"t:submit:{race_id}")) for _ in range(10)]
    check(not any(errors), "повторные отчёты не уронили обработчик",
          str(next((e for e in errors if e), "")))
    async with session_scope() as session:
        done_events = await session.scalar(
            select(func.count(TaskEvent.id)).where(
                TaskEvent.task_id == race_id, TaskEvent.kind.in_(["COMPLETED", "SUBMITTED"])
            )
        )
    check(done_events == 1, "закрытие записано один раз", f"записей: {done_events}")

    print("\n6. Чужой человек жмёт кнопки чужого поручения")
    outcomes = []
    for action in ("open", "accept", "submit", "approve", "cancel", "comment"):
        outcomes.append(await hit(callback_update(bot, TG["stranger"], f"t:{action}:{race_id}")))
    check(not any(outcomes), "отказы обрабатываются без исключений",
          str(next((o for o in outcomes if o), "")))
    async with session_scope() as session:
        foreign_events = await session.scalar(
            select(func.count(TaskEvent.id)).where(
                TaskEvent.task_id == race_id, TaskEvent.actor_id == ids["stranger"]
            )
        )
    check(foreign_events == 0, "чужой не оставил следов в чужом поручении",
          f"событий: {foreign_events}")

    print("\n7. Подставленные и несуществующие идентификаторы")
    probes = ["t:open:999999", "t:accept:0", "t:approve:-1", "t:open:abc",
              "nt:who:999999", "adm:approve:999999", "av:OPEN:zzz"]
    probe_errors = [await hit(callback_update(bot, TG["worker"], p)) for p in probes]
    check(not any(probe_errors), "мусор в callback не роняет бота",
          str(next((e for e in probe_errors if e), "")))

    print("\n8. Мусорный ввод в создание поручения")
    await hit(text_update(bot, TG["head"], "➕ Поручение"))
    await hit(callback_update(bot, TG["head"], f"nt:who:{ids['worker']}"))

    junk = [
        "<b>жирный</b> и <script>alert(1)</script>",
        "🔥" * 200,
        "А" * 5000,
        "'; DROP TABLE tasks; --",
    ]
    junk_errors = []
    for value in junk:
        junk_errors.append(await hit(text_update(bot, TG["head"], value)))
        # после каждого названия бот спрашивает срок — отвечаем и возвращаемся к началу
        await hit(callback_update(bot, TG["head"], "nt:due:none"))
        await hit(callback_update(bot, TG["head"], "nt:prio:NORMAL"))
        await hit(text_update(bot, TG["head"], "➕ Поручение"))
        await hit(callback_update(bot, TG["head"], f"nt:who:{ids['worker']}"))
    check(not any(junk_errors), "мусорный ввод не уронил создание",
          str(next((e for e in junk_errors if e), "")))

    async with session_scope() as session:
        rows = await session.execute(
            select(Task.title).where(Task.creator_id == ids["head"]).order_by(Task.id.desc()).limit(4)
        )
        titles = [row[0] for row in rows.all()]
    check(all(len(t) <= 300 for t in titles), "длинные названия обрезаны до предела поля",
          f"максимум {max((len(t) for t in titles), default=0)}")
    check(
        any("DROP TABLE" in t for t in titles),
        "текст сохранён как текст — инъекция не выполнилась",
    )
    async with session_scope() as session:
        alive = await session.scalar(select(func.count(Task.id)))
    check(alive > 0, "таблица поручений на месте после попытки инъекции")

    async with session_scope() as session:
        rows = await session.execute(
            select(Task.id).where(Task.creator_id == ids["head"]).order_by(Task.id.desc()).limit(4)
        )
        junk_ids = [row[0] for row in rows.all()]
    errors_before = len(net.errors)
    for junk_id in junk_ids:
        await hit(callback_update(bot, TG["head"], f"t:open:{junk_id}"))
        await hit(callback_update(bot, TG["worker"], f"t:open:{junk_id}"))
    check(
        len(net.errors) == errors_before,
        "карточка с тегами в названии отправляется без ошибок разметки",
        "; ".join(net.errors[errors_before:][:2]),
    )

    print("\n9. Кривые даты десять раз подряд")
    await hit(text_update(bot, TG["head"], "Нормальная задача"))
    bad_dates = ["вчера позавчера", "32.13.2026", "когда-нибудь", "!!!", "0",
                 "31 фывапролджа", "-5 дней", "99:99", "  ", "0.0.0"]
    date_errors = [await hit(text_update(bot, TG["head"], value)) for value in bad_dates]
    check(not any(date_errors), "кривые даты не роняют шаг ввода срока",
          str(next((e for e in date_errors if e), "")))
    replies = [d.get("text", "") for n, d in net.calls[-10:] if n == "SendMessage"]
    check(any("Не понял срок" in r for r in replies), "на кривую дату бот отвечает понятно")

    print("\n10. Десять запросов на продление одного поручения")
    async with session_scope() as session:
        head = await session.get(User, ids["head"])
        worker = await session.get(User, ids["worker"])
        from app.core.dates import parse_due

        ext_task = await create_task(
            session, creator=head, assignee=worker, title="Задача для продлений",
            due_at=parse_due("завтра", worker.timezone), priority=Priority.NORMAL,
        )
        ext_id = ext_task.id

    for _ in range(10):
        await hit(callback_update(bot, TG["worker"], f"t:ext:{ext_id}"))
        await hit(text_update(bot, TG["worker"], "через 5 дней"))
        await hit(text_update(bot, TG["worker"], "нужно больше времени"))

    async with session_scope() as session:
        pending = await session.scalar(
            select(func.count(TaskExtension.id)).where(
                TaskExtension.task_id == ext_id, TaskExtension.status == "NEW"
            )
        )
    check(pending == 1, "открытым остаётся один запрос на продление, а не десять",
          f"открытых запросов: {pending}")

    print("\n11. Ответы бота и отзывчивость")
    check(
        not net.errors,
        "за весь прогон ни одно сообщение не было отвергнуто Telegram",
        "; ".join(net.errors[:3]),
    )
    slow = [t for t in timings if t > 1.0]
    average = sum(timings) / len(timings) if timings else 0
    check(
        average < 0.5,
        f"среднее время обработки {average * 1000:.0f} мс",
        f"замеров: {len(timings)}",
    )
    check(not slow, "нет обновлений дольше секунды", f"медленных: {len(slow)}")

    await bot.session.close()
    await cleanup()

    print(f"\n{'=' * 52}\nПройдено: {passed}   Ошибок: {failed}")
    if findings:
        print("\nНайденные проблемы:")
        for item in findings:
            print(f"  • {item}")
    print("=" * 52)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
