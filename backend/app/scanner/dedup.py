"""Cross-channel purchase deduplication, independent of classification.

The same purchase may arrive as a fiscal check, bank transaction and loyalty or
fuel-network operation.  Source IDs only dedupe *within* one channel.  This
module compares neutral payment evidence and returns a decision; it does not
assign categories and does not persist or merge records.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import re

from app.core.time import as_utc


@dataclasses.dataclass(frozen=True)
class PurchaseEvidence:
    source: str
    external_id: str | None
    occurred_at: dt.datetime | None
    amount: float | None
    currency: str = "UAH"
    brand: str | None = None
    merchant_edrpou: str | None = None
    card_last4: str | None = None
    rrn: str | None = None
    auth_code: str | None = None
    fuel_liters: float | None = None
    loyalty_ids: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class DedupResult:
    decision: str                 # same | probable | review | different
    score: int
    reasons: tuple[str, ...]

    @property
    def auto_merge(self) -> bool:
        return self.decision == "same"


def _digits(value: str | None) -> str | None:
    result = re.sub(r"\D", "", value or "")
    return result or None


def compare(a: PurchaseEvidence, b: PurchaseEvidence) -> DedupResult:
    """Score two channel records conservatively.

    RRN is treated as a transaction-level identifier. Otherwise an automatic
    merge requires exact amount, close time and one additional independent key
    (card, brand/entity, litres or loyalty ID). Ambiguous amount/time matches go
    to review instead of being silently merged.
    """
    score = 0
    reasons: list[str] = []

    if a.source == b.source and a.external_id and a.external_id == b.external_id:
        return DedupResult("same", 100, ("same source id",))
    if a.rrn and b.rrn and _digits(a.rrn) == _digits(b.rrn):
        return DedupResult("same", 100, ("same RRN",))

    exact_amount = (
        a.amount is not None and b.amount is not None
        and a.currency == b.currency and abs(a.amount - b.amount) <= 0.01
    )
    if exact_amount:
        score += 35
        reasons.append("exact amount")
    elif a.amount is not None and b.amount is not None:
        return DedupResult("different", 0, ("different amount",))

    minutes: float | None = None
    if a.occurred_at and b.occurred_at:
        minutes = abs((as_utc(a.occurred_at) - as_utc(b.occurred_at)).total_seconds()) / 60
        if minutes <= 10:
            score += 30
            reasons.append("time within 10m")
        elif minutes <= 48 * 60:
            score += 15
            reasons.append("time within 48h")
        else:
            return DedupResult("different", score, tuple(reasons + ["time too far apart"]))

    independent = False
    if a.card_last4 and b.card_last4 and _digits(a.card_last4) == _digits(b.card_last4):
        score += 25
        independent = True
        reasons.append("same card last4")
    if a.merchant_edrpou and b.merchant_edrpou and _digits(a.merchant_edrpou) == _digits(b.merchant_edrpou):
        score += 25
        independent = True
        reasons.append("same merchant entity")
    elif a.brand and b.brand and a.brand.casefold() == b.brand.casefold():
        score += 15
        independent = True
        reasons.append("same brand")
    if a.fuel_liters is not None and b.fuel_liters is not None and abs(a.fuel_liters - b.fuel_liters) <= 0.01:
        score += 25
        independent = True
        reasons.append("same fuel volume")
    if set(a.loyalty_ids) & set(b.loyalty_ids):
        score += 20
        independent = True
        reasons.append("same loyalty id")

    if exact_amount and minutes is not None and minutes <= 10 and independent:
        decision = "same"
    elif score >= 70:
        decision = "probable"
    elif exact_amount and minutes is not None and minutes <= 48 * 60:
        decision = "review"
    else:
        decision = "different"
    return DedupResult(decision, min(score, 100), tuple(reasons))


def from_fiscal_check(check, *, brand: str | None = None) -> PurchaseEvidence:
    """Build neutral evidence without importing classification/category logic."""
    keys = check.reconcile_keys()
    return PurchaseEvidence(
        source="fiscal_check",
        external_id=(f"{check.fiscal_number}:{check.document_id}"
                     if check.fiscal_number and check.document_id else None),
        occurred_at=keys.issued_at,
        amount=keys.total,
        brand=brand,
        merchant_edrpou=keys.seller_edrpou,
        card_last4=keys.card_last4,
        rrn=keys.rrn,
        auth_code=keys.auth_code,
        fuel_liters=keys.fuel_liters,
        loyalty_ids=tuple(keys.loyalty_ids),
    )
