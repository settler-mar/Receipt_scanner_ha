"""The confidence gate between pipeline stages (ROADMAP.md Крок 3 "Звірка").

A parsed receipt is trusted — and the pipeline stops — when its line items add
up to its printed total:

    Σ(item.total_price) - Σ(item.discount already netted?) ± rounding == receipt total

Per-line `total_price` in NormalizedItem is the amount actually charged for that
line (post-discount), matching every store adapter, so the check is simply
Σ(total_price) vs receipt total with a small tolerance for rounding and for a
receipt-level rounding line ("Округлення").
"""
from __future__ import annotations

import dataclasses

from app.adapters.base import NormalizedReceipt

# Absolute UAH tolerance + a relative component for big baskets.
ABS_TOLERANCE = 0.50
REL_TOLERANCE = 0.01


@dataclasses.dataclass
class Reconciliation:
    ok: bool
    items_sum: float
    total: float
    delta: float
    reason: str = ""


def reconcile(receipt: NormalizedReceipt) -> Reconciliation:
    items_sum = round(sum(it.total_price for it in receipt.items), 2)
    total = round(receipt.total_amount or 0.0, 2)
    delta = round(items_sum - total, 2)
    tol = ABS_TOLERANCE + REL_TOLERANCE * total

    if not receipt.items:
        return Reconciliation(False, items_sum, total, delta, "no line items parsed")
    if total <= 0:
        return Reconciliation(False, items_sum, total, delta, "no printed total")
    # A total the parser derived by summing the very items being checked proves
    # nothing — without an independently printed total there is no evidence the
    # OCR read the receipt correctly, so such a draft always goes to review.
    if (receipt.raw or {}).get("total_inferred"):
        return Reconciliation(False, items_sum, total, delta,
                              "total was summed from items, not read from the receipt")
    if abs(delta) > tol:
        return Reconciliation(False, items_sum, total, delta,
                              f"items {items_sum} vs total {total} (Δ {delta})")
    return Reconciliation(True, items_sum, total, delta, "balanced")
