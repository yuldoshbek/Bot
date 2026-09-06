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
    # Основной язык — узбекский латиницей. Русский остаётся, но дополнительным:
    # человек переключает его сам в профиле.
    default_locale: str = "uz"

    # Хранилища
    database_url: str = "postgresql+asyncpg://seta:seta@localhost:5432/seta"
    redis_url: str = "redis://localhost:6379/0"

    # ИИ (блок 6). Выключен по умолчанию: система обязана работать целиком
    # и без него, а включение — осознанное действие с ключом и бюджетом.
    ai_enabled: bool = False
    openai_api_key: str = ""
    # Потолки расхода. При превышении ИИ отключается, администратор получает
    # уведомление, система продолжает работать полностью.
    ai_daily_budget_usd: float = 1.0
    ai_monthly_budget_usd: float = 20.0
    # Рутина — младшая модель, недельный отчёт — старшая, речь — whisper.
    ai_model_routine: str = "gpt-4o-mini"
    ai_model_report: str = "gpt-4o"
    ai_model_voice: str = "whisper-1"
    # Своя служба расшифровки. Задан адрес — речь слушает она, и это
    # не стоит ничего: потолок расхода такую расшифровку не касается.
    # Пусто — речь слушает платная модель, и только при AI_ENABLED=true.
    stt_url: str = ""

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
