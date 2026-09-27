"""repair ATB line totals that were stored before discounts

Revision ID: f3a4b5c6d7e8
Revises: f1a2b3c4d5e6
Create Date: 2026-09-27
"""
from __future__ import annotations

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f3a4b5c6d7e8"
down_revision: Union[str, Sequence[str], None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    receipts = sa.table(
        "receipts",
        sa.column("id", sa.String),
        sa.column("store_code", sa.String),
        sa.column("raw", sa.JSON),
        sa.column("total_discount", sa.Numeric),
    )
    items = sa.table(
        "receipt_items",
        sa.column("id", sa.String),
        sa.column("receipt_id", sa.String),
        sa.column("name", sa.String),
        sa.column("quantity", sa.Numeric),
        sa.column("unit_price", sa.Numeric),
        sa.column("total_price", sa.Numeric),
        sa.column("discount", sa.Numeric),
    )

    rows = bind.execute(
        sa.select(receipts.c.id, receipts.c.raw).where(receipts.c.store_code == "atb")
    ).all()
    for receipt_id, raw in rows:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                raw = {}
        details = (raw or {}).get("details") or {}
        if not isinstance(details, list):
            continue

        receipt_items = bind.execute(
            sa.select(
                items.c.id,
                items.c.name,
                items.c.quantity,
                items.c.total_price,
            ).where(items.c.receipt_id == receipt_id)
        ).all()
        unmatched = list(receipt_items)
        discounts_total = 0.0
        for detail in details:
            name = (detail.get("name") or "").strip()
            quantity = float(detail.get("qty") or 1)
            gross_total = float(str(detail.get("sum") or 0).replace(",", "."))
            discount = float(str(detail.get("discount") or 0).replace(",", "."))
            gross_unit = detail.get("price")
            gross_unit = float(str(gross_unit).replace(",", ".")) if gross_unit not in (None, "") else None
            target = next(
                (
                    row for row in unmatched
                    if row.name == name and abs(float(row.quantity or 0) - quantity) < 0.001
                ),
                None,
            )
            if target is None:
                continue
            unmatched.remove(target)
            values = {
                "total_price": round(max(gross_total - discount, 0), 2),
                "discount": round(discount, 2),
            }
            if gross_unit is not None and quantity > 0 and discount:
                values["unit_price"] = round(gross_unit - discount / quantity, 2)
            bind.execute(
                sa.update(items).where(items.c.id == target.id).values(**values)
            )
            discounts_total += discount
        if discounts_total:
            bind.execute(
                sa.update(receipts)
                .where(receipts.c.id == receipt_id)
                .values(total_discount=round(discounts_total, 2))
            )


def downgrade() -> None:
    # The previous gross line values cannot be recovered safely after this
    # repair without retaining a second copy of every item.
    pass
