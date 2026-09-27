"""Store unexplained receipt-level amount corrections.

Revision ID: b4c6d8e0f2a4
Revises: a2b4c6d8e0f1
Create Date: 2026-09-27
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b4c6d8e0f2a4"
down_revision: Union[str, Sequence[str], None] = "a2b4c6d8e0f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("receipts")}
    if "total_adjustment" not in columns:
        op.add_column("receipts", sa.Column("total_adjustment", sa.Numeric(12, 2), nullable=False, server_default="0"))
    if "adjustment_reason" not in columns:
        op.add_column("receipts", sa.Column("adjustment_reason", sa.String(), nullable=True))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("receipts")}
    if "adjustment_reason" in columns:
        op.drop_column("receipts", "adjustment_reason")
    if "total_adjustment" in columns:
        op.drop_column("receipts", "total_adjustment")
