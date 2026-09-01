"""Документы: идентификатор в Telegram, права на файл, журнал открытий, текст.

Файл у нас не хранится. Telegram держит его бесплатно и без ограничения по объёму
и возвращает идентификатор — мы сохраняем идентификатор, метаданные и политику
доступа. Отсюда два следствия, которые надо помнить:

  * идентификатор привязан к конкретному боту: смена токена бота обесценивает все
    сохранённые `file_id`, поэтому важные документы дублируются на диск сервера;
  * скачать файл бот может только до 20 МБ. Больший документ сохраняется и
    пересылается, но текст из него не извлекается — это отмечено состоянием.

**Доступ к встрече не даёт доступа к её документам.** Право на файл проверяется
отдельно и всегда: это правило раздела «Безопасность» архитектуры, а не удобство.
"""
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PKMixin, TimestampMixin
from app.models.enums import DocumentScope, IndexStatus, ViewChannel


class Document(Base, PKMixin, TimestampMixin):
    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_org_created", "organization_id", "created_at"),
        Index("ix_documents_meeting", "meeting_id"),
        Index("ix_documents_task", "task_id"),
        # Поиск по имени файла прощает опечатку: «догвор» найдёт «договор».
        Index(
            "ix_documents_name_trgm", "file_name",
            postgresql_using="gin", postgresql_ops={"file_name": "gin_trgm_ops"},
        ),
    )

    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Идентификатор файла в Telegram и стабильный идентификатор содержимого:
    # второй переживает пересылки и позволяет узнать повторную загрузку.
    file_id: Mapped[str] = mapped_column(String(256), nullable=False)
    file_unique_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    file_name: Mapped[str] = mapped_column(String(300), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    uploaded_by: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)

    # Привязки: документ может относиться к встрече, поручению или решению.
    meeting_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("meetings.id", ondelete="SET NULL"), nullable=True
    )
    task_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    decision_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("decisions.id", ondelete="SET NULL"), nullable=True
    )

    # Кому открыт по умолчанию. Точечные исключения — в document_access.
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default=DocumentScope.PRIVATE)
    # Важные документы (протоколы, приказы) дополнительно копируются на диск:
    # идентификаторы Telegram живут ровно столько, сколько живёт токен бота.
    is_important: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    local_path: Mapped[str | None] = mapped_column(String(500), nullable=True)

    index_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=IndexStatus.PENDING, index=True
    )
    index_error: Mapped[str | None] = mapped_column(String(300), nullable=True)


class DocumentAccess(Base, PKMixin, TimestampMixin):
    # Точечная выдача доступа: человеку или отделу. Владелец и так видит свой файл.
    __tablename__ = "document_access"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "subject_user_id", "subject_department_id",
            name="uq_document_access_subject",
        ),
    )

    document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subject_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    subject_department_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("departments.id", ondelete="CASCADE"), nullable=True
    )
    granted_by: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )


class DocumentView(Base, PKMixin):
    # Журнал открытий: каждое получение файла оставляет запись. Только добавление.
    __tablename__ = "document_views"
    __table_args__ = (
        Index("ix_document_views_doc_at", "document_id", "viewed_at"),
    )

    document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(12), nullable=False, default=ViewChannel.BOT)
    viewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DocumentText(Base, PKMixin):
    # Извлечённый текст отдельной таблицей: сам документ читается часто и должен
    # оставаться узким, а текст на двести страниц тянуть при каждом показе незачем.
    __tablename__ = "document_texts"
    __table_args__ = (
        UniqueConstraint("document_id", name="uq_document_text"),
        Index("ix_document_texts_search", "search_vector", postgresql_using="gin"),
    )

    document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Заполняется при записи текста: хранимый вектор дешевле, чем пересчёт
    # морфологии на каждый запрос по большому документу.
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)
    pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DownloadToken(Base, PKMixin):
    # Одноразовая ссылка для Mini App блока 5. Пока интерфейс — Telegram,
    # выдача идёт пересылкой файла ботом, но механизм закладывается сейчас,
    # чтобы потом не переделывать журнал открытий.
    __tablename__ = "download_tokens"
    __table_args__ = (
        UniqueConstraint("token", name="uq_download_token"),
        Index("ix_download_tokens_expiry", "expires_at", postgresql_where=text("used_at IS NULL")),
    )

    token: Mapped[str] = mapped_column(String(64), nullable=False)
    document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    issued_to: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
