"""Конфигурация приложения. Единственный источник настроек — переменные окружения."""
from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    bot_token: str = ""
    bot_mode: str = "polling"            # polling | webhook
    webhook_base_url: str = ""
    webhook_secret: str = "change-me"

    # Первый администратор — получает роль ADMIN при первом /start
    bootstrap_admin_telegram_id: int = 0

    # Организация
    org_name: str = "Организация"
    default_timezone: str = "Asia/Tashkent"
    default_locale: str = "ru"

    # Хранилища
    database_url: str = "postgresql+asyncpg://seta:seta@localhost:5432/seta"
    redis_url: str = "redis://localhost:6379/0"

    # ИИ (блок 6)
    openai_api_key: str = ""
    ai_daily_budget_usd: float = 1.0

    # Рабочие правила по умолчанию (настраиваются в админке на отдел/человека)
    work_start: str = "09:00"
    work_end: str = "19:00"
    lunch_start: str = "13:00"
    lunch_end: str = "14:00"
    late_end: str = "22:00"              # предел для поздних встреч, когда руководитель их открыл
    buffer_minutes: int = 15
    quiet_hours_start: str = "21:00"
    quiet_hours_end: str = "07:30"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.default_timezone)

    @property
    def webhook_path(self) -> str:
        return "/telegram/webhook"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
