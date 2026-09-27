"""Repair historical METRO receipt lines from the stored invoice PDFs.

The original METRO parser skipped invoice rows that did not contain its old
separator format. This repair keeps receipt ids and linked ReceiptItem ids,
updates matching rows, appends missing rows, and re-runs shopping-list matching.
It deliberately keeps the stored order total: METRO invoice PDFs can show a
pre-discount total that differs from the charged order total.
"""
from __future__ import annotations

import base64
from collections import defaultdict
from sqlalchemy import select

from app.adapters.metro import _distribute_receipt_total, _invoice_items
from app.db import SessionLocal
from app.models.models import Receipt, ReceiptItem
from app.services.shopping_list import match_receipt


def _apply_item(row: ReceiptItem, item) -> None:
    row.name = item.name
    row.barcode = item.barcode
    row.category_raw = item.category
    row.quantity = item.quantity
    row.unit = item.unit
    row.unit_price = item.unit_price
    row.total_price = item.total_price
    row.discount = item.discount


def repair() -> dict[str, int]:
    repaired = skipped = added = updated = matched = 0
    with SessionLocal() as db:
        receipts = db.execute(
            select(Receipt).where(Receipt.store_code == "metro")
            .order_by(Receipt.purchased_at)
        ).scalars().all()
        for receipt in receipts:
            invoice = (receipt.raw or {}).get("invoice") if isinstance(receipt.raw, dict) else None
            encoded = invoice.get("invoice") if isinstance(invoice, dict) else None
            if not encoded:
                skipped += 1
                continue
            try:
                # _invoice_items performs the PDF line/summary validation when
                # the invoice carries a total and VAT summary.
                data = base64.b64decode(encoded)
                items = _invoice_items({**invoice, "invoice": base64.b64encode(data).decode()})
            except Exception:
                skipped += 1
                continue
            if not items:
                skipped += 1
                continue

            # Persisted METRO lines are grossed up to the charged order total;
            # the PDF's invoice total may be before order-level discounts.
            _distribute_receipt_total(items, receipt.total_amount)
            by_barcode = defaultdict(list)
            by_name = defaultdict(list)
            for row in receipt.items:
                if row.barcode:
                    by_barcode[row.barcode].append(row)
                by_name[row.name].append(row)

            changed = False
            for item in items:
                row = by_barcode[item.barcode].pop(0) if item.barcode and by_barcode[item.barcode] else None
                if row is None and by_name[item.name]:
                    row = by_name[item.name].pop(0)
                if row is None:
                    row = ReceiptItem(receipt=receipt)
                    db.add(row)
                    added += 1
                else:
                    updated += 1
                _apply_item(row, item)
                changed = True

            if changed:
                db.flush()
                matched += len(match_receipt(db, receipt, use_ai=False))
                repaired += 1
        db.commit()
    return {"receipts_repaired": repaired, "receipts_skipped": skipped,
            "lines_updated": updated, "lines_added": added,
            "shopping_items_matched": matched}


if __name__ == "__main__":
    print(repair())
