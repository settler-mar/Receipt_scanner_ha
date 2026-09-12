"""receipt exchange rate to UAH

Revision ID: f2e3d4c5b6a7
Revises: e1d2c3b4a5f6
Create Date: 2026-09-03
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f2e3d4c5b6a7"
down_revision: Union[str, Sequence[str], None] = "e1d2c3b4a5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    if not _has_table(table):
        return False
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if not _has_column("receipts", "exchange_rate_to_uah"):
        op.add_column("receipts", sa.Column("exchange_rate_to_uah", sa.Numeric(18, 6), nullable=True))


def downgrade() -> None:
    if _has_column("receipts", "exchange_rate_to_uah"):
        op.drop_column("receipts", "exchange_rate_to_uah")

