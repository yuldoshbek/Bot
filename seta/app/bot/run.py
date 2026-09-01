"""Запуск бота.

Локально работает через long polling - сервер и домен не нужны.
На боевом сервере достаточно переключить BOT_MODE=webhook.
"""
import asyncio
import logging

from aiogram.types import ErrorEvent

from app.bot.handlers import admin, availability, menu, start, tasks
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


async def on_error(event: ErrorEvent) -> bool:
    """Последний рубеж: неожиданная ошибка не должна оставлять человека без ответа.

    Обработчики защищены сами, но пользователь не обязан страдать от того,
    чего мы не предусмотрели: он получает понятное сообщение, а мы — запись в логе.
    """
    log.exception("необработанная ошибка", exc_info=event.exception)

    text = "Что-то пошло не так. Попробуйте ещё раз или нажмите /start."
    update = event.update
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
    dp.include_router(menu.router)

    dp.errors.register(on_error)


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
        while True:
            await asyncio.sleep(3600)

    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
