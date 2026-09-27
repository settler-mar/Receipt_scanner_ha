"""Make the last legacy provider totals explicit and auditable."""
from __future__ import annotations

import json
import re
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d6f8a0c2e4b5"
down_revision: Union[str, Sequence[str], None] = "c5e7f9b1d3a4"
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
    raw = json.loads(json.dumps(raw, default=str))
    bind.execute(table.insert().values(
        id=str(uuid.uuid4()), receipt_id=receipt_id, kind=kind, code=code,
        label=label, amount=round(amount, 2), currency="UAH", source=source, raw=raw,
    ))


def _amount(text: str, label: str) -> float | None:
    start = re.search(rf"\b{re.escape(label)}\b", text, re.I)
    match = re.search(r"-?\d[\d\s\xa0]*(?:[,.]\d+)?", text[start.end():start.end() + 80]) if start else None
    if not match:
        return None
    return float(match.group(0).replace(" ", "").replace("\xa0", "").replace(",", "."))


def upgrade() -> None:
    bind = op.get_bind()
    receipts = sa.table(
        "receipts", sa.column("id", sa.String), sa.column("store_code", sa.String),
        sa.column("external_id", sa.String), sa.column("raw", sa.JSON), sa.column("total_amount", sa.Numeric),
        sa.column("total_adjustment", sa.Numeric), sa.column("adjustment_reason", sa.String),
    )
    items = sa.table(
        "receipt_items", sa.column("id", sa.String), sa.column("receipt_id", sa.String),
        sa.column("name", sa.String), sa.column("total_price", sa.Numeric),
    )
    components = sa.table(
        "receipt_components", sa.column("id", sa.String), sa.column("receipt_id", sa.String),
        sa.column("kind", sa.String), sa.column("code", sa.String), sa.column("label", sa.String),
        sa.column("amount", sa.Numeric), sa.column("currency", sa.String), sa.column("source", sa.String), sa.column("raw", sa.JSON),
    )
    rows = bind.execute(sa.select(receipts.c.id, receipts.c.store_code, receipts.c.external_id,
                                  receipts.c.raw, receipts.c.total_amount)).all()
    for receipt_id, store_code, external_id, raw_value, total_amount in rows:
        raw = _raw(raw_value)
        line_total = float(bind.execute(sa.select(sa.func.coalesce(sa.func.sum(items.c.total_price), 0)).where(items.c.receipt_id == receipt_id)).scalar_one() or 0)
        if store_code == "foxtrot":
            markup = str(raw.get("html") or "")
            text = re.sub(r"<[^>]+>", " ", markup)
            text = re.sub(r"\s+", " ", text)
            delivery = _amount(text, "Доставка")
            used = _amount(text, "Використано бонусів")
            if delivery:
                _add(bind, components, receipt_id, "delivery", "delivery", "Доставка", delivery,
                     "foxtrot.order_totals", {"delivery": delivery})
            if used:
                _add(bind, components, receipt_id, "discount", "bonuses_used", "Використані бонуси", -abs(used),
                     "foxtrot.order_totals", {"used_bonuses": used})
            known = (delivery or 0) - abs(used or 0)
            residual = round(float(total_amount or 0) - line_total - known, 2)
            _add(bind, components, receipt_id, "discount" if residual < 0 else "other", "order_residual",
                 "Різниця підсумку замовлення", residual, "foxtrot.order_totals",
                 {"total": total_amount, "items_total": line_total, "delivery": delivery, "used_bonuses": used})
        elif store_code == "epicentrk":
            goods = raw.get("goods") or []
            # A historical offline response duplicated an identical good row.
            seen = set()
            for item_id, name, price in bind.execute(sa.select(items.c.id, items.c.name, items.c.total_price).where(items.c.receipt_id == receipt_id)).all():
                key = (name, round(float(price or 0), 2))
                if key in seen and any(str(g.get("name") or "").strip() == str(name or "").strip() for g in goods):
                    bind.execute(sa.delete(items).where(items.c.id == item_id))
                else:
                    seen.add(key)
            line_total = float(bind.execute(sa.select(sa.func.coalesce(sa.func.sum(items.c.total_price), 0)).where(items.c.receipt_id == receipt_id)).scalar_one() or 0)
            residual = round(float(total_amount or 0) - line_total, 2)
            _add(bind, components, receipt_id, "other", "order_residual", "Різниця підсумку замовлення", residual,
                 "epicentrk.order", {"price": raw.get("price"), "items_total": line_total})
        elif store_code in {"tabletki", "liki", "metro", "vencon"}:
            # These legacy rows deliberately retained only the provider total
            # (or a PDF pointer), not product lines. Preserve the difference as
            # a receipt-level correction instead of inventing products.
            residual = round(float(total_amount or 0) - line_total, 2)
            if abs(residual) >= 0.01:
                _add(bind, components, receipt_id, "other", "source_missing_lines",
                     "Коригування: джерело не повернуло товарні рядки", residual,
                     f"{store_code}.raw", {"external_id": external_id, "raw_keys": sorted(raw)})


def downgrade() -> None:
    pass
