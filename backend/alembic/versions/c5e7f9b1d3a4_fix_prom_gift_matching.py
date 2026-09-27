"""Match Prom cart rows by product name when repairing legacy receipts."""
from __future__ import annotations

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c5e7f9b1d3a4"
down_revision: Union[str, Sequence[str], None] = "b4d6e8f0a2c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _raw(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def upgrade() -> None:
    bind = op.get_bind()
    receipts = sa.table("receipts", sa.column("id", sa.String), sa.column("store_code", sa.String), sa.column("raw", sa.JSON))
    items = sa.table(
        "receipt_items", sa.column("receipt_id", sa.String), sa.column("name", sa.String),
        sa.column("unit_price", sa.Numeric), sa.column("total_price", sa.Numeric), sa.column("discount", sa.Numeric),
    )
    for receipt_id, raw_value in bind.execute(sa.select(receipts.c.id, receipts.c.raw).where(receipts.c.store_code == "prom")):
        raw = _raw(raw_value)
        cart_items = ((raw.get("cart") or {}).get("cartItems") or [])
        db_items = bind.execute(sa.select(items.c.name).where(items.c.receipt_id == receipt_id)).all()
        for raw_item in cart_items:
            name = str(raw_item.get("productName") or "").strip()
            if not name:
                continue
            match = next((db_name for db_name, in db_items if db_name == name), None)
            if match is None:
                continue
            quantity = float(raw_item.get("quantity") or 1)
            unit = float(raw_item.get("productPrice") or 0)
            base = float(raw_item.get("productBasePrice") or unit)
            gift = bool(raw_item.get("isGift"))
            bind.execute(sa.update(items).where(
                (items.c.receipt_id == receipt_id) & (items.c.name == match)
            ).values(
                unit_price=0 if gift else unit,
                total_price=0 if gift else round(unit * quantity, 2),
                discount=round(base * quantity, 2) if gift else max(0, round((base - unit) * quantity, 2)),
            ))


def downgrade() -> None:
    pass
