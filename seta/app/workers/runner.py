"""Фоновый обработчик: доставка уведомлений и контроль сроков.

Отдельный процесс, а не поток внутри бота: бот должен отвечать Telegram за
секунды и не зависеть от того, сколько сейчас рассылается сообщений.

Два цикла с разной частотой:
  доставка  - каждые 3 секунды (цель по задержке очереди: не больше 5 секунд);
  сроки     - раз в минуту.

Оба защищены распределённой блокировкой в Redis: даже если запустить второй
обработчик, одно напоминание уйдёт один раз.
"""
import asyncio
import logging

from app.core.db import engine, session_scope
from app.core.redis import acquire_lock, redis, release_lock
from app.services import deadlines
from app.services.notifications import deliver_pending

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("seta.worker")

DELIVERY_INTERVAL = 3
DEADLINE_INTERVAL = 60


async def delivery_loop() -> None:
    from app.bot.loader import bot

    async def send(telegram_id: int, text: str) -> None:
        await bot.send_message(telegram_id, text, disable_web_page_preview=True)

    while True:
        try:
            if await acquire_lock("notifications:deliver", ttl_seconds=DELIVERY_INTERVAL * 3):
                try:
                    async with session_scope() as session:
                        sent = await deliver_pending(session, send)
                    if sent:
                        log.info("доставлено уведомлений: %s", sent)
                finally:
                    await release_lock("notifications:deliver")
        except Exception:
            log.exception("сбой доставки уведомлений")
        await asyncio.sleep(DELIVERY_INTERVAL)


async def deadline_loop() -> None:
    while True:
        try:
            if await acquire_lock("deadlines:check", ttl_seconds=DEADLINE_INTERVAL * 2):
                try:
                    async with session_scope() as session:
                        stats = await deadlines.process(session)
                    if any(stats.values()):
                        log.info(
                            "сроки: напоминаний %s, просрочено %s, эскалаций %s",
                            stats["reminded"], stats["overdue"], stats["escalated"],
                        )
                finally:
                    await release_lock("deadlines:check")
        except Exception:
            log.exception("сбой проверки сроков")
        await asyncio.sleep(DEADLINE_INTERVAL)


async def main() -> None:
    log.info("Фоновый обработчик запущен")
    try:
        await asyncio.gather(delivery_loop(), deadline_loop())
    finally:
        from app.bot.loader import bot

        await bot.session.close()
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
