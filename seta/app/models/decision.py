"""Повестка встречи и реестр решений.

Решение живёт дольше встречи, на которой принято. Через полгода вопрос «а что мы
тогда решили по складу» задаётся без привязки к дате совещания, поэтому решение —
самостоятельная запись со своим сроком и ответственным, а связь со встречей
необязательна.

Удалить решение нельзя, можно отменить. Реестр, из которого пропадают строки,
перестаёт быть реестром.
"""
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PKMixin, TimestampMixin
from app.models.enums import DecisionStatus


class AgendaItem(Base, PKMixin, TimestampMixin):
    # Пункт повестки. Порядок задаётся полем, а не датой создания:
    # пункты переставляют, и «как добавили» быстро расходится с «как обсуждаем».
    __tablename__ = "agenda_items"
    __table_args__ = (
        Index("ix_agenda_meeting_position", "meeting_id", "position"),
    )

    meeting_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    covered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    covered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_by: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )


class Decision(Base, PKMixin, TimestampMixin):
    # Реестр решений: кто, когда, по какому вопросу.
    __tablename__ = "decisions"
    __table_args__ = (
        Index("ix_decisions_org_created", "organization_id", "created_at"),
        Index("ix_decisions_meeting", "meeting_id"),
        # Поиск по формулировке с русской морфологией. Выражение неизменяемо,
        # поэтому индекс строится прямо по нему — отдельная таблица индекса
        # потребовала бы синхронизации, а расходящийся индекс молча теряет строки.
        Index(
            "ix_decisions_search",
            text("to_tsvector('russian', title || ' ' || coalesce(details, ''))"),
            postgresql_using="gin",
        ),
    )

    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Встреча необязательна: решение бывает принято и вне совещания.
    meeting_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("meetings.id", ondelete="SET NULL"), nullable=True
    )
    agenda_item_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agenda_items.id", ondelete="SET NULL"), nullable=True
    )

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)

    author_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    responsible_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[str] = mapped_column(String(12), nullable=False, default=DecisionStatus.OPEN)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Решение не удаляют, а отменяют — с причиной, которая остаётся в реестре.
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
