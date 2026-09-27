"""archive flag for voice messages

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-09-17
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e2f3a4b5c6d7"
down_revision: Union[str, Sequence[str], None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    if not _has_column("voice_messages", "archived_at"):
        op.add_column("voice_messages",
                      sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
        op.create_index("ix_voice_messages_archived_at", "voice_messages", ["archived_at"])


def downgrade() -> None:
    if _has_column("voice_messages", "archived_at"):
        op.drop_index("ix_voice_messages_archived_at", table_name="voice_messages")
        op.drop_column("voice_messages", "archived_at")
