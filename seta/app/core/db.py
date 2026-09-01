"""Подключение к PostgreSQL. Одна асинхронная сессия на запрос или на апдейт бота."""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

# Пул небольшой намеренно: соединение держится всё время обработки апдейта,
# включая обращения к Telegram. При замедлении Telegram большой пул не спасает,
# а исчерпывает лимит соединений базы. Короткий таймаут честнее долгого зависания.
engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=8,
    max_overflow=8,
    pool_timeout=10,
    pool_recycle=1800,
    echo=False,
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Транзакция целиком: коммит при успехе, откат при любой ошибке."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session
