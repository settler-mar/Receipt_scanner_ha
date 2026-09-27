"""unified operational journal (app_events)

Revision ID: d1e2f3a4b5c6
Revises: c9f2a6b8d1e3
Create Date: 2026-09-17
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, Sequence[str], None] = "c9f2a6b8d1e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("app_events"):
        return
    op.create_table(
        "app_events",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("level", sa.String(), nullable=False, server_default="info"),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
    )
    op.create_index("ix_app_events_created_at", "app_events", ["created_at"])
    op.create_index("ix_app_events_source", "app_events", ["source"])
    op.create_index("ix_app_events_created_source", "app_events", ["created_at", "source"])


def downgrade() -> None:
    if _has_table("app_events"):
        op.drop_table("app_events")
