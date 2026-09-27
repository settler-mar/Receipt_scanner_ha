"""Backfill provider-level totals that are not product lines."""
from __future__ import annotations

import json
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b4d6e8f0a2c3"
down_revision: Union[str, Sequence[str], None] = "a3c5e7f9b1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _raw(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def _add(bind, table, receipt_id, kind, code, label, amount, source, raw):
    if abs(amount) < 0.005:
        return
    bind.execute(table.insert().values(
        id=str(uuid.uuid4()), receipt_id=receipt_id, kind=kind, code=code,
        label=label, amount=round(amount, 2), currency="UAH", source=source, raw=raw,
    ))


def upgrade() -> None:
    bind = op.get_bind()
    receipts = sa.table(
        "receipts", sa.column("id", sa.String), sa.column("store_code", sa.String),
        sa.column("raw", sa.JSON), sa.column("total_adjustment", sa.Numeric),
        sa.column("adjustment_reason", sa.String), sa.column("total_discount", sa.Numeric),
    )
    components = sa.table(
        "receipt_components", sa.column("id", sa.String), sa.column("receipt_id", sa.String),
        sa.column("kind", sa.String), sa.column("code", sa.String), sa.column("label", sa.String),
        sa.column("amount", sa.Numeric), sa.column("currency", sa.String),
        sa.column("source", sa.String), sa.column("raw", sa.JSON),
    )
    items = sa.table(
        "receipt_items", sa.column("id", sa.String), sa.column("receipt_id", sa.String),
        sa.column("unit_price", sa.Numeric), sa.column("total_price", sa.Numeric),
        sa.column("discount", sa.Numeric),
    )
    rows = bind.execute(sa.select(receipts.c.id, receipts.c.store_code, receipts.c.raw)).all()
    for receipt_id, store_code, raw_value in rows:
        raw = _raw(raw_value)
        if store_code == "dnipro-m":
            amount = float(raw.get("bonuses_debiting_amount") or 0)
            if amount:
                _add(bind, components, receipt_id, "discount", "bonuses_debiting_amount",
                     "Списані бонуси", -abs(amount), "dnipro_m.order",
                     {"bonuses_debiting_amount": raw.get("bonuses_debiting_amount")})
                bind.execute(sa.update(receipts).where(receipts.c.id == receipt_id).values(
                    total_adjustment=0, adjustment_reason=None))
        elif store_code == "eva":
            amount = float(raw.get("shipping_amount") or 0)
            if amount:
                _add(bind, components, receipt_id, "delivery", "shipping_amount", "Доставка",
                     amount, "eva.order", {"shipping_amount": raw.get("shipping_amount")})
                bind.execute(sa.update(receipts).where(receipts.c.id == receipt_id).values(
                    total_adjustment=0, adjustment_reason=None))
        elif store_code == "prom":
            cart = raw.get("cart") or {}
            raw_items = cart.get("cartItems") or []
            db_items = bind.execute(sa.select(items.c.id).where(items.c.receipt_id == receipt_id).order_by(items.c.id)).all()
            for db_row, raw_item in zip(db_items, raw_items):
                if not raw_item.get("isGift"):
                    continue
                base = float(raw_item.get("productBasePrice") or raw_item.get("productPrice") or 0)
                qty = float(raw_item.get("quantity") or 1)
                bind.execute(sa.update(items).where(items.c.id == db_row.id).values(
                    unit_price=0, total_price=0, discount=round(base * qty, 2)))
            bind.execute(sa.update(receipts).where(receipts.c.id == receipt_id).values(
                total_adjustment=0, adjustment_reason=None))


def downgrade() -> None:
    pass
