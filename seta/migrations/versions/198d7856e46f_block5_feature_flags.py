"""Блок 5: переключатели разделов

Строка заводится только для отклонения от нормы: отсутствие записи означает
«включено». Поэтому новая организация работает целиком, ничего не заполняя,
а таблица хранит ровно то, что администратор осознанно выключил.

Revision ID: 198d7856e46f
Revises: 250096e3db15
Create Date: 2026-09-05 10:05:46.685229
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '198d7856e46f'
down_revision: str | None = '250096e3db15'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('feature_flags',
    sa.Column('organization_id', sa.BigInteger(), nullable=False),
    sa.Column('code', sa.String(length=40), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('updated_by', sa.BigInteger(), nullable=True),
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('organization_id', 'code', name='uq_feature_flag')
    )
    op.create_index(op.f('ix_feature_flags_organization_id'), 'feature_flags', ['organization_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_feature_flags_organization_id'), table_name='feature_flags')
    op.drop_table('feature_flags')
