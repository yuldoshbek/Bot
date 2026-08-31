"""HTTP-слой: приём вебхука Telegram и проверки состояния системы.

Вебхук обязан отвечать за секунды, поэтому здесь не выполняется ничего долгого:
апдейт передаётся диспетчеру, тяжёлое уходит в фоновые обработчики.
"""
import logging
from contextlib import asynccontextmanager

from aiogram.types import Update
from fastapi import APIRouter, FastAPI, Header, HTTPException, Request

from app.bot.loader import bot, dp
from app.bot.run import setup as setup_bot
from app.core.config import settings
from app.core.db import engine, session_scope
from app.core.redis import redis
from app.services.bootstrap import bootstrap

log = logging.getLogger("seta.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_bot()
    async with session_scope() as session:
        await bootstrap(session)

    if settings.bot_mode == "webhook" and settings.webhook_base_url:
        url = settings.webhook_base_url.rstrip("/") + settings.webhook_path
        await bot.set_webhook(
            url=url,
            secret_token=settings.webhook_secret,
            drop_pending_updates=False,
        )
        log.info("Вебхук установлен: %s", url)

    yield

    await bot.session.close()
    await redis.aclose()
    await engine.dispose()


app = FastAPI(title="SETA API", version="1.0", lifespan=lifespan)
api_v1 = APIRouter(prefix="/api/v1")


@app.get("/health")
async def health() -> dict[str, str]:
    """Состояние зависимостей. Используется мониторингом и проверкой контейнера."""
    status = {"api": "ok"}
    try:
        async with session_scope() as session:
            await session.execute(__import__("sqlalchemy").text("SELECT 1"))
        status["database"] = "ok"
    except Exception as error:
        status["database"] = f"error: {error.__class__.__name__}"
    try:
        await redis.ping()
        status["redis"] = "ok"
    except Exception as error:
        status["redis"] = f"error: {error.__class__.__name__}"
    return status


@app.post(settings.webhook_path)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    # Секретный токен подтверждает, что запрос действительно от Telegram.
    if x_telegram_bot_api_secret_token != settings.webhook_secret:
        raise HTTPException(status_code=403, detail="invalid secret token")

    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}


app.include_router(api_v1)
