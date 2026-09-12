"""language a voice message was recognised in

Revision ID: a3f1c9d0e2b5
Revises: c8a5d1e7f204
Create Date: 2026-09-10
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a3f1c9d0e2b5"
down_revision: Union[str, Sequence[str], None] = "c8a5d1e7f204"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    if not _has_column("voice_messages", "language"):
        op.add_column("voice_messages", sa.Column("language", sa.String(), nullable=True))


def downgrade() -> None:
    if _has_column("voice_messages", "language"):
        op.drop_column("voice_messages", "language")
