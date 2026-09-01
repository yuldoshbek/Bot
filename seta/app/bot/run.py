"""Запуск бота.

Локально работает через long polling - сервер и домен не нужны.
На боевом сервере достаточно переключить BOT_MODE=webhook.
"""
import asyncio
import logging

from aiogram.types import ErrorEvent

from app.bot.handlers import (
    admin,
    availability,
    documents,
    meetings,
    menu,
    registry,
    start,
    tasks,
)
from app.bot.loader import bot, dp
from app.bot.middlewares.auth import AuthMiddleware
from app.core.config import settings
from app.core.db import session_scope
from app.services.bootstrap import bootstrap
from app.services.health import HEARTBEAT_INTERVAL, beat, record_error

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("seta.bot")


async def on_error(event: ErrorEvent) -> bool:
    """Последний рубеж: неожиданная ошибка не должна оставлять человека без ответа.

    Обработчики защищены сами, но пользователь не обязан страдать от того,
    чего мы не предусмотрели: он получает понятное сообщение, а мы — запись в логе.
    """
    log.exception("необработанная ошибка", exc_info=event.exception)

    # Ошибка попадает и в журнал: логи контейнера видит тот, кто умеет их читать,
    # а владельцу системы она нужна на странице состояния.
    update = event.update
    context = None
    telegram_user_id = None
    if update.message is not None:
        context = update.message.text
        telegram_user_id = update.message.from_user.id if update.message.from_user else None
    elif update.callback_query is not None:
        context = update.callback_query.data
        telegram_user_id = update.callback_query.from_user.id
    await record_error(
        event.exception, source="bot", context=context, telegram_user_id=telegram_user_id
    )

    text = "Что-то пошло не так. Попробуйте ещё раз или нажмите /start."
    try:
        if update.callback_query is not None:
            await update.callback_query.answer(text, show_alert=True)
        elif update.message is not None:
            await update.message.answer(text)
    except Exception:
        pass
    return True


def setup() -> None:
    dp.message.middleware(AuthMiddleware())
    dp.callback_query.middleware(AuthMiddleware())

    dp.include_router(start.router)
    dp.include_router(availability.router)
    dp.include_router(admin.router)
    dp.include_router(tasks.router)
    dp.include_router(meetings.router)
    dp.include_router(registry.router)
    dp.include_router(documents.router)
    dp.include_router(menu.router)

    dp.errors.register(on_error)


async def heartbeat_loop() -> None:
    """Отметка «бот жив» для страницы состояния.

    Без неё «контейнер запущен» и «бот работает» — разные вещи: процесс может
    висеть, а снаружи выглядеть здоровым.
    """
    while True:
        await beat("bot")
        await asyncio.sleep(HEARTBEAT_INTERVAL)


async def main() -> None:
    setup()

    async with session_scope() as session:
        organization = await bootstrap(session)
        log.info("Организация: %s (id=%s)", organization.name, organization.id)

    me = await bot.get_me()
    log.info("Бот запущен: @%s", me.username)

    if settings.bot_mode == "webhook":
        # Апдейты принимает API. Процесс не завершается: иначе restart-политика
        # поднимала бы контейнер по кругу, и в логах вместо работы был бы поток
        # перезапусков, в котором не видно настоящих ошибок.
        log.info("Режим webhook: апдейты принимает API на %s", settings.webhook_path)
        await heartbeat_loop()

    await bot.delete_webhook(drop_pending_updates=False)
    asyncio.create_task(heartbeat_loop())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
