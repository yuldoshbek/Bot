"""Блок укрепления: границы организаций, эскалация, откат, индексы впрок

Миграция написана руками: автогенерация не умеет ни расширений PostgreSQL,
ни частичных индексов. Делается на пустой базе намеренно — те же изменения
через полгода означали бы перенос миллионов строк под блокировкой.

Revision ID: a1c4e7b9d200
Revises: 616f2abfae00
Create Date: 2026-09-01
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c4e7b9d200"
down_revision: str | None = "616f2abfae00"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Статусы, в которых поручение ещё ждёт исполнителя. Дублируются здесь строками
# намеренно: миграция должна пережить любое будущее переименование в коде.
PENDING = "'NEW','ACKNOWLEDGED','IN_PROGRESS','BLOCKED','OVERDUE'"


def upgrade() -> None:
    # ── Расширения ──────────────────────────────────────────────────────────
    # pg_trgm нужен для поиска сотрудника по части фамилии: без него LIKE '%...%'
    # не может использовать индекс и читает таблицу целиком.
    # btree_gist понадобится в блоке 3 для запрета пересекающихся броней.
    # Включать их на пустой базе — секунда; на боевой это отдельная процедура
    # с правами суперпользователя.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    # ── Ступень эскалации ───────────────────────────────────────────────────
    op.add_column(
        "tasks",
        sa.Column("escalation_level", sa.SmallInteger(), nullable=False, server_default="0"),
    )

    # ── Отложенная повторная попытка доставки ───────────────────────────────
    op.add_column(
        "notifications",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ── Границы организаций там, где выборка идёт без привязки к человеку ───
    op.add_column("notifications", sa.Column("organization_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_notifications_organization", "notifications", "organizations",
        ["organization_id"], ["id"], ondelete="CASCADE",
    )
    op.create_index("ix_notifications_organization_id", "notifications", ["organization_id"])

    op.add_column("audit_log", sa.Column("organization_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_audit_log_organization", "audit_log", "organizations",
        ["organization_id"], ["id"], ondelete="CASCADE",
    )
    op.create_index("ix_audit_log_organization_id", "audit_log", ["organization_id"])

    # Заполняем существующие строки: организация берётся у владельца записи.
    op.execute(
        "UPDATE notifications n SET organization_id = u.organization_id "
        "FROM users u WHERE u.id = n.user_id AND n.organization_id IS NULL"
    )
    op.execute(
        "UPDATE audit_log a SET organization_id = u.organization_id "
        "FROM users u WHERE u.id = a.actor_id AND a.organization_id IS NULL"
    )

    # ── Один открытый запрос на продление ───────────────────────────────────
    # Правило живёт в схеме, а не в аккуратности вызывающего кода: десять
    # одновременных нажатий «Продлить» физически не создадут десять запросов.
    op.execute(
        "CREATE UNIQUE INDEX uq_task_extension_open ON task_extensions (task_id) "
        "WHERE status = 'NEW'"
    )

    # ── Индексы под реальные выборки ────────────────────────────────────────
    # Планировщик читает только поручения с близким сроком. Без частичного
    # индекса он сканировал бы всю таблицу, включая выполненные и отменённые.
    op.execute(
        "CREATE INDEX ix_tasks_due_watch ON tasks (due_at) "
        f"WHERE status IN ({PENDING}) AND due_at IS NOT NULL"
    )
    # Очередь ищет несколько десятков PENDING среди истории, где 99% — SENT.
    op.execute(
        "CREATE INDEX ix_notifications_pending ON notifications (scheduled_at) "
        "WHERE status = 'PENDING'"
    )
    # Индекс по kind не используется ни одним запросом и замедляет каждую вставку.
    op.drop_index("ix_notifications_kind", table_name="notifications")

    # Поиск исполнителя по части фамилии.
    op.execute(
        "CREATE INDEX ix_users_full_name_trgm ON users USING gin (lower(full_name) gin_trgm_ops)"
    )

    # ── Индексы впрок под блок 3: пересечение интервалов ────────────────────
    op.create_index(
        "ix_calendar_blocks_user_interval", "calendar_blocks", ["user_id", "start_at", "end_at"]
    )
    op.create_index(
        "ix_absences_user_interval", "absences", ["user_id", "start_date", "end_date"]
    )
    op.create_index("ix_holidays_org_day", "holidays", ["organization_id", "day"])

    # ── Запрет двойного бронирования средствами самой базы ──────────────────
    # Пересекающиеся блокировки календаря одного человека станут невозможны:
    # база отвергнет вторую запись, независимо от того, что решил код.
    op.execute(
        "ALTER TABLE calendar_blocks ADD CONSTRAINT excl_calendar_blocks_overlap "
        "EXCLUDE USING gist (user_id WITH =, tstzrange(start_at, end_at) WITH &&)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE calendar_blocks DROP CONSTRAINT IF EXISTS excl_calendar_blocks_overlap")
    op.drop_index("ix_holidays_org_day", table_name="holidays")
    op.drop_index("ix_absences_user_interval", table_name="absences")
    op.drop_index("ix_calendar_blocks_user_interval", table_name="calendar_blocks")
    op.execute("DROP INDEX IF EXISTS ix_users_full_name_trgm")
    op.create_index("ix_notifications_kind", "notifications", ["kind"])
    op.execute("DROP INDEX IF EXISTS ix_notifications_pending")
    op.execute("DROP INDEX IF EXISTS ix_tasks_due_watch")
    op.execute("DROP INDEX IF EXISTS uq_task_extension_open")

    op.drop_index("ix_audit_log_organization_id", table_name="audit_log")
    op.drop_constraint("fk_audit_log_organization", "audit_log", type_="foreignkey")
    op.drop_column("audit_log", "organization_id")

    op.drop_index("ix_notifications_organization_id", table_name="notifications")
    op.drop_constraint("fk_notifications_organization", "notifications", type_="foreignkey")
    op.drop_column("notifications", "organization_id")
    op.drop_column("notifications", "next_attempt_at")

    op.drop_column("tasks", "escalation_level")
