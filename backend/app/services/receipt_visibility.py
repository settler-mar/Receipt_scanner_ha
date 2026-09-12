from __future__ import annotations

from sqlalchemy import func

from app.models.models import Receipt


def visible_receipt_clause():
    return func.json_extract(Receipt.raw, "$.merged_into_receipt_id").is_(None)


def is_merged_source(receipt: Receipt) -> bool:
    return bool((receipt.raw or {}).get("merged_into_receipt_id"))
