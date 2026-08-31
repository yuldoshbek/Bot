"""Redis: кэш, блокировки планировщика, удержание слотов, лимиты частоты."""
from redis.asyncio import Redis

from app.core.config import settings

redis: Redis = Redis.from_url(settings.redis_url, decode_responses=True)


async def acquire_lock(key: str, ttl_seconds: int = 60) -> bool:
    """Распределённая блокировка: гарантирует, что напоминание уйдёт один раз."""
    return bool(await redis.set(f"lock:{key}", "1", nx=True, ex=ttl_seconds))


async def release_lock(key: str) -> None:
    await redis.delete(f"lock:{key}")
