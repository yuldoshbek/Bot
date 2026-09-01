"""Журнал аудита. Только добавление: изменение и удаление записей запрещены."""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PKMixin


class AuditLog(Base, PKMixin):
    __tablename__ = "audit_log"

    # Журнал читают выборкой по времени, без привязки к человеку, поэтому
    # организация хранится здесь: без неё администратор одной организации
    # увидел бы действия другой.
    organization_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    actor_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Действие по делегированию: actor_id - настоящий автор,
    # on_behalf_of_id - тот, от чьего имени оно совершено.
    on_behalf_of_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    entity_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)

    before_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="bot")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
