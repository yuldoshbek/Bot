from sqlalchemy import BigInteger, Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PKMixin, TimestampMixin


class Organization(Base, PKMixin, TimestampMixin):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Tashkent")


class Department(Base, PKMixin, TimestampMixin):
    __tablename__ = "departments"

    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("departments.id", ondelete="SET NULL"), nullable=True
    )
    head_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class FeatureFlag(Base, PKMixin, TimestampMixin):
    # Переключатель раздела. Строки заводятся только для отклонений от нормы:
    # отсутствие записи означает «включено», поэтому новая организация работает
    # целиком, ничего предварительно не заполняя.
    __tablename__ = "feature_flags"
    __table_args__ = (
        UniqueConstraint("organization_id", "code", name="uq_feature_flag"),
    )

    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
