"""Repair confirmed Fora line totals and Rozetka delivery components."""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e1a4c6d8f0b2"
down_revision: Union[str, Sequence[str], None] = "d7f1a3b5c9e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[^0-9,.-]", "", str(value or "")).replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return default


def _quantity(value: Any, default: float = 1.0) -> float:
    match = re.search(r"\d+(?:[.,]\d+)?", str(value or ""))
    return _number(match.group(0), default) if match else default


def _raw(value: Any) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def _component(bind, components, receipt_id, *, kind, code, label, amount, source, evidence):
    if abs(amount) < 0.005:
        return
    bind.execute(components.insert().values(
        id=str(uuid.uuid4()), receipt_id=receipt_id, kind=kind, code=code,
        label=label, amount=round(amount, 2), currency="UAH", source=source, raw=evidence,
    ))


def upgrade() -> None:
    bind = op.get_bind()
    receipts = sa.table(
        "receipts", sa.column("id", sa.String), sa.column("store_code", sa.String),
        sa.column("raw", sa.JSON), sa.column("total_amount", sa.Numeric),
        sa.column("total_adjustment", sa.Numeric), sa.column("adjustment_reason", sa.String),
        sa.column("total_discount", sa.Numeric),
    )
    items = sa.table(
        "receipt_items", sa.column("id", sa.String), sa.column("receipt_id", sa.String),
        sa.column("name", sa.String), sa.column("quantity", sa.Numeric),
        sa.column("unit_price", sa.Numeric), sa.column("total_price", sa.Numeric),
    )
    components = sa.table(
        "receipt_components", sa.column("id", sa.String), sa.column("receipt_id", sa.String),
        sa.column("kind", sa.String), sa.column("code", sa.String), sa.column("label", sa.String),
        sa.column("amount", sa.Numeric), sa.column("currency", sa.String),
        sa.column("source", sa.String), sa.column("raw", sa.JSON),
    )
    rows = bind.execute(sa.select(
        receipts.c.id, receipts.c.store_code, receipts.c.raw, receipts.c.total_amount,
    ).where(receipts.c.store_code.in_(["fora", "rozetka"]))).all()
    for receipt_id, store_code, raw_value, total_amount in rows:
        raw = _raw(raw_value)
        already = bind.execute(
            sa.select(sa.func.count()).select_from(components).where(components.c.receipt_id == receipt_id)
        ).scalar_one()
        if already:
            continue
        line_rows = bind.execute(sa.select(
            items.c.id, items.c.name, items.c.quantity, items.c.total_price,
        ).where(items.c.receipt_id == receipt_id)).all()
        line_total = sum(float(row.total_price or 0) for row in line_rows)
        discount_total = 0.0

        if store_code == "fora":
            detail = raw.get("detail") or {}
            detail_items = detail.get("items") if isinstance(detail.get("items"), list) else []
            payment = detail.get("paymentDetail") or {}
            used_ids = set()
            if detail_items:
                for source_item in detail_items:
                    name = str(source_item.get("lagerName") or source_item.get("name") or "")
                    target = next((row for row in line_rows if row.id not in used_ids and row.name == name), None)
                    if target is None:
                        target = next((row for row in line_rows if row.id not in used_ids), None)
                    if target is None:
                        continue
                    used_ids.add(target.id)
                    quantity = _quantity(source_item.get("count") or source_item.get("lagerCountString"), 1) or 1
                    total_line = round(_number(source_item.get("priceOut")), 2)
                    bind.execute(sa.update(items).where(items.c.id == target.id).values(
                        quantity=quantity, unit_price=round(total_line / quantity, 2), total_price=total_line,
                    ))
                line_total = sum(
                    _number(source_item.get("priceOut"))
                    for source_item in detail_items
                    if isinstance(source_item, dict)
                )
                discount = _number(payment.get("discount"))
                if discount:
                    _component(bind, components, receipt_id, kind="discount", code="receipt_discount",
                               label="Знижка чека", amount=-abs(discount), source="fora.payment_detail",
                               evidence={"discount": payment.get("discount")})
                    discount_total += abs(discount)
                delivery = payment.get("deliveryPayment")
                delivery_amount = _number(delivery.get("amount", delivery.get("value"))) if isinstance(delivery, dict) else _number(delivery)
                if delivery_amount:
                    _component(bind, components, receipt_id, kind="delivery", code="delivery",
                               label="Доставка", amount=delivery_amount, source="fora.payment_detail",
                               evidence={"deliveryPayment": delivery})
                residual = round(float(total_amount or 0) - line_total - (-abs(discount)) - delivery_amount, 2)
                reason = None
            else:
                residual = round(float(total_amount or 0), 2)
                reason = "Фора: API повернув чек без товарних рядків"
            bind.execute(sa.update(receipts).where(receipts.c.id == receipt_id).values(
                total_adjustment=residual if abs(residual) >= 0.01 else 0,
                adjustment_reason=reason,
                total_discount=round(discount_total, 2),
            ))
        else:
            detail = raw.get("order_detailed") or {}
            delivery = detail.get("order_delivery_info") or {}
            delivery_amount = _number(delivery.get("cost_with_discount", delivery.get("cost")))
            if delivery_amount:
                _component(bind, components, receipt_id, kind="delivery", code="delivery",
                           label="Доставка", amount=delivery_amount, source="rozetka.order_delivery_info",
                           evidence={"delivery": delivery})
            residual = round(float(total_amount or 0) - line_total - delivery_amount, 2)
            bind.execute(sa.update(receipts).where(receipts.c.id == receipt_id).values(
                total_adjustment=residual if abs(residual) >= 0.01 else 0,
                adjustment_reason=("Rozetka.ua: залишок не пояснений доступними даними замовлення" if abs(residual) >= 0.01 else None),
            ))


def downgrade() -> None:
    # Data backfill is intentionally not destructive.
    pass
