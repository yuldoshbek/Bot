"""Экземпляры бота и диспетчера."""
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage

from app.core.config import settings
from app.core.redis import redis

bot = Bot(
    token=settings.bot_token,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

storage = RedisStorage(redis=redis)
dp = Dispatcher(storage=storage)
