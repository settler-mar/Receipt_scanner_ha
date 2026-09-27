"""Bank-transaction import (Monobank first): bank_transactions table

Revision ID: a2b4c6d8e0f1
Revises: 9b7c6d5e4f3a
Create Date: 2026-09-27
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a2b4c6d8e0f1"
down_revision: Union[str, Sequence[str], None] = "9b7c6d5e4f3a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("bank_transactions"):
        return
    op.create_table(
        "bank_transactions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("account_id", sa.String(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("store_code", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(), nullable=False, server_default="UAH"),
        sa.Column("mcc", sa.Integer(), nullable=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("counterparty_raw", sa.JSON(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw", sa.JSON(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "unmatched", "matched", "needs_review", "ignored",
                name="banktransactionstatus",
            ),
            nullable=False,
            server_default="unmatched",
        ),
        sa.Column("linked_receipt_id", sa.String(), sa.ForeignKey("receipts.id"), nullable=True),
        sa.Column("ignore_reason", sa.String(), nullable=True),
        sa.Column(
            "category_override_id",
            sa.String(),
            sa.ForeignKey("categories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("account_id", "external_id", name="uq_bank_transaction_account_external"),
    )
    op.create_index("ix_bank_transactions_account_id", "bank_transactions", ["account_id"])
    op.create_index("ix_bank_transactions_store_code", "bank_transactions", ["store_code"])
    op.create_index("ix_bank_transactions_external_id", "bank_transactions", ["external_id"])
    op.create_index("ix_bank_transactions_occurred_at", "bank_transactions", ["occurred_at"])
    op.create_index("ix_bank_transactions_status", "bank_transactions", ["status"])
    op.create_index("ix_bank_transactions_linked_receipt_id", "bank_transactions", ["linked_receipt_id"])


def downgrade() -> None:
    if _has_table("bank_transactions"):
        op.drop_table("bank_transactions")
