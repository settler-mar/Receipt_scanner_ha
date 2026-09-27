"""Add flexible receipt-level amount components and backfill AliExpress details.

Revision ID: d7f1a3b5c9e2
Revises: c6e8f0a2b4d6
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d7f1a3b5c9e2"
down_revision: Union[str, Sequence[str], None] = "c6e8f0a2b4d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _number(value: Any) -> float:
    match = re.search(r"-?\d[\d\s.,]*", str(value or ""))
    if match is None:
        return 0.0
    text = match.group(0).replace(" ", "")
    if "," in text and "." in text:
        decimal = "," if text.rfind(",") > text.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        text = text.replace(thousands, "").replace(decimal, ".")
    else:
        text = text.replace(",", ".")
        if text.count(".") > 1:
            parts = text.split(".")
            text = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return float(text)
    except ValueError:
        return 0.0


def _kind(label: str) -> str:
    text = label.casefold()
    if any(x in text for x in ("shipping", "delivery", "достав")):
        return "delivery"
    if any(x in text for x in ("tax", "vat", "подат")):
        return "tax"
    if any(x in text for x in ("coupon", "discount", "coins", "зниж", "купон")):
        return "discount"
    if any(x in text for x in ("fee", "service", "коміс")):
        return "fee"
    if any(x in text for x in ("adjustment", "кориг")):
        return "adjustment"
    return "other"


def _details(value: Any) -> list[dict]:
    if isinstance(value, dict):
        if isinstance(value.get("priceDetails"), list):
            return [row for row in value["priceDetails"] if isinstance(row, dict)]
        for child in value.values():
            found = _details(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _details(child)
            if found:
                return found
    return []


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "receipt_components" not in inspector.get_table_names():
        op.create_table(
            "receipt_components",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("receipt_id", sa.String(), nullable=False),
            sa.Column("kind", sa.String(), nullable=False),
            sa.Column("code", sa.String(), nullable=True),
            sa.Column("label", sa.String(), nullable=False),
            sa.Column("amount", sa.Numeric(12, 2), nullable=False),
            sa.Column("currency", sa.String(), nullable=False, server_default="UAH"),
            sa.Column("source", sa.String(), nullable=True),
            sa.Column("raw", sa.JSON(), nullable=False, server_default="{}"),
            sa.ForeignKeyConstraint(["receipt_id"], ["receipts.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    existing_indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("receipt_components")}
    if "ix_receipt_components_receipt" not in existing_indexes:
        op.create_index("ix_receipt_components_receipt", "receipt_components", ["receipt_id"])
    if "ix_receipt_components_kind" not in existing_indexes:
        op.create_index("ix_receipt_components_kind", "receipt_components", ["kind"])

    bind = op.get_bind()
    receipts = sa.table(
        "receipts",
        sa.column("id", sa.String),
        sa.column("store_code", sa.String),
        sa.column("raw", sa.JSON),
        sa.column("total_amount", sa.Numeric),
        sa.column("total_adjustment", sa.Numeric),
        sa.column("adjustment_reason", sa.String),
        sa.column("total_discount", sa.Numeric),
    )
    items = sa.table(
        "receipt_items",
        sa.column("receipt_id", sa.String),
        sa.column("total_price", sa.Numeric),
    )
    components = sa.table(
        "receipt_components",
        sa.column("id", sa.String),
        sa.column("receipt_id", sa.String),
        sa.column("kind", sa.String),
        sa.column("code", sa.String),
        sa.column("label", sa.String),
        sa.column("amount", sa.Numeric),
        sa.column("currency", sa.String),
        sa.column("source", sa.String),
        sa.column("raw", sa.JSON),
    )
    rows = bind.execute(
        sa.select(receipts.c.id, receipts.c.raw)
        .where(receipts.c.store_code == "aliexpress")
    ).all()
    for receipt_id, raw in rows:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                raw = {}
        detail_rows = _details((raw or {}).get("_detail_responses"))
        parsed = []
        for row in detail_rows:
            label = str(row.get("title") or row.get("label") or row.get("name") or "Other charge").strip()
            if not label or label.casefold() == "subtotal":
                continue
            raw_value = row.get("value", row.get("amount", row.get("price")))
            amount = _number(raw_value)
            if str(raw_value or "").strip().startswith(("-", "−")):
                amount = -abs(amount)
            if abs(amount) < 0.005:
                continue
            code = re.sub(r"[^a-z0-9]+", "_", label.casefold()).strip("_") or None
            parsed.append({
                "id": str(uuid.uuid4()),
                "receipt_id": receipt_id,
                "kind": _kind(label),
                "code": code,
                "label": label,
                "amount": round(amount, 2),
                "currency": "UAH",
                "source": "aliexpress.order_detail",
                "raw": row,
            })
        if not parsed:
            continue
        bind.execute(components.insert(), parsed)
        component_sum = round(sum(float(row["amount"]) for row in parsed), 2)
        line_sum = bind.execute(
            sa.select(sa.func.coalesce(sa.func.sum(items.c.total_price), 0))
            .where(items.c.receipt_id == receipt_id)
        ).scalar_one()
        total = bind.execute(
            sa.select(receipts.c.total_amount).where(receipts.c.id == receipt_id)
        ).scalar_one()
        residual = round(float(total or 0) - float(line_sum or 0) - component_sum, 2)
        values = {"total_adjustment": residual}
        if abs(residual) < 0.01:
            values.update({"total_adjustment": 0, "adjustment_reason": None})
        else:
            values["adjustment_reason"] = "AliExpress: в деталях заказа остался нераспознанный компонент"
        values["total_discount"] = round(sum(abs(row["amount"]) for row in parsed if row["amount"] < 0), 2)
        bind.execute(sa.update(receipts).where(receipts.c.id == receipt_id).values(**values))


def downgrade() -> None:
    op.drop_index("ix_receipt_components_kind", table_name="receipt_components")
    op.drop_index("ix_receipt_components_receipt", table_name="receipt_components")
    op.drop_table("receipt_components")
