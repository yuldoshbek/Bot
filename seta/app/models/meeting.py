"""Встречи: сама встреча, участники, заявки, удержание слотов, явка, оценки.

Два правила вынесены в схему базы, а не оставлены на аккуратность кода:

  * две пересекающиеся встречи одного человека невозможны;
  * два одновременных удержания одного окна невозможны.

Оба реализованы через EXCLUDE USING gist. Это тот же приём, что уже работает
для `event_key` уведомлений: правило, которое нельзя обойти по забывчивости.
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
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PKMixin, TimestampMixin
from app.models.enums import (
    AttendanceSource,
    MeetingStatus,
    MeetingVisibility,
    ParticipantResponse,
    ParticipantRole,
    Priority,
    QuotaPeriod,
    RequestStatus,
)


class Meeting(Base, PKMixin, TimestampMixin):
    __tablename__ = "meetings"
    __table_args__ = (
        Index("ix_meetings_owner_start", "owner_id", "start_at"),
        # Поиск по теме с русской морфологией. Выражение неизменяемо, поэтому
        # индекс строится по нему напрямую, без отдельной таблицы индекса.
        Index(
            "ix_meetings_search",
            text("to_tsvector('russian', title || ' ' || coalesce(description, ''))"),
            postgresql_using="gin",
        ),
        Index("ix_meetings_org_start", "organization_id", "start_at"),
        # Двойное бронирование календаря невозможно физически. Отменённые
        # встречи из правила исключены: их время снова свободно.
        ExcludeConstraint(
            ("owner_id", "="),
            (text("tstzrange(start_at, end_at)"), "&&"),
            name="excl_meetings_owner_overlap",
            using="gist",
            where=text("status <> 'CANCELLED'"),
        ),
    )

    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Владелец календаря: тот, чьё время занимает встреча.
    owner_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    agenda: Mapped[str | None] = mapped_column(Text, nullable=True)

    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default=MeetingStatus.PLANNED)
    # Приватная встреча видна ассистенту только как занятое время, без темы.
    visibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default=MeetingVisibility.NORMAL
    )
    priority: Mapped[str] = mapped_column(String(12), nullable=False, default=Priority.NORMAL)

    room_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("rooms.id", ondelete="SET NULL"), nullable=True
    )
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)

    created_by: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    # Ассистент создаёт встречу по поручению руководителя - видно обоих.
    on_behalf_of_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    request_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reschedule_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)

    def __repr__(self) -> str:
        return f"<Meeting {self.id} {self.status} {self.title[:30]!r}>"


class MeetingParticipant(Base, PKMixin):
    __tablename__ = "meeting_participants"
    __table_args__ = (
        UniqueConstraint("meeting_id", "user_id", name="uq_meeting_participant"),
        Index("ix_meeting_participants_user", "user_id", "meeting_id"),
    )

    meeting_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False, default=ParticipantRole.REQUIRED)
    response: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ParticipantResponse.NEW
    )
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MeetingRequest(Base, PKMixin, TimestampMixin):
    # Заявка на встречу: сотрудник выбрал окно, руководитель ещё не решил.
    __tablename__ = "meeting_requests"
    __table_args__ = (
        Index("ix_meeting_requests_owner_status", "owner_id", "status"),
    )

    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    initiator_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    owner_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=30)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    status: Mapped[str] = mapped_column(String(12), nullable=False, default=RequestStatus.NEW)
    # Заявка сверх лимита времени не блокируется, но помечается: срочный вопрос
    # не должен упираться в норму, а руководитель видит такие отдельно.
    over_quota: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    decided_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decline_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    meeting_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("meetings.id", ondelete="SET NULL"), nullable=True
    )


class SlotHold(Base, PKMixin):
    # Удержание окна на время рассмотрения заявки. Без него двойные бронирования
    # начались бы на первой же неделе: окно остаётся свободным для других,
    # пока руководитель думает.
    __tablename__ = "slot_holds"
    __table_args__ = (
        # Два действующих удержания одного окна невозможны на уровне базы.
        ExcludeConstraint(
            ("owner_id", "="),
            (text("tstzrange(start_at, end_at)"), "&&"),
            name="excl_slot_holds_overlap",
            using="gist",
            where=text("released_at IS NULL"),
        ),
        Index("ix_slot_holds_expiry", "expires_at", postgresql_where=text("released_at IS NULL")),
    )

    owner_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    request_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("meeting_requests.id", ondelete="CASCADE"), nullable=True
    )
    held_by: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MeetingAttendance(Base, PKMixin):
    # Отметка присутствия. Система фиксирует факт, а не наказывает:
    # интерпретация остаётся за руководителем.
    __tablename__ = "meeting_attendance"
    __table_args__ = (
        UniqueConstraint("meeting_id", "user_id", name="uq_meeting_attendance"),
    )

    meeting_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    late_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default=AttendanceSource.SELF)
    marked_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class MeetingRating(Base, PKMixin):
    # Оценка встречи руководителем: одно касание и необязательный голосовой
    # комментарий. Оценки конкретных встреч видят только руководитель и ассистент.
    __tablename__ = "meeting_ratings"
    __table_args__ = (
        UniqueConstraint("meeting_id", "rated_by", name="uq_meeting_rating"),
    )

    meeting_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rated_by: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # 1 полезная, 0 нейтральная, -1 бесполезная.
    score: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    voice_file_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Room(Base, PKMixin, TimestampMixin):
    __tablename__ = "rooms"

    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    capacity: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class RoomBooking(Base, PKMixin):
    __tablename__ = "room_bookings"
    __table_args__ = (
        # Переговорная не может быть занята дважды.
        ExcludeConstraint(
            ("room_id", "="),
            (text("tstzrange(start_at, end_at)"), "&&"),
            name="excl_room_bookings_overlap",
            using="gist",
        ),
    )

    room_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    meeting_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TimeQuota(Base, PKMixin, TimestampMixin):
    # Сколько времени руководителя полагается отделу или конкретному человеку.
    # Превышение не запрещает заявку, а помечает её - см. MeetingRequest.over_quota.
    __tablename__ = "time_quotas"
    __table_args__ = (
        UniqueConstraint(
            "owner_id", "subject_department_id", "subject_user_id", name="uq_time_quota_subject"
        ),
    )

    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Чьё время расходуется.
    owner_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subject_department_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("departments.id", ondelete="CASCADE"), nullable=True
    )
    subject_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )

    minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=60)
    period: Mapped[str] = mapped_column(String(12), nullable=False, default=QuotaPeriod.WEEK)
