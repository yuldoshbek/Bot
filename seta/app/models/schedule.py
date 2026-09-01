"""Рабочее время, доступность руководителя, блокировки, отсутствия, праздники."""
from datetime import date, datetime, time

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    SmallInteger,
    String,
    Text,
    Time,
)
from sqlalchemy import Index, text
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PKMixin, TimestampMixin
from app.models.enums import AbsenceKind, Availability


class WorkingHours(Base, PKMixin, TimestampMixin):
    # Рабочие часы на день недели: 0 - понедельник, 6 - воскресенье.
    __tablename__ = "working_hours"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    weekday: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    is_working: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    lunch_start: Mapped[time | None] = mapped_column(Time, nullable=True)
    lunch_end: Mapped[time | None] = mapped_column(Time, nullable=True)

    # Поздние встречи разрешены не всегда, а только когда руководитель их открыл.
    allow_late: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    late_end_time: Mapped[time | None] = mapped_column(Time, nullable=True)

    buffer_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=15)
    max_consecutive_meetings: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=3)


class AvailabilityState(Base, PKMixin, TimestampMixin):
    # Индикатор "я на связи": руководитель включает приём одной кнопкой.
    # Работает поверх календаря - пока состояние OPEN, сотрудники видят,
    # что руководитель принимает сейчас, и могут обратиться без заявки.
    # Состояние всегда имеет срок: вечного "доступен" не бывает.
    __tablename__ = "availability_states"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, default=Availability.OFFLINE)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    until_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    changed_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # Виден ли индикатор всем сотрудникам или только ассистенту.
    visible_to_all: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Открывает ли состояние поздние окна вне обычных рабочих часов.
    opens_late_slots: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class AvailabilityLog(Base, PKMixin):
    # История переключений: из неё считается, сколько руководитель реально был доступен.
    __tablename__ = "availability_log"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    changed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class CalendarBlock(Base, PKMixin, TimestampMixin):
    # Личная блокировка времени: "не занимать".
    __tablename__ = "calendar_blocks"
    __table_args__ = (
        Index("ix_calendar_blocks_user_interval", "user_id", "start_at", "end_at"),
        # Пересекающиеся блокировки одного человека невозможны на уровне базы:
        # двойное бронирование отвергается независимо от того, что решил код.
        ExcludeConstraint(
            ("user_id", "="),
            (text("tstzrange(start_at, end_at)"), "&&"),
            name="excl_calendar_blocks_overlap",
            using="gist",
        ),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="Занято")
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class Absence(Base, PKMixin, TimestampMixin):
    # Отпуск, командировка, больничный: влияют и на календарь, и на сроки поручений.
    __tablename__ = "absences"
    __table_args__ = (
        Index("ix_absences_user_interval", "user_id", "start_date", "end_date"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default=AbsenceKind.VACATION)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    substitute_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)


class Holiday(Base, PKMixin, TimestampMixin):
    # Праздничный или перенесённый рабочий день организации.
    __tablename__ = "holidays"
    __table_args__ = (
        Index("ix_holidays_org_day", "organization_id", "day"),
    )

    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    day: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    is_working_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
