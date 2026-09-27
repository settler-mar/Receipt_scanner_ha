"""Bank-transaction auto-categorization rules (bank_transaction_rules table
+ bank_transactions.auto_rule_id)

Revision ID: c1e3a5b7d9f0
Revises: h2c4e6a8b0d1
Create Date: 2026-09-27
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c1e3a5b7d9f0"
down_revision: Union[str, Sequence[str], None] = "h2c4e6a8b0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if not _has_table("bank_transaction_rules"):
        op.create_table(
            "bank_transaction_rules",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("label", sa.String(), nullable=False),
            sa.Column("counterparty_match", sa.String(), nullable=False),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("branches", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    if _has_table("bank_transactions") and not _has_column("bank_transactions", "auto_rule_id"):
        with op.batch_alter_table("bank_transactions") as batch_op:
            batch_op.add_column(sa.Column("auto_rule_id", sa.String(), nullable=True))
            batch_op.create_foreign_key(
                "fk_bank_transactions_auto_rule_id",
                "bank_transaction_rules", ["auto_rule_id"], ["id"],
                ondelete="SET NULL",
            )


def downgrade() -> None:
    if _has_table("bank_transactions") and _has_column("bank_transactions", "auto_rule_id"):
        with op.batch_alter_table("bank_transactions") as batch_op:
            batch_op.drop_column("auto_rule_id")
    if _has_table("bank_transaction_rules"):
        op.drop_table("bank_transaction_rules")
