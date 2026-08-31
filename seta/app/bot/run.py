"""Запуск бота.

Локально работает через long polling - сервер и домен не нужны.
На боевом сервере достаточно переключить BOT_MODE=webhook.
"""
import asyncio
import logging

from app.bot.handlers import admin, availability, menu, start
from app.bot.loader import bot, dp
from app.bot.middlewares.auth import AuthMiddleware
from app.core.config import settings
from app.core.db import session_scope
from app.services.bootstrap import bootstrap

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("seta.bot")


def setup() -> None:
    dp.message.middleware(AuthMiddleware())
    dp.callback_query.middleware(AuthMiddleware())

    dp.include_router(start.router)
    dp.include_router(availability.router)
    dp.include_router(admin.router)
    dp.include_router(menu.router)


async def main() -> None:
    setup()

    async with session_scope() as session:
        organization = await bootstrap(session)
        log.info("Организация: %s (id=%s)", organization.name, organization.id)

    me = await bot.get_me()
    log.info("Бот запущен: @%s", me.username)

    if settings.bot_mode == "webhook":
        log.info("Режим webhook: апдейты принимает API на %s", settings.webhook_path)
        return

    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
