"""HTTP-слой: приём вебхука Telegram и проверки состояния системы.

Вебхук обязан отвечать за секунды, поэтому здесь не выполняется ничего долгого:
апдейт передаётся диспетчеру, тяжёлое уходит в фоновые обработчики.
"""
import logging
from contextlib import asynccontextmanager

from aiogram.types import Update
from fastapi import APIRouter, FastAPI, Header, HTTPException, Request, Response
from sqlalchemy import func, select, text

from app.bot.loader import bot, dp
from app.bot.run import setup as setup_bot
from app.core.config import settings
from app.core.db import engine, session_scope
from app.core.redis import redis
from app.core.timeutil import utcnow
from app.models.enums import NotificationStatus
from app.models.notification import Notification
from app.services.bootstrap import bootstrap

# Целевой показатель по отставанию очереди — 5 секунд; тревога при заметном превышении.
QUEUE_LAG_LIMIT = 120

log = logging.getLogger("seta.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_bot()
    async with session_scope() as session:
        await bootstrap(session)

    if settings.bot_mode == "webhook" and settings.webhook_secret in ("", "change-me"):
        raise RuntimeError(
            "BOT_MODE=webhook с секретом по умолчанию: заполните WEBHOOK_SECRET в .env, "
            "иначе эндпоинт примет поддельные обновления от кого угодно."
        )

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
async def health(response: Response) -> dict[str, object]:
    """Состояние системы для внешнего мониторинга.

    Отвечает 503, если хоть одна зависимость недоступна или очередь встала.
    Раньше здесь всегда возвращалось 200, и любой монитор считал лежащую базу
    исправной работой - о сбое владелец узнавал от сотрудников.
    """
    status: dict[str, object] = {"api": "ok"}
    healthy = True

    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
            # Глубина очереди — второй важный признак: службы живы,
            # а уведомления не уходят.
            pending = await session.scalar(
                select(func.count(Notification.id)).where(
                    Notification.status == NotificationStatus.PENDING,
                    Notification.scheduled_at <= utcnow(),
                )
            )
            oldest = await session.scalar(
                select(func.min(Notification.scheduled_at)).where(
                    Notification.status == NotificationStatus.PENDING,
                    Notification.scheduled_at <= utcnow(),
                )
            )
        status["database"] = "ok"
        status["queue_pending"] = pending or 0
        lag = int((utcnow() - oldest).total_seconds()) if oldest else 0
        status["queue_lag_seconds"] = lag
        if lag > QUEUE_LAG_LIMIT:
            status["queue"] = f"отставание {lag} с — обработчик не разбирает очередь"
            healthy = False
    except Exception as error:
        status["database"] = f"error: {error.__class__.__name__}"
        healthy = False

    try:
        await redis.ping()
        status["redis"] = "ok"
    except Exception as error:
        status["redis"] = f"error: {error.__class__.__name__}"
        healthy = False

    if not healthy:
        response.status_code = 503
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
