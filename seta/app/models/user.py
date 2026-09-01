from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PKMixin, TimestampMixin
from app.models.enums import RoleCode, UserStatus


class User(Base, PKMixin, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (
        # Поиск исполнителя по части фамилии. Без триграммного индекса
        # LIKE '%...%' читает таблицу целиком на каждую букву.
        Index(
            "ix_users_full_name_trgm",
            text("lower(full_name) gin_trgm_ops"),
            postgresql_using="gin",
        ),
    )

    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True, index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(64), nullable=True)

    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    position: Mapped[str | None] = mapped_column(String(200), nullable=True)

    department_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("departments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    manager_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False, default=UserStatus.PENDING)
    requested_role: Mapped[str | None] = mapped_column(String(20), nullable=True)

    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Tashkent")
    locale: Mapped[str] = mapped_column(String(10), nullable=False, default="ru")

    notifications_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<User {self.id} {self.full_name!r} tg={self.telegram_user_id}>"


class Invite(Base, PKMixin, TimestampMixin):
    """Приглашение: персональное (одноразовое) или отдельское (многоразовое)."""
    __tablename__ = "invites"
    __table_args__ = (UniqueConstraint("token", name="uq_invites_token"),)

    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    token: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    department_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("departments.id", ondelete="SET NULL"), nullable=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=RoleCode.EMPLOYEE)

    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_multi_use: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_uses: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    used_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
