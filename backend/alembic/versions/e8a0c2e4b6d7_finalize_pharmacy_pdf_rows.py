"""Finalize reloaded pharmacy rows and the Vencon fiscal PDF."""
from __future__ import annotations

import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e8a0c2e4b6d7"
down_revision: Union[str, Sequence[str], None] = "d6f8a0c2e4b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    receipts = sa.table("receipts", sa.column("id", sa.String), sa.column("store_code", sa.String), sa.column("external_id", sa.String))
    items = sa.table("receipt_items", sa.column("id", sa.String), sa.column("receipt_id", sa.String), sa.column("name", sa.String), sa.column("quantity", sa.Numeric), sa.column("unit_price", sa.Numeric), sa.column("total_price", sa.Numeric))
    components = sa.table("receipt_components", sa.column("id", sa.String), sa.column("receipt_id", sa.String), sa.column("code", sa.String))

    # Liki's first parser used abbreviated names. The richer reload then
    # appended the same lines because names differed; remove only exact
    # amount/quantity duplicates, preserving the first row ids.
    for receipt_id, in bind.execute(sa.select(receipts.c.id).where(receipts.c.store_code == "liki")):
        rows = bind.execute(sa.select(items.c.id, items.c.quantity, items.c.total_price).where(items.c.receipt_id == receipt_id).order_by(items.c.id)).all()
        seen = set()
        for item_id, quantity, total_price in rows:
            key = (round(float(quantity or 0), 3), round(float(total_price or 0), 2))
            if key in seen:
                bind.execute(sa.delete(items).where(items.c.id == item_id))
            else:
                seen.add(key)

    # Vencon's order HTML exposed a stale line total, while its saved fiscal
    # PDF contains the authoritative paid line and total.
    receipt = bind.execute(sa.select(receipts.c.id).where(
        (receipts.c.store_code == "vencon") & (receipts.c.external_id == "331681")
    )).scalar_one_or_none()
    if receipt:
        rows = bind.execute(sa.select(items.c.id).where(items.c.receipt_id == receipt)).all()
        if rows:
            bind.execute(sa.update(items).where(items.c.id == rows[0].id).values(
                quantity=1, unit_price=2660, total_price=2660
            ))
            if len(rows) > 1:
                for row in rows[1:]:
                    bind.execute(sa.delete(items).where(items.c.id == row.id))
        bind.execute(sa.delete(components).where(
            (components.c.receipt_id == receipt) & (components.c.code == "source_missing_lines")
        ))


def downgrade() -> None:
    pass
