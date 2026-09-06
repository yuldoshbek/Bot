"""Блок 6: голосовые сообщения и расшифровки

Запись хранится вместе с расшифровкой, а не вместо неё: распознавание
ошибается, и спор «я такого не говорил» разрешается только звуком.

Уникальность по (организация, file_unique_id) делает повторную расшифровку
невозможной: пересланное второй раз голосовое — тот же файл, и время
человека на него тратить незачем.

Revision ID: 36f2b87b5662
Revises: c867911df459
Create Date: 2026-09-06 13:31:22.609719
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '36f2b87b5662'
down_revision: str | None = 'c867911df459'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('voice_notes',
    sa.Column('organization_id', sa.BigInteger(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('file_id', sa.String(length=256), nullable=False),
    sa.Column('file_unique_id', sa.String(length=128), nullable=False),
    sa.Column('duration_seconds', sa.Integer(), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('transcript', sa.Text(), nullable=True),
    sa.Column('model', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('organization_id', 'file_unique_id', name='uq_voice_note_file')
    )
    op.create_index('ix_voice_notes_author', 'voice_notes', ['organization_id', 'user_id', 'created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_voice_notes_author', table_name='voice_notes')
    op.drop_table('voice_notes')
