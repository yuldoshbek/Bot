"""Поручения: сам объект, история, комментарии, продления, шаблоны."""
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
from app.models.enums import ExtensionStatus, Priority, TaskEventKind, TaskStatus


class Task(Base, PKMixin, TimestampMixin):
    # Центральный объект системы. Всё остальное - встречи, решения, контроль -
    # существует ради того, чтобы поручение появилось, было выполнено и проверено.
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_assignee_status_due", "assignee_id", "status", "due_at"),
        Index("ix_tasks_reviewer_status", "reviewer_id", "status"),
        Index("ix_tasks_creator_status", "creator_id", "status"),
        # Планировщик читает только поручения с близким сроком. Частичный индекс
        # держит его стоимость независимой от размера архива выполненных.
        Index(
            "ix_tasks_due_watch",
            "due_at",
            postgresql_where=text("status IN ('NEW','ACKNOWLEDGED','IN_PROGRESS','BLOCKED','OVERDUE') AND due_at IS NOT NULL"),
        ),
    )

    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    creator_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    assignee_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    reviewer_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Ассистент создаёт поручение по поручению руководителя: в карточке и в журнале
    # видно обоих. Скрытых действий от чужого имени в системе не бывает.
    on_behalf_of_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    department_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("departments.id", ondelete="SET NULL"), nullable=True, index=True
    )

    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    priority: Mapped[str] = mapped_column(String(12), nullable=False, default=Priority.NORMAL)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=TaskStatus.NEW)

    requires_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Руководитель пометил поручение лично: о просрочке он узнаёт сразу,
    # а не в общей утренней сводке.
    personal_control: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Источник поручения - встреча или решение (блоки 3 и 4).
    meeting_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    decision_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # До какой ступени эскалации уже дошло поручение: 0 - норма, 1 - просрочено,
    # 2 - сообщено начальнику отдела, 3 - подключён ассистент.
    # Без этого поля планировщик каждую минуту пытался бы вставить уведомление
    # заново: конфликт по event_key его отбрасывает, но мёртвая строка в таблице
    # и в индексе всё равно остаётся, и база пухнет на ровном месте.
    escalation_level: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)

    # Сколько раз работу возвращали на доработку - основа показателя качества.
    rework_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    extensions_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)

    def __repr__(self) -> str:
        return f"<Task {self.id} {self.status} {self.title[:30]!r}>"


class TaskEvent(Base, PKMixin):
    # Полная история жизни поручения: кто, что и когда изменил.
    __tablename__ = "task_events"

    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default=TaskEventKind.CREATED)
    from_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class TaskComment(Base, PKMixin):
    # Обсуждение внутри поручения, а не в личных чатах: через месяц будет понятно,
    # почему срок сдвинулся и что мешало.
    __tablename__ = "task_comments"

    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_file_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    file_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TaskExtension(Base, PKMixin):
    # Исполнитель не двигает срок сам: он просит, автор решает.
    # Каждое продление сохраняется вместе с причиной.
    __tablename__ = "task_extensions"
    __table_args__ = (
        # Один открытый запрос на поручение — правило в схеме, а не в коде.
        Index(
            "uq_task_extension_open",
            "task_id",
            unique=True,
            postgresql_where=text("status = 'NEW'"),
        ),
    )

    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requested_by: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    old_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    new_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[str] = mapped_column(String(12), nullable=False, default=ExtensionStatus.NEW)
    decided_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TaskTemplate(Base, PKMixin, TimestampMixin):
    # Типовое поручение в одно нажатие.
    __tablename__ = "task_templates"

    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_priority: Mapped[str] = mapped_column(String(12), nullable=False, default=Priority.NORMAL)
    default_assignee_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    default_days: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=3)
    requires_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
