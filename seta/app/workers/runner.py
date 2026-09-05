"""Фоновый обработчик: доставка уведомлений и контроль сроков.

Отдельный процесс, а не поток внутри бота: бот должен отвечать Telegram за
секунды и не зависеть от того, сколько сейчас рассылается сообщений.

Пять циклов с разной частотой:
  доставка  - каждые 3 секунды (цель по задержке очереди: не больше 5 секунд);
  сроки     - раз в минуту;
  встречи   - раз в минуту: удержания, досье за полчаса, отметка за пять минут;
  сводки    - раз в минуту: утренняя сводка в 07:30 по месту получателя;
  документы - раз в полминуты: достаёт текст из загруженных файлов.

Все защищены распределённой блокировкой в Redis: даже если запустить второй
обработчик, одно напоминание уйдёт один раз.
"""
import asyncio
import logging

from app.core.db import engine, session_scope
from app.core.redis import acquire_lock, redis, release_lock
from app.services import attendance, briefing, deadlines, digest, indexer, meetings
from app.services.health import beat, record_error
from app.services.notifications import deliver_pending

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("seta.worker")

DELIVERY_INTERVAL = 3
DEADLINE_INTERVAL = 60
MEETING_INTERVAL = 60
DIGEST_INTERVAL = 60
INDEX_INTERVAL = 30


async def delivery_loop() -> None:
    from app.bot.loader import bot

    async def send(telegram_id: int, text: str) -> None:
        await bot.send_message(telegram_id, text, disable_web_page_preview=True)

    while True:
        try:
            await beat("worker:delivery")
            if await acquire_lock("notifications:deliver", ttl_seconds=DELIVERY_INTERVAL * 3):
                try:
                    async with session_scope() as session:
                        sent = await deliver_pending(session, send)
                    if sent:
                        log.info("доставлено уведомлений: %s", sent)
                finally:
                    await release_lock("notifications:deliver")
        except Exception as error:
            log.exception("сбой доставки уведомлений")
            await record_error(error, source="worker", context="доставка уведомлений")
        await asyncio.sleep(DELIVERY_INTERVAL)


async def deadline_loop() -> None:
    while True:
        try:
            await beat("worker:deadlines")
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
        except Exception as error:
            log.exception("сбой проверки сроков")
            await record_error(error, source="worker", context="проверка сроков")
        await asyncio.sleep(DEADLINE_INTERVAL)


async def meeting_loop() -> None:
    """Ежеминутный уход за встречами: освобождение окон и приглашение отметиться.

    Оба дела опираются на время, а не на действие человека. Опираться на то,
    что кто-то откроет календарь и «заодно» подчистит, нельзя: окно должно
    вернуться в оборот, даже если в системе неделю никого нет.
    """
    while True:
        try:
            await beat("worker:meetings")
            if await acquire_lock("meetings:upkeep", ttl_seconds=MEETING_INTERVAL * 2):
                try:
                    async with session_scope() as session:
                        released = await meetings.expire_holds(session)
                        briefed = await briefing.send_briefings(session)
                        called = await attendance.open_checkins(session)
                    if released or briefed or called:
                        log.info(
                            "окон освобождено %s, досье %s, позвано отметиться %s",
                            released, briefed, called,
                        )
                finally:
                    await release_lock("meetings:upkeep")
        except Exception as error:
            log.exception("сбой ухода за встречами")
            await record_error(error, source="worker", context="уход за встречами")
        await asyncio.sleep(MEETING_INTERVAL)


async def digest_loop() -> None:
    """Утренняя сводка. Проход каждую минуту, отправка — по местному времени.

    Час сервера здесь ни при чём: 07:30 наступает у каждого получателя своё,
    и проход обязан смотреть на его пояс. Повторные проходы внутри окна
    безопасны — ключ события содержит местную дату, и вторая сводка
    отбрасывается уникальностью в схеме.
    """
    while True:
        try:
            await beat("worker:digest")
            if await acquire_lock("digest:morning", ttl_seconds=DIGEST_INTERVAL * 2):
                try:
                    async with session_scope() as session:
                        sent = await digest.send_digests(session)
                    if sent:
                        log.info("утренних сводок поставлено: %s", sent)
                finally:
                    await release_lock("digest:morning")
        except Exception as error:
            log.exception("сбой утренней сводки")
            await record_error(error, source="worker", context="утренняя сводка")
        await asyncio.sleep(DIGEST_INTERVAL)


async def index_loop() -> None:
    """Извлечение текста из документов.

    Файл скачивается у Telegram, поэтому цикл берёт бота — но только чтобы
    получить байты. Ни одного сообщения отсюда не уходит.
    """
    from app.bot.loader import bot

    async def download(file_id: str) -> bytes:
        info = await bot.get_file(file_id)
        buffer = await bot.download_file(info.file_path)
        return buffer.read()

    while True:
        try:
            await beat("worker:documents")
            if await acquire_lock("documents:index", ttl_seconds=INDEX_INTERVAL * 4):
                try:
                    async with session_scope() as session:
                        stats = await indexer.index_pending(session, download)
                    if any(stats.values()):
                        log.info(
                            "документы: разобрано %s, без текста %s, сбоев %s",
                            stats["done"], stats["empty"], stats["failed"],
                        )
                finally:
                    await release_lock("documents:index")
        except Exception as error:
            log.exception("сбой разбора документов")
            await record_error(error, source="worker", context="разбор документов")
        await asyncio.sleep(INDEX_INTERVAL)


async def main() -> None:
    log.info("Фоновый обработчик запущен")
    try:
        await asyncio.gather(
            delivery_loop(), deadline_loop(), meeting_loop(),
            digest_loop(), index_loop(),
        )
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
