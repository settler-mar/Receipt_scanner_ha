"""Backfill signed AliExpress receipt adjustments from stored order data.

Revision ID: c6e8f0a2b4d6
Revises: b4c6d8e0f2a4
Create Date: 2026-09-27
"""
from __future__ import annotations

import json
from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c6e8f0a2b4d6"
down_revision: Union[str, Sequence[str], None] = "b4c6d8e0f2a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _number(value: Any) -> float:
    import re

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


def _has_detail_breakdown(value: Any) -> bool:
    if isinstance(value, dict):
        if isinstance(value.get("priceDetails"), list) and isinstance(value.get("totalPrice"), dict):
            return True
        return any(_has_detail_breakdown(child) for child in value.values())
    if isinstance(value, list):
        return any(_has_detail_breakdown(child) for child in value)
    return False


def upgrade() -> None:
    bind = op.get_bind()
    receipts = sa.table(
        "receipts",
        sa.column("id", sa.String),
        sa.column("store_code", sa.String),
        sa.column("total_amount", sa.Numeric),
        sa.column("raw", sa.JSON),
        sa.column("total_adjustment", sa.Numeric),
        sa.column("adjustment_reason", sa.String),
    )
    items = sa.table(
        "receipt_items",
        sa.column("receipt_id", sa.String),
        sa.column("total_price", sa.Numeric),
    )
    rows = bind.execute(
        sa.select(receipts.c.id, receipts.c.total_amount, receipts.c.raw)
        .where(receipts.c.store_code == "aliexpress")
    ).all()
    for receipt_id, total_amount, raw in rows:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                raw = {}
        line_sum = bind.execute(
            sa.select(sa.func.coalesce(sa.func.sum(items.c.total_price), 0))
            .where(items.c.receipt_id == receipt_id)
        ).scalar_one()
        adjustment = round(float(total_amount or 0) - float(line_sum or 0), 2)
        if abs(adjustment) < 0.01 or _has_detail_breakdown((raw or {}).get("_detail_responses")):
            adjustment = 0.0
            reason = None
        else:
            reason = "AliExpress: причина расхождения не определена по доступным данным заказа"
        bind.execute(
            sa.update(receipts).where(receipts.c.id == receipt_id).values(
                total_adjustment=adjustment,
                adjustment_reason=reason,
            )
        )


def downgrade() -> None:
    # The adjustment columns are owned by the preceding schema migration.
    pass
