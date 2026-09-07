"""Голосовые сообщения и их расшифровки.

**Голосовое сохраняется.** Telegram хранит файл по `file_id` сколько угодно
долго, но `file_id` живёт только в самом сообщении: пролистали чат — и запись
не найти. Здесь остаётся ссылка на файл, длительность и расшифровка, поэтому
к сказанному можно вернуться и через полгода.

**Расшифровка хранится рядом с записью, а не вместо неё.** Распознавание
ошибается, и спор «я такого не говорил» разрешается только звуком. Текст —
удобство чтения, запись — свидетельство.

**Одно и то же не расшифровывается дважды.** `file_unique_id` у Telegram
постоянен для файла: пересланное второй раз голосовое — тот же файл.
Уникальность в схеме превращает это в факт, а не в намерение: повторная
расшифровка не стоила бы денег только у своей службы, а время человека
тратила бы всегда.
"""
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PKMixin


class VoiceNote(Base, PKMixin):
    __tablename__ = "voice_notes"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "file_unique_id", name="uq_voice_note_file"
        ),
        Index("ix_voice_notes_author", "organization_id", "user_id", "created_at"),
    )

    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    # Ссылка на файл в Telegram. `file_id` меняется от бота к боту,
    # `file_unique_id` постоянен — по нему и узнаётся повтор.
    file_id: Mapped[str] = mapped_column(String(256), nullable=False)
    file_unique_id: Mapped[str] = mapped_column(String(128), nullable=False)

    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    # Пусто — расшифровки нет: не смогли или не настроено. Запись при этом
    # всё равно сохранена, и расшифровать её можно позже.
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
