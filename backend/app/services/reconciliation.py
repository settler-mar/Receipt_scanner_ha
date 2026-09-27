"""Cheap, persisted receipt reconciliation snapshots."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.models import Receipt, ReceiptComponent, ReceiptItem


def recalculate_receipt(db: Session, receipt: Receipt) -> None:
    """Refresh the denormalized totals for one receipt.

    This deliberately runs on writes/imports, never while listing receipts.
    The timestamp is UTC-naive because that is the database convention.
    """
    items_total = db.execute(
        select(func.coalesce(func.sum(ReceiptItem.total_price), 0)).where(
            ReceiptItem.receipt_id == receipt.id
        )
    ).scalar_one()
    components_total = db.execute(
        select(func.coalesce(func.sum(ReceiptComponent.amount), 0)).where(
            ReceiptComponent.receipt_id == receipt.id
        )
    ).scalar_one()
    items_total = round(float(items_total or 0), 2)
    components_total = round(float(components_total or 0), 2)
    reconciled_total = round(items_total + components_total + float(receipt.total_adjustment or 0), 2)
    delta = round(reconciled_total - float(receipt.total_amount or 0), 2)
    receipt.items_total = items_total
    receipt.components_total = components_total
    receipt.reconciled_total = reconciled_total
    receipt.reconciliation_delta = delta
    receipt.reconciliation_status = "open" if abs(delta) >= 0.01 else "ok"
    receipt.reconciliation_calculated_at = dt.datetime.utcnow()


def recalculate_receipts(db: Session, receipts: list[Receipt]) -> None:
    for receipt in receipts:
        recalculate_receipt(db, receipt)
