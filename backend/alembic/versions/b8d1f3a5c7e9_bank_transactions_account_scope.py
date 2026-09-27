"""Fix bank_transactions to be account-scoped (external_id/account_id/store_code)

The original bank_transactions migration (a2b4c6d8e0f1) was edited in place
after it had already run against a live dev DB under its pre-release,
Monobank-only shape (`mono_id`, no account scoping) — editing an
already-applied migration's upgrade() has no effect on a DB that already
passed through it, so this repairs it forward instead.

Revision ID: b8d1f3a5c7e9
Revises: f0a2c4e6b8d9
Create Date: 2026-09-27
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b8d1f3a5c7e9"
down_revision: Union[str, Sequence[str], None] = "f0a2c4e6b8d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if _has_table("bank_transactions") and not _has_column("bank_transactions", "external_id"):
        # Empty under the old shape (the account-scoped ingest path never
        # successfully wrote a row against it) — safe to drop and recreate
        # rather than rename/backfill columns.
        op.drop_table("bank_transactions")

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
