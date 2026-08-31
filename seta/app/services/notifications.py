"""Уведомления: постановка в очередь и доставка.

Четыре правила, которые не дают завалить человека сообщениями:

1. Без дублей - event_key уникален, повторная обработка события ничего не создаёт.
2. Тихие часы - обычное уведомление ждёт утра, критичное уходит сразу.
3. Группировка - несколько накопившихся сообщений уходят одним.
4. Приоритеты - критичное всегда отдельным сообщением.
"""
import logging
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import in_quiet_hours, next_quiet_hours_end, utcnow
from app.models.enums import NotificationPriority, NotificationStatus
from app.models.notification import Notification
from app.models.user import User

log = logging.getLogger("seta.notifications")

MAX_ATTEMPTS = 3
GROUP_THRESHOLD = 3  # начиная со скольких сообщений они объединяются в одно


async def enqueue(
    session: AsyncSession,
    *,
    user_id: int,
    event_key: str,
    kind: str,
    body: str,
    priority: NotificationPriority = NotificationPriority.NORMAL,
    payload: dict | None = None,
    scheduled_at: datetime | None = None,
    timezone_name: str | None = None,
) -> bool:
    """Ставит уведомление в очередь. Возвращает False, если оно уже было поставлено.

    Тихие часы сдвигают время доставки, а не отменяют её: человек получит
    сообщение утром, ничего не потеряется.
    """
    when = scheduled_at or utcnow()

    if priority != NotificationPriority.CRITICAL and in_quiet_hours(when, timezone_name):
        when = next_quiet_hours_end(when, timezone_name)

    statement = (
        pg_insert(Notification)
        .values(
            user_id=user_id,
            event_key=event_key,
            kind=kind,
            priority=priority,
            body=body,
            payload=payload,
            status=NotificationStatus.PENDING,
            scheduled_at=when,
            created_at=utcnow(),
        )
        .on_conflict_do_nothing(index_elements=["event_key"])
        .returning(Notification.id)
    )
    created = (await session.execute(statement)).scalar_one_or_none()
    return created is not None


async def pending_for_delivery(session: AsyncSession, limit: int = 200) -> list[Notification]:
    rows = await session.execute(
        select(Notification)
        .where(
            Notification.status == NotificationStatus.PENDING,
            Notification.scheduled_at <= utcnow(),
            Notification.attempts < MAX_ATTEMPTS,
        )
        .order_by(Notification.priority.desc(), Notification.scheduled_at)
        .limit(limit)
    )
    return list(rows.scalars().all())


async def mark_sent(session: AsyncSession, ids: list[int]) -> None:
    if not ids:
        return
    await session.execute(
        update(Notification)
        .where(Notification.id.in_(ids))
        .values(status=NotificationStatus.SENT, sent_at=utcnow())
    )


async def mark_failed(session: AsyncSession, ids: list[int], error: str) -> None:
    if not ids:
        return
    await session.execute(
        update(Notification)
        .where(Notification.id.in_(ids))
        .values(
            attempts=Notification.attempts + 1,
            error=error[:500],
            status=NotificationStatus.PENDING,
        )
    )
    # Исчерпавшие попытки уходят в разбор: администратор видит их в админке.
    await session.execute(
        update(Notification)
        .where(Notification.id.in_(ids), Notification.attempts >= MAX_ATTEMPTS)
        .values(status=NotificationStatus.FAILED)
    )


async def suppress_for_user(session: AsyncSession, ids: list[int]) -> None:
    if not ids:
        return
    await session.execute(
        update(Notification)
        .where(Notification.id.in_(ids))
        .values(status=NotificationStatus.SUPPRESSED)
    )


def group_messages(items: list[Notification]) -> list[tuple[list[int], str]]:
    """Собирает сообщения одного человека в пачки.

    Критичные всегда идут отдельно. Остальные, если их накопилось много,
    объединяются в одно сообщение - вместо пяти уведомлений подряд.
    """
    critical = [n for n in items if n.priority == NotificationPriority.CRITICAL]
    ordinary = [n for n in items if n.priority != NotificationPriority.CRITICAL]

    result: list[tuple[list[int], str]] = [([n.id], n.body) for n in critical]

    if not ordinary:
        return result

    if len(ordinary) < GROUP_THRESHOLD:
        result.extend(([n.id], n.body) for n in ordinary)
        return result

    lines = [f"📋 <b>Обновления по вашим поручениям: {len(ordinary)}</b>", ""]
    for item in ordinary:
        first_line = item.body.strip().splitlines()[0]
        lines.append(f"• {first_line}")
    result.append(([n.id for n in ordinary], "\n".join(lines)))
    return result


async def deliver_pending(session: AsyncSession, send) -> int:
    """Отправляет готовые уведомления. send(telegram_id, text) -> None.

    Возвращает количество доставленных сообщений.
    """
    items = await pending_for_delivery(session)
    if not items:
        return 0

    by_user: dict[int, list[Notification]] = {}
    for item in items:
        by_user.setdefault(item.user_id, []).append(item)

    users = {
        u.id: u
        for u in (
            await session.execute(select(User).where(User.id.in_(by_user.keys())))
        ).scalars().all()
    }

    delivered = 0
    for user_id, user_items in by_user.items():
        user = users.get(user_id)
        if user is None:
            await suppress_for_user(session, [n.id for n in user_items])
            continue
        if not user.notifications_enabled:
            await suppress_for_user(session, [n.id for n in user_items])
            continue

        for ids, text in group_messages(user_items):
            try:
                await send(user.telegram_user_id, text)
                await mark_sent(session, ids)
                delivered += len(ids)
            except Exception as error:  # человек мог заблокировать бота
                log.warning("не доставлено пользователю %s: %s", user_id, error)
                await mark_failed(session, ids, str(error))

    return delivered
