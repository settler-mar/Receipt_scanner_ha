"""Backfill Fozzy order-level discounts and delivery components."""
from __future__ import annotations

import json
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f2b4d6e8a0c1"
down_revision: Union[str, Sequence[str], None] = "e1a4c6d8f0b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _raw(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def _component(bind, table, receipt_id, kind, code, label, amount, source, raw):
    if abs(amount) < 0.005:
        return
    bind.execute(table.insert().values(
        id=str(uuid.uuid4()), receipt_id=receipt_id, kind=kind, code=code,
        label=label, amount=round(amount, 2), currency="UAH", source=source, raw=raw,
    ))


def upgrade() -> None:
    bind = op.get_bind()
    receipts = sa.table(
        "receipts", sa.column("id", sa.String), sa.column("raw", sa.JSON),
        sa.column("total_amount", sa.Numeric), sa.column("total_adjustment", sa.Numeric),
        sa.column("adjustment_reason", sa.String), sa.column("total_discount", sa.Numeric),
    )
    items = sa.table("receipt_items", sa.column("receipt_id", sa.String), sa.column("total_price", sa.Numeric))
    components = sa.table(
        "receipt_components", sa.column("id", sa.String), sa.column("receipt_id", sa.String),
        sa.column("kind", sa.String), sa.column("code", sa.String), sa.column("label", sa.String),
        sa.column("amount", sa.Numeric), sa.column("currency", sa.String), sa.column("source", sa.String), sa.column("raw", sa.JSON),
    )
    rows = bind.execute(sa.select(receipts.c.id, receipts.c.raw, receipts.c.total_amount)).all()
    # Only Fozzy rows are selected through the raw payload shape in this
    # migration; store_code is intentionally not needed for the calculation.
    for receipt_id, raw_value, total_amount in rows:
        raw = _raw(raw_value)
        if not ({"header", "detail"} <= raw.keys() or {"head", "detail"} <= raw.keys()):
            continue
        existing = bind.execute(sa.select(sa.func.count()).select_from(components).where(components.c.receipt_id == receipt_id)).scalar_one()
        if existing:
            continue
        line_total = float(bind.execute(sa.select(sa.func.coalesce(sa.func.sum(items.c.total_price), 0)).where(items.c.receipt_id == receipt_id)).scalar_one() or 0)
        if "header" in raw:
            header = raw.get("header") or {}
            amount = round(float(total_amount or 0) - line_total, 2)
            if abs(amount) >= 0.01:
                _component(bind, components, receipt_id, "discount" if amount < 0 else "other",
                           "order_discount" if amount < 0 else "order_residual",
                           "Знижка замовлення" if amount < 0 else "Різниця підсумку замовлення",
                           amount, "fozzy.cheque_totals", {"sumReg": header.get("sumReg"), "sumDiscount": header.get("sumDiscount")})
            bind.execute(sa.update(receipts).where(receipts.c.id == receipt_id).values(
                total_adjustment=0, adjustment_reason=None,
                total_discount=abs(amount) if amount < 0 else 0,
            ))
        else:
            head = raw.get("head") or {}
            detail = raw.get("detail") or {}
            delivery = float(detail.get("sumActualDelivery") or 0)
            _component(bind, components, receipt_id, "delivery", "delivery", "Доставка", delivery,
                       "fozzy.order_totals", {"sumActualDelivery": detail.get("sumActualDelivery")})
            residual = round(float(total_amount or 0) - line_total - delivery, 2)
            _component(bind, components, receipt_id, "discount" if residual < 0 else "other",
                       "order_discount" if residual < 0 else "order_residual",
                       "Знижка замовлення" if residual < 0 else "Різниця підсумку замовлення", residual,
                       "fozzy.order_totals", {"sumActual": head.get("sumActual"), "sumActualDiscount": head.get("sumActualDiscount")})
            bind.execute(sa.update(receipts).where(receipts.c.id == receipt_id).values(
                total_adjustment=0, adjustment_reason=None,
                total_discount=abs(residual) if residual < 0 else 0,
            ))


def downgrade() -> None:
    pass
