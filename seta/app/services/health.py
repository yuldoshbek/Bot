"""Состояние системы и журнал ошибок.

Владелец системы — не программист, и `docker compose logs` для него не способ
узнать, что случилось. Поэтому состояние собирается здесь и показывается
страницей: живы ли службы, не встала ли очередь, что сломалось за сутки.

Признак «жива ли служба» — отметка в Redis с коротким сроком жизни. Процесс
раз в полминуты обновляет её; если отметка пропала, служба молчит дольше
допустимого, и это видно, даже когда контейнер формально запущен.
"""
import hashlib
import logging
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import desc, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import session_scope
from app.core.redis import redis
from app.core.timeutil import utcnow
from app.models.enums import NotificationStatus, TaskStatus, UserStatus
from app.models.error import ErrorLog
from app.models.notification import Notification
from app.models.task import Task
from app.models.user import User

log = logging.getLogger("seta.health")

# Отметка живёт минуту: служба обновляет её каждые 30 секунд, и одна
# пропущенная отметка ещё не повод считать её мёртвой.
HEARTBEAT_TTL = 90
HEARTBEAT_INTERVAL = 30

# Пороги, после которых состояние считается нездоровым.
QUEUE_LAG_LIMIT = 120        # секунд отставания очереди
ERRORS_ALERT = 10            # ошибок за час

SERVICES = {
    "bot": "Бот",
    "worker:delivery": "Доставка уведомлений",
    "worker:deadlines": "Контроль сроков",
    "worker:holds": "Освобождение окон",
}


async def beat(name: str) -> None:
    """Отметка «я жива» от процесса."""
    try:
        await redis.set(f"hb:{name}", utcnow().isoformat(), ex=HEARTBEAT_TTL)
    except Exception:  # состояние не должно ронять саму работу
        log.debug("не удалось записать отметку %s", name, exc_info=True)


async def last_beat(name: str) -> datetime | None:
    try:
        value = await redis.get(f"hb:{name}")
    except Exception:
        return None
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# ── Запись ошибок ───────────────────────────────────────────────────────────
def fingerprint_of(kind: str, message: str) -> str:
    """Отпечаток ошибки: одинаковые падения схлопываются в одну строку."""
    base = f"{kind}|{message[:200]}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:32]


async def record_error(
    exception: BaseException,
    *,
    source: str = "bot",
    context: str | None = None,
    telegram_user_id: int | None = None,
) -> None:
    """Записывает ошибку в журнал. Никогда не бросает исключение сама.

    Отдельная транзакция: ошибка часто случается там, где основная уже
    откатывается, и запись о ней должна пережить этот откат.
    """
    kind = type(exception).__name__
    message = str(exception)[:2000] or kind
    details = "".join(
        traceback.format_exception(type(exception), exception, exception.__traceback__)
    )[-4000:]
    mark = fingerprint_of(kind, message)

    try:
        async with session_scope() as session:
            statement = (
                pg_insert(ErrorLog)
                .values(
                    source=source,
                    kind=kind,
                    message=message,
                    details=details,
                    context=(context or "")[:255] or None,
                    telegram_user_id=telegram_user_id,
                    fingerprint=mark,
                    seen_count=1,
                    occurred_at=utcnow(),
                    last_seen_at=utcnow(),
                )
                .returning(ErrorLog.id)
            )
            await session.execute(statement)
    except Exception:
        # Если не пишется даже журнал ошибок — остаётся только лог контейнера.
        log.exception("не удалось записать ошибку в журнал")


async def recent_errors(session: AsyncSession, limit: int = 15) -> list[ErrorLog]:
    rows = await session.execute(
        select(ErrorLog).order_by(desc(ErrorLog.occurred_at)).limit(limit)
    )
    return list(rows.scalars().all())


async def purge_old_errors(session: AsyncSession, days: int = 30) -> int:
    """Журнал ошибок не растёт вечно: это эксплуатационные данные, не юридические."""
    from sqlalchemy import delete

    result = await session.execute(
        delete(ErrorLog).where(ErrorLog.occurred_at < utcnow() - timedelta(days=days))
    )
    return result.rowcount or 0


# ── Сбор состояния ──────────────────────────────────────────────────────────
@dataclass(slots=True)
class Status:
    healthy: bool = True
    checks: dict[str, dict] = field(default_factory=dict)
    services: dict[str, dict] = field(default_factory=dict)
    numbers: dict[str, int] = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def fail(self, problem: str) -> None:
        self.healthy = False
        self.problems.append(problem)


async def collect(session: AsyncSession | None = None) -> Status:
    """Полная картина состояния. Используется и страницей, и монитором."""
    status = Status()

    # Redis проверяется первым: от него зависят отметки служб.
    try:
        await redis.ping()
        status.checks["redis"] = {"ok": True, "text": "отвечает"}
    except Exception as error:
        status.checks["redis"] = {"ok": False, "text": f"недоступен ({type(error).__name__})"}
        status.fail("Redis недоступен")

    try:
        async with session_scope() as fresh:
            await fresh.execute(select(func.count(User.id)))
            status.checks["database"] = {"ok": True, "text": "отвечает"}

            status.numbers["people"] = await fresh.scalar(
                select(func.count(User.id)).where(User.status == UserStatus.ACTIVE)
            ) or 0
            status.numbers["pending_people"] = await fresh.scalar(
                select(func.count(User.id)).where(User.status == UserStatus.PENDING)
            ) or 0
            status.numbers["tasks_active"] = await fresh.scalar(
                select(func.count(Task.id)).where(
                    Task.status.notin_([TaskStatus.DONE, TaskStatus.CANCELLED])
                )
            ) or 0
            status.numbers["tasks_overdue"] = await fresh.scalar(
                select(func.count(Task.id)).where(Task.status == TaskStatus.OVERDUE)
            ) or 0

            pending = await fresh.scalar(
                select(func.count(Notification.id)).where(
                    Notification.status == NotificationStatus.PENDING,
                    Notification.scheduled_at <= utcnow(),
                )
            ) or 0
            oldest = await fresh.scalar(
                select(func.min(Notification.scheduled_at)).where(
                    Notification.status == NotificationStatus.PENDING,
                    Notification.scheduled_at <= utcnow(),
                )
            )
            failed = await fresh.scalar(
                select(func.count(Notification.id)).where(
                    Notification.status == NotificationStatus.FAILED
                )
            ) or 0

            status.numbers["queue_pending"] = pending
            status.numbers["queue_failed"] = failed
            lag = int((utcnow() - oldest).total_seconds()) if oldest else 0
            status.numbers["queue_lag_seconds"] = lag
            if lag > QUEUE_LAG_LIMIT:
                status.fail(f"Очередь уведомлений отстаёт на {lag} с")

            hour_ago = utcnow() - timedelta(hours=1)
            errors_hour = await fresh.scalar(
                select(func.count(ErrorLog.id)).where(ErrorLog.occurred_at >= hour_ago)
            ) or 0
            status.numbers["errors_hour"] = errors_hour
            status.numbers["errors_day"] = await fresh.scalar(
                select(func.count(ErrorLog.id)).where(
                    ErrorLog.occurred_at >= utcnow() - timedelta(days=1)
                )
            ) or 0
            if errors_hour >= ERRORS_ALERT:
                status.fail(f"Ошибок за час: {errors_hour}")

            for item in await recent_errors(fresh):
                status.errors.append(
                    {
                        "occurred_at": item.occurred_at,
                        "source": item.source,
                        "kind": item.kind,
                        "message": item.message,
                        "context": item.context,
                        "telegram_user_id": item.telegram_user_id,
                    }
                )
    except Exception as error:
        status.checks["database"] = {"ok": False, "text": f"недоступна ({type(error).__name__})"}
        status.fail("База данных недоступна")

    now = utcnow()
    for name, title in SERVICES.items():
        seen = await last_beat(name)
        if seen is None:
            status.services[name] = {"ok": False, "title": title, "text": "молчит"}
            status.fail(f"{title}: нет отметки о работе")
        else:
            silence = int((now - seen).total_seconds())
            status.services[name] = {
                "ok": True,
                "title": title,
                "text": f"отметка {silence} с назад",
                "seconds": silence,
            }

    return status
