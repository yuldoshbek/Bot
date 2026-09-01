"""Журнал ошибок системы.

Отдельная таблица, а не только логи контейнера: логи видит тот, кто умеет
читать `docker compose logs`, а владелец системы — не программист. Ошибки должны
быть видны на странице состояния, с текстом, временем и тем, что человек делал
в этот момент.

От audit_log отличается назначением: там «кто что сделал» — юридическая запись,
здесь «что сломалось» — эксплуатационная. Записи отсюда можно чистить, оттуда нельзя.
"""
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PKMixin


class ErrorLog(Base, PKMixin):
    __tablename__ = "error_log"
    __table_args__ = (
        Index("ix_error_log_recent", "occurred_at"),
        # Одинаковые ошибки схлопываются по отпечатку: сто одинаковых падений
        # должны читаться как «сто раз одно и то же», а не как сто разных строк.
        Index("ix_error_log_fingerprint", "fingerprint"),
    )

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="bot")
    kind: Mapped[str] = mapped_column(String(120), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Что человек делал в момент ошибки: текст кнопки или команда.
    context: Mapped[str | None] = mapped_column(String(255), nullable=True)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    seen_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
