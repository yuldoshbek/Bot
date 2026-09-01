"""Уведомления: постановка в очередь и доставка.

Четыре правила, которые не дают завалить человека сообщениями:

1. Без дублей - event_key уникален, повторная обработка события ничего не создаёт.
2. Тихие часы - обычное уведомление ждёт утра, критичное уходит сразу.
3. Группировка - несколько накопившихся сообщений уходят одним.
4. Приоритеты - критичное всегда отдельным сообщением.
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import case, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import in_quiet_hours, next_quiet_hours_end, utcnow
from app.models.enums import NotificationPriority, NotificationStatus
from app.models.notification import Notification
from app.models.user import User

log = logging.getLogger("seta.notifications")

# Попыток много, но они разнесены по времени: короткая недоступность Telegram
# больше не сжигает очередь за девять секунд.
MAX_ATTEMPTS = 8
GROUP_THRESHOLD = 3      # начиная со скольких сообщений они объединяются в одно
GROUP_MAX_ITEMS = 15     # больше в одно сообщение не собираем
GROUP_MAX_CHARS = 3500   # предел Telegram 4096, оставляем запас на заголовок

# Ошибки, которые повтором не лечатся: человек заблокировал бота или удалил чат.
PERMANENT_ERRORS = (
    "bot was blocked",
    "user is deactivated",
    "chat not found",
    "bot can't initiate conversation",
)


def _backoff(attempts: int) -> timedelta:
    """Пауза перед следующей попыткой: минута, две, четыре... до часа."""
    return timedelta(seconds=min(60 * (2 ** attempts), 3600))


def _is_permanent(error: str) -> bool:
    lowered = error.lower()
    return any(marker in lowered for marker in PERMANENT_ERRORS)


def _priority_rank():
    """Порядок доставки: критичное первым.

    Столбец priority строковый, и сортировка по нему давала обратный порядок
    (NORMAL, LOW, CRITICAL по алфавиту) — критичное уходило последним.
    """
    return case(
        {
            NotificationPriority.CRITICAL: 0,
            NotificationPriority.NORMAL: 1,
            NotificationPriority.LOW: 2,
        },
        value=Notification.priority,
        else_=3,
    )


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
    organization_id: int | None = None,
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
            organization_id=organization_id,
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


async def pending_for_delivery(
    session: AsyncSession,
    limit: int = 200,
    organization_id: int | None = None,
) -> list[Notification]:
    """Готовые к отправке уведомления.

    organization_id ограничивает выборку одной организацией. Проверочным
    скриптам он обязателен: иначе прогон разберёт боевую очередь и пометит
    настоящие уведомления доставленными, хотя никто их не получит.
    """
    query = (
        select(Notification)
        .where(
            Notification.status == NotificationStatus.PENDING,
            Notification.scheduled_at <= utcnow(),
            Notification.attempts < MAX_ATTEMPTS,
            # Отложенные после сбоя ждут своей паузы.
            (Notification.next_attempt_at.is_(None))
            | (Notification.next_attempt_at <= utcnow()),
        )
        .order_by(_priority_rank(), Notification.scheduled_at)
        .limit(limit)
    )
    if organization_id is not None:
        query = query.join(User, User.id == Notification.user_id).where(
            User.organization_id == organization_id
        )

    rows = await session.execute(query)
    return list(rows.scalars().all())


async def mark_sent(session: AsyncSession, ids: list[int]) -> None:
    if not ids:
        return
    await session.execute(
        update(Notification)
        .where(Notification.id.in_(ids))
        .values(status=NotificationStatus.SENT, sent_at=utcnow())
    )


async def mark_failed(
    session: AsyncSession, ids: list[int], error: str, retry_after: int | None = None
) -> None:
    """Откладывает повторную попытку.

    retry_after приходит от Telegram при превышении частоты: такую задержку
    попыткой не считаем - виноват не адресат, а темп рассылки.
    """
    if not ids:
        return

    if _is_permanent(error):
        # Повторять бессмысленно: адресат недоступен навсегда.
        await session.execute(
            update(Notification)
            .where(Notification.id.in_(ids))
            .values(status=NotificationStatus.FAILED, error=error[:500])
        )
        return

    if retry_after is not None:
        await session.execute(
            update(Notification)
            .where(Notification.id.in_(ids))
            .values(
                error=error[:500],
                next_attempt_at=utcnow() + timedelta(seconds=retry_after + 1),
            )
        )
        return

    rows = await session.execute(
        select(Notification.id, Notification.attempts).where(Notification.id.in_(ids))
    )
    for notification_id, attempts in rows.all():
        nxt = attempts + 1
        await session.execute(
            update(Notification)
            .where(Notification.id == notification_id)
            .values(
                attempts=nxt,
                error=error[:500],
                next_attempt_at=utcnow() + _backoff(nxt),
                status=(
                    NotificationStatus.FAILED
                    if nxt >= MAX_ATTEMPTS
                    else NotificationStatus.PENDING
                ),
            )
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

    # Пачка режется по количеству и по длине: одно сообщение на сорок уведомлений
    # превысило бы предел Telegram и потерялось бы целиком вместе со всей группой.
    for start in range(0, len(ordinary), GROUP_MAX_ITEMS):
        chunk = ordinary[start : start + GROUP_MAX_ITEMS]
        lines = [f"📋 <b>Обновления по вашим поручениям: {len(chunk)}</b>", ""]
        ids: list[int] = []
        length = len(lines[0])
        for item in chunk:
            body = item.body.strip().splitlines()
            first_line = body[0] if body else "обновление"
            if length + len(first_line) > GROUP_MAX_CHARS and ids:
                result.append((ids, "\n".join(lines)))
                lines = [f"📋 <b>Обновления по вашим поручениям</b>", ""]
                ids, length = [], len(lines[0])
            lines.append(f"• {first_line}")
            ids.append(item.id)
            length += len(first_line) + 3
        if ids:
            result.append((ids, "\n".join(lines)))
    return result


async def deliver_pending(
    session: AsyncSession, send, organization_id: int | None = None
) -> int:
    """Отправляет готовые уведомления. send(telegram_id, text) -> None.

    Возвращает количество доставленных сообщений.
    """
    items = await pending_for_delivery(session, organization_id=organization_id)
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
                retry_after = getattr(error, "retry_after", None)
                log.warning("не доставлено пользователю %s: %s", user_id, error)
                await mark_failed(session, ids, str(error), retry_after=retry_after)

    return delivered
