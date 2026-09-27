"""Persist receipt reconciliation aggregates for fast list queries."""
from __future__ import annotations

import datetime as dt
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f0a2c4e6b8d9"
down_revision: Union[str, Sequence[str], None] = "e8a0c2e4b6d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    existing_columns = {column["name"] for column in sa.inspect(bind).get_columns("receipts")}
    columns = {
        "items_total": sa.Numeric(12, 2),
        "components_total": sa.Numeric(12, 2),
        "reconciled_total": sa.Numeric(12, 2),
        "reconciliation_delta": sa.Numeric(12, 2),
        "reconciliation_status": sa.String(),
        "reconciliation_calculated_at": sa.DateTime(timezone=True),
    }
    for name, type_ in columns.items():
        if name not in existing_columns:
            op.add_column("receipts", sa.Column(name, type_, nullable=name != "reconciliation_calculated_at", server_default="0" if name != "reconciliation_calculated_at" else None))
    existing_indexes = {index["name"] for index in sa.inspect(bind).get_indexes("receipts")}
    if "ix_receipts_reconciliation_status" not in existing_indexes:
        op.create_index("ix_receipts_reconciliation_status", "receipts", ["reconciliation_status"])
    if "ix_receipts_reconciliation_delta" not in existing_indexes:
        op.create_index("ix_receipts_reconciliation_delta", "receipts", ["reconciliation_delta"])
    receipts = sa.table("receipts", sa.column("id", sa.String), sa.column("total_amount", sa.Numeric),
                        sa.column("total_adjustment", sa.Numeric),
                        sa.column("items_total", sa.Numeric), sa.column("components_total", sa.Numeric),
                        sa.column("reconciled_total", sa.Numeric), sa.column("reconciliation_delta", sa.Numeric),
                        sa.column("reconciliation_status", sa.String),
                        sa.column("reconciliation_calculated_at", sa.DateTime()))
    items = sa.table("receipt_items", sa.column("receipt_id", sa.String), sa.column("total_price", sa.Numeric))
    components = sa.table("receipt_components", sa.column("receipt_id", sa.String), sa.column("amount", sa.Numeric))
    rows = bind.execute(sa.select(receipts.c.id, receipts.c.total_amount, receipts.c.total_adjustment)).all()
    now = dt.datetime.utcnow()
    for receipt_id, total_amount, total_adjustment in rows:
        items_total = float(bind.execute(sa.select(sa.func.coalesce(sa.func.sum(items.c.total_price), 0)).where(items.c.receipt_id == receipt_id)).scalar_one() or 0)
        components_total = float(bind.execute(sa.select(sa.func.coalesce(sa.func.sum(components.c.amount), 0)).where(components.c.receipt_id == receipt_id)).scalar_one() or 0)
        reconciled = round(items_total + components_total + float(total_adjustment or 0), 2)
        delta = round(reconciled - float(total_amount or 0), 2)
        bind.execute(sa.update(receipts).where(receipts.c.id == receipt_id).values(
            items_total=round(items_total, 2), components_total=round(components_total, 2),
            reconciled_total=reconciled, reconciliation_delta=delta,
            reconciliation_status="open" if abs(delta) >= 0.01 else "ok",
            reconciliation_calculated_at=now,
        ))


def downgrade() -> None:
    op.drop_index("ix_receipts_reconciliation_delta", table_name="receipts")
    op.drop_index("ix_receipts_reconciliation_status", table_name="receipts")
    for column in ("reconciliation_calculated_at", "reconciliation_status", "reconciliation_delta",
                   "reconciled_total", "components_total", "items_total"):
        op.drop_column("receipts", column)
