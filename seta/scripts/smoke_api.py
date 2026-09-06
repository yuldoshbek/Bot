"""Проверка API для Mini App.

    docker compose -f docker-compose.dev.yml \
      run --rm --no-deps migrate python scripts/smoke_api.py

Здесь проверяется не «работают ли маршруты» — это видно и так. Проверяется
то, ради чего фаза написана: **API не даёт ничего сверх того, что человек
видит в боте**.

Отсюда две группы сценариев. Первая — вход: подделанная подпись, просроченная,
чужой токен, подменённый идентификатор в теле. Вторая — совпадение: на одних
и тех же данных бот и API отдают один и тот же список, и это сверяется
матрицей «запись × человек», как для документов и решений.

Анти-паттерн, который здесь и ловится: «в API проверим права попроще, там же
только чтение». Чтение и есть утечка.
"""
import asyncio
import hashlib
import hmac
import json
import sys
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.db import session_scope
from app.core.timeutil import utcnow
from app.models import Department, Organization, Task, TaskStatus, User, UserStatus
from app.models.enums import Priority, RoleCode
from app.services import tasks as task_service
from app.services.bootstrap import bootstrap, ensure_default_working_hours, grant_role
from app.services.rbac import load_grants

ORG_NAME = "ТЕСТ API"
TOKEN = settings.bot_token or "123456:TEST"

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


def sign(fields: dict[str, str], token: str = TOKEN) -> str:
    """Собирает `initData` так же, как это делает Telegram."""
    payload = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})


def init_data(telegram_id: int, *, at: int | None = None, token: str = TOKEN) -> str:
    return sign(
        {
            "user": json.dumps({"id": telegram_id, "username": "test"}),
            "auth_date": str(at if at is not None else int(time.time())),
            "query_id": "TESTQUERY",
        },
        token,
    )


async def cleanup() -> None:
    async with session_scope() as session:
        org_ids = list((await session.execute(
            select(Organization.id).where(Organization.name == ORG_NAME)
        )).scalars().all())
        if not org_ids:
            return
        # Только по organization_id: чужие записи не трогаем никогда.
        from app.models.task import TaskEvent

        await session.execute(delete(TaskEvent).where(TaskEvent.task_id.in_(
            select(Task.id).where(Task.organization_id.in_(org_ids))
        )))
        from app.models.notification import Notification

        await session.execute(
            delete(Notification).where(Notification.organization_id.in_(org_ids))
        )
        await session.execute(delete(Task).where(Task.organization_id.in_(org_ids)))
        from app.models.rbac import UserRole

        await session.execute(delete(UserRole).where(UserRole.user_id.in_(
            select(User.id).where(User.organization_id.in_(org_ids))
        )))
        await session.execute(delete(User).where(User.organization_id.in_(org_ids)))
        await session.execute(delete(Department).where(
            Department.organization_id.in_(org_ids)
        ))
        await session.execute(delete(Organization).where(Organization.id.in_(org_ids)))


async def seed() -> dict[str, int]:
    """Двое в одном отделе, третий в другом. У каждого своё поручение."""
    async with session_scope() as session:
        await bootstrap(session)
        org = Organization(name=ORG_NAME, timezone="Asia/Tashkent")
        session.add(org)
        await session.flush()

        sales = Department(organization_id=org.id, name="ТЕСТ Продажи")
        supply = Department(organization_id=org.id, name="ТЕСТ Снабжение")
        session.add_all([sales, supply])
        await session.flush()

        people = {}
        for key, name, tg, dept, role in (
            ("chief", "ТЕСТ Рахбар", 991_001, None, RoleCode.EXECUTIVE),
            ("head", "ТЕСТ Boshliq", 991_002, sales.id, RoleCode.DEPT_HEAD),
            ("worker", "ТЕСТ Xodim", 991_003, sales.id, RoleCode.EMPLOYEE),
            ("alien", "ТЕСТ Begona", 991_004, supply.id, RoleCode.EMPLOYEE),
            # Отдельный человек для проверки ограничителя: его счётчик частоты
            # никому не мешает, даже если прогон прервали на середине перебора.
            ("burner", "ТЕСТ Yuklama", 991_007, supply.id, RoleCode.EMPLOYEE),
        ):
            person = User(
                organization_id=org.id, telegram_user_id=tg, full_name=name,
                status=UserStatus.ACTIVE, timezone="Asia/Tashkent",
                department_id=dept, locale="uz",
            )
            session.add(person)
            await session.flush()
            await grant_role(session, user=person, role_code=role)
            await ensure_default_working_hours(session, person)
            people[key] = person.id

        # Заявка на рассмотрении и приостановленный доступ: в боте такие
        # проходят дальше, чтобы закончить регистрацию, а в приложении им
        # делать нечего — регистрация идёт в чате.
        for key, tg, state in (
            ("pending", 991_005, UserStatus.PENDING),
            ("suspended", 991_006, UserStatus.SUSPENDED),
        ):
            person = User(
                organization_id=org.id, telegram_user_id=tg,
                full_name=f"ТЕСТ {key}", status=state,
                timezone="Asia/Tashkent", locale="uz",
            )
            session.add(person)
            await session.flush()
            people[key] = person.id

        chief = await session.get(User, people["chief"])
        for owner_key, title in (
            ("worker", "TEST xodim topshirigʻi"),
            ("head", "TEST boshliq topshirigʻi"),
            ("alien", "TEST begona topshirigʻi"),
        ):
            assignee = await session.get(User, people[owner_key])
            await task_service.create_task(
                session, creator=chief, assignee=assignee,
                title=title, priority=Priority.NORMAL,
                due_at=utcnow() + timedelta(days=2),
            )
        return people


async def main() -> None:
    await cleanup()
    people = await seed()

    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://api"
    ) as client:
        await stage_entry(client)
        await stage_source()
        await stage_reading(client, people)
        await stage_agreement(client, people)
        await stage_rate(client)

    await cleanup()
    async with session_scope() as session:
        left = await session.scalar(
            select(Organization.id).where(Organization.name == ORG_NAME)
        )
    check(left is None, "тестовая организация убрана")

    print(f"\n{'=' * 50}\nПройдено: {passed}   Ошибок: {failed}\n{'=' * 50}")
    sys.exit(1 if failed else 0)


def _app():
    """Приложение без `lifespan`: подниматься целиком проверке не нужно.

    `lifespan` ставит вебхук и обращается к Telegram — в проверке это лишняя
    сеть и лишний способ упасть по чужой причине.
    """
    from fastapi import FastAPI

    from app.api.v1 import routes as v1_routes

    app = FastAPI()
    app.include_router(v1_routes.router, prefix="/api/v1")
    return app


async def stage_entry(client: httpx.AsyncClient) -> None:
    print("\n1. Вход: пускает только подписанная строка")
    now = int(time.time())

    answer = await client.get("/api/v1/me")
    check(answer.status_code == 401, "без initData — отказ", str(answer.status_code))

    answer = await client.get(
        "/api/v1/me", headers={"X-Telegram-Init-Data": "user=%7B%22id%22%3A991001%7D"}
    )
    check(answer.status_code == 401, "без подписи — отказ", str(answer.status_code))

    # Подпись верна, но чужим токеном: так выглядит подделка от того, кто
    # знает формат, но не знает токена.
    answer = await client.get(
        "/api/v1/me",
        headers={"X-Telegram-Init-Data": init_data(991_001, token="999:OTHER")},
    )
    check(answer.status_code == 401, "подпись чужим токеном — отказ", str(answer.status_code))

    # Подписанная строка, в которой поменяли идентификатор после подписи.
    tampered = init_data(991_001).replace("991001", "991004")
    answer = await client.get("/api/v1/me", headers={"X-Telegram-Init-Data": tampered})
    check(answer.status_code == 401, "правка после подписи — отказ", str(answer.status_code))

    old = init_data(991_001, at=now - 25 * 60 * 60)
    answer = await client.get("/api/v1/me", headers={"X-Telegram-Init-Data": old})
    check(answer.status_code == 401, "просроченная строка — отказ", str(answer.status_code))

    future = init_data(991_001, at=now + 3600)
    answer = await client.get("/api/v1/me", headers={"X-Telegram-Init-Data": future})
    check(answer.status_code == 401, "время из будущего — отказ", str(answer.status_code))

    # Подпись верна, но такого человека в системе нет.
    answer = await client.get(
        "/api/v1/me", headers={"X-Telegram-Init-Data": init_data(999_999_999)}
    )
    check(answer.status_code == 403, "незнакомец — отказ", str(answer.status_code))

    # Подпись верна, человек в базе есть — но доступ ему не открыт.
    answer = await client.get(
        "/api/v1/me", headers={"X-Telegram-Init-Data": init_data(991_005)}
    )
    check(answer.status_code == 403, "заявка на рассмотрении — отказ",
          str(answer.status_code))
    answer = await client.get(
        "/api/v1/me", headers={"X-Telegram-Init-Data": init_data(991_006)}
    )
    check(answer.status_code == 403, "приостановленный — отказ", str(answer.status_code))

    answer = await client.get(
        "/api/v1/me", headers={"X-Telegram-Init-Data": init_data(991_001)}
    )
    check(answer.status_code == 200, "верная строка — пускает", str(answer.status_code))
    body = answer.json()
    check(body["full_name"] == "ТЕСТ Рахбар", "и это тот, кто подписан", str(body)[:80])


async def stage_rate(client: httpx.AsyncClient) -> None:
    print("\n5. Ограничитель частоты держит перебор")
    from app.api.deps import RATE_LIMIT, RATE_WINDOW_SECONDS
    from app.core.redis import redis

    # Предел проверяется отдельно от механизма: сценарий, который берёт число
    # из той же константы, подтвердит работу счётчика и молча примет любое
    # её значение — хоть миллион. Разумность предела проверяется числом.
    check(30 <= RATE_LIMIT <= 600, f"предел разумен: {RATE_LIMIT} запросов")
    check(RATE_WINDOW_SECONDS <= 300, f"окно короткое: {RATE_WINDOW_SECONDS} с")

    # Свой человек и свой счётчик: иначе перебор здесь закрыл бы доступ
    # остальным сценариям в том же окне.
    await redis.delete("api:rate:991007")
    headers = {"X-Telegram-Init-Data": init_data(991_007)}
    # Длина цикла ограничена сверху числом, а не только константой: подняв
    # предел до миллиона, проверка сама себя подвесила бы на миллионе запросов
    # вместо того чтобы сообщить об ошибке. Разумность предела ловит проверка
    # выше, а эта обязана завершиться при любом его значении.
    attempts = min(RATE_LIMIT + 3, 200)
    codes = []
    for _ in range(attempts):
        answer = await client.get("/api/v1/me", headers=headers)
        codes.append(answer.status_code)
    check(429 in codes, "перебор упирается в предел",
          f"кодов 429: {codes.count(429)} из {len(codes)}")
    check(codes[0] == 200, "а первые запросы проходят", str(codes[:3]))

    # У счётчика обязан быть срок жизни. Без него предел не «на минуту»,
    # а навсегда: человек, разок открывший приложение слишком бойко, потерял бы
    # доступ до перезапуска Redis. Изнутри одного прогона это не видно вовсе —
    # видно только по самому ключу.
    ttl = await redis.ttl("api:rate:991007")
    check(0 < ttl <= RATE_WINDOW_SECONDS,
          f"счётчик истекает сам: осталось {ttl} с", str(ttl))
    await redis.delete("api:rate:991007")


async def stage_source() -> None:
    print("\n2. Подпись сверяется постоянным временем")
    # Это свойство не видно снаружи: обычное `==` отвергает подделку так же
    # исправно. Разница — во времени ответа, по которому подпись подбирается
    # посимвольно. Проверять такое замерами ненадёжно, поэтому проверяется
    # исходник: сравнение обязано идти через `hmac.compare_digest`.
    source = (Path(__file__).resolve().parents[1] / "app/api/initdata.py").read_text(
        encoding="utf-8"
    )
    check("hmac.compare_digest(" in source,
          "сверка идёт через hmac.compare_digest")
    check("expected != given" not in source and "given != expected" not in source,
          "и обычного сравнения подписи в файле нет")
    # Токен не должен попадать в журнал ни при какой ошибке.
    check("bot_token" not in source.split("def check")[1].split("log")[0]
          or "log" not in source,
          "токен не уходит в журнал")


async def stage_reading(client: httpx.AsyncClient, people: dict[str, int]) -> None:
    print("\n3. Ответ не богаче того, что видно в боте")

    head = {"X-Telegram-Init-Data": init_data(991_003)}  # рядовой сотрудник

    answer = await client.get("/api/v1/me", headers=head)
    body = answer.json()
    check("features" in body and "locale" in body,
          "«кто я» отдаёт язык и включённые разделы", str(sorted(body))[:90])
    check("telegram_user_id" not in body and "phone" not in body,
          "и не отдаёт телефон и Telegram ID", str(sorted(body))[:90])

    answer = await client.get("/api/v1/tasks", headers=head)
    check(answer.status_code == 200, "список поручений открывается")
    items = answer.json()["items"]
    check(all("assignee_id" not in item for item in items),
          "в списке нет внутренних идентификаторов людей", str(items[:1])[:90])
    check(all(item.get("status_title") for item in items),
          "и статус подписан словом, а не кодом", str(items[:1])[:90])

    answer = await client.get("/api/v1/tasks?bucket=выдумка", headers=head)
    check(answer.status_code == 400, "неизвестный разрез — отказ", str(answer.status_code))

    answer = await client.get("/api/v1/day", headers=head)
    check(answer.status_code == 200, "экран «Мой день» открывается")


async def stage_agreement(client: httpx.AsyncClient, people: dict[str, int]) -> None:
    print("\n4. Бот и API отдают одно и то же")
    # Матрица «запись × человек»: для каждого поручения и каждого человека
    # ответ API обязан совпасть с ответом службы, которой отвечает бот.
    async with session_scope() as session:
        rows = list((await session.execute(
            select(Task).join(Organization, Organization.id == Task.organization_id)
            .where(Organization.name == ORG_NAME).order_by(Task.id)
        )).scalars().all())
        check(len(rows) == 3, "заведено три поручения", str(len(rows)))

        mismatch = []
        for key, telegram_id in (
            ("chief", 991_001), ("head", 991_002),
            ("worker", 991_003), ("alien", 991_004),
        ):
            viewer = await session.get(User, people[key])
            grants = await load_grants(session, viewer)
            headers = {"X-Telegram-Init-Data": init_data(telegram_id)}
            for task in rows:
                access = await task_service.access_for(session, task, viewer, grants)
                answer = await client.get(f"/api/v1/tasks/{task.id}", headers=headers)
                opened = answer.status_code == 200
                if opened != access.can_view:
                    mismatch.append(
                        f"{key}×{task.id}: бот={access.can_view}, API={opened}"
                    )
        check(not mismatch, f"матрица сошлась: {len(rows)} × 4 человека",
              "; ".join(mismatch[:3]))

        # Ответ на чужое поручение неотличим от ответа на несуществующее:
        # иначе перебором составляется список чужих записей.
        alien_headers = {"X-Telegram-Init-Data": init_data(991_004)}
        foreign = [t for t in rows if t.assignee_id != people["alien"]]
        if foreign:
            hidden = await client.get(
                f"/api/v1/tasks/{foreign[0].id}", headers=alien_headers
            )
            missing = await client.get("/api/v1/tasks/999999999", headers=alien_headers)
            check(
                hidden.status_code == missing.status_code == 404,
                "чужое поручение отвечает так же, как несуществующее",
                f"чужое={hidden.status_code}, нет такого={missing.status_code}",
            )

        # Список: то же множество, что отдаёт служба боту.
        worker = await session.get(User, people["worker"])
        by_service = await task_service.my_tasks(session, worker, bucket="active")
        answer = await client.get(
            "/api/v1/tasks?bucket=active",
            headers={"X-Telegram-Init-Data": init_data(991_003)},
        )
        by_api = [item["id"] for item in answer.json()["items"]]
        check(sorted(by_api) == sorted(t.id for t in by_service),
              "список поручений совпадает с ботовым",
              f"API={sorted(by_api)}, служба={sorted(t.id for t in by_service)}")


if __name__ == "__main__":
    asyncio.run(main())
