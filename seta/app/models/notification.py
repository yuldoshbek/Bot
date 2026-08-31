"""Очередь уведомлений.

Ключевое поле - event_key. Оно уникально, поэтому повторная обработка события
не создаёт второе сообщение: защита от дублей встроена в саму таблицу,
а не в аккуратность вызывающего кода.
"""
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PKMixin
from app.models.enums import NotificationPriority, NotificationStatus


class Notification(Base, PKMixin):
    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("event_key", name="uq_notifications_event_key"),
        Index("ix_notifications_delivery", "status", "scheduled_at"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_key: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    priority: Mapped[str] = mapped_column(
        String(12), nullable=False, default=NotificationPriority.NORMAL
    )

    body: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    status: Mapped[str] = mapped_column(
        String(12), nullable=False, default=NotificationStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
