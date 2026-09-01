"""HTTP-слой: приём вебхука Telegram и проверки состояния системы.

Вебхук обязан отвечать за секунды, поэтому здесь не выполняется ничего долгого:
апдейт передаётся диспетчеру, тяжёлое уходит в фоновые обработчики.
"""
import logging
from contextlib import asynccontextmanager

from aiogram.types import Update
from fastapi import APIRouter, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from app.bot.loader import bot, dp
from app.bot.run import setup as setup_bot
from app.core.config import settings
from app.core.db import engine, session_scope
from app.core.redis import redis
from app.api.health_page import render
from app.core.timeutil import utcnow
from app.services.bootstrap import bootstrap
from app.services.health import collect

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
async def health(request: Request, response: Response):
    """Состояние системы. Один адрес для человека и для монитора.

    Браузер просит HTML — получает страницу с показателями и последними ошибками.
    Монитор просит JSON — получает данные. Код 503 в обоих случаях, если
    что-то не работает: внешняя проверка увидит сбой раньше сотрудников.
    """
    status = await collect()

    if not status.healthy:
        response.status_code = 503

    wants_html = "text/html" in (request.headers.get("accept") or "")
    if wants_html:
        return HTMLResponse(
            render(status, utcnow()),
            status_code=response.status_code or 200,
        )

    return {
        "healthy": status.healthy,
        "problems": status.problems,
        "checks": {name: info["ok"] for name, info in status.checks.items()},
        "services": {
            name: {"ok": info["ok"], "silence_seconds": info.get("seconds")}
            for name, info in status.services.items()
        },
        "numbers": status.numbers,
        "errors_recent": [
            {
                "at": item["occurred_at"].isoformat(),
                "source": item["source"],
                "kind": item["kind"],
                "message": item["message"][:200],
            }
            for item in status.errors[:5]
        ],
    }


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
