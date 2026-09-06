"""Журнал обращений к ИИ.

Каждый вызов записывается: тип, модель, версия промпта, стоимость, кто
инициировал, подтвердил ли человек результат. Без этого нельзя ни посчитать
расход, ни разобрать жалобу «бот придумал поручение».

**Стоимость пишется до ответа, а не после.** Оборвавшийся вызов оплачен
поставщиком так же, как удавшийся, и если писать расход по факту успеха,
потолок обходится повторными обрывами. Поэтому строка создаётся перед
обращением, а ответ дописывается в неё же.

**`confirmed` — не украшение.** Это единственный способ потом ответить
на вопрос «система сама завела поручение или человек подтвердил». Пустое
значение означает, что подтверждения и не требовалось (сводка, отчёт);
`false` — что человек отказался.
"""
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PKMixin


class AiCall(Base, PKMixin):
    __tablename__ = "ai_calls"
    __table_args__ = (
        # Расход считается за сутки и за месяц по организации — индекс под это.
        Index("ix_ai_calls_spend", "organization_id", "started_at"),
    )

    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # Кто инициировал. NULL — фоновый цикл: у сводки и отчёта инициатора нет.
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # Что делали: digest, voice_task, protocol, weekly_report, ask, meeting_quality.
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    # Версия промпта: без неё нельзя понять, почему ответы вчера были другими.
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Стоимость в долларах. Numeric, а не float: расход складывается тысячами
    # строк, и накопленная ошибка двоичной дроби превращает потолок в решето.
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False, default=0)
    tokens_in: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # None — подтверждения не требовалось. True/False — человек решил.
    confirmed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
