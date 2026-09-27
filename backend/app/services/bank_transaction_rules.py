"""Auto-categorization rules for bank transactions — mainly person-to-person
transfers, which have no MCC/merchant to classify by, only a counterparty
name and a pattern in how often/when they recur (rent vs. utilities to the
same landlord, an allowance to a child, support sent to a parent).

A rule matches transactions by a counterparty substring (against
`description`, where Monobank puts the other person's name for P2P
transfers) and carries an ordered list of branches; each branch pairs a
condition with a result (category and/or ignore_reason). Conditions can see
the transaction's amount and its position/count among the *same rule's*
matches within the same calendar month — e.g.:

    Rule "Женя":
      1. count_in_month == 1   -> ignore_reason "utilities"   (only payment that month)
      2. position_in_month == 1 -> ignore_reason "rent"        (first payment)
      3. always                 -> ignore_reason "utilities"   (everything after)

`apply_rules` re-evaluates every eligible transaction on every call (not just
newly ingested ones) because a later transaction in the same month can change
what an earlier one should have been classified as (a second transfer arrives
-> the first one is no longer "only one this month"). It never touches a
transaction a person classified by hand — see `auto_rule_id` on BankTransaction.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.time import as_utc
from app.models.models import BankTransaction, BankTransactionRule, BankTransactionStatus


def _condition_matches(condition: dict, *, position: int, count: int, amount: float) -> bool:
    kind = condition.get("kind")
    if kind == "always":
        return True
    if kind == "position_in_month":
        return position == condition.get("position")
    if kind == "count_in_month":
        return count == condition.get("count")
    if kind == "amount_gte":
        return amount >= float(condition.get("amount", 0))
    if kind == "amount_lte":
        return amount <= float(condition.get("amount", 0))
    if kind == "amount_between":
        low = float(condition.get("min", 0))
        high = float(condition.get("max", float("inf")))
        return low <= amount <= high
    return False


def _pick_branch(branches: list[dict], **kwargs: Any) -> dict | None:
    for branch in branches:
        if _condition_matches(branch.get("condition") or {}, **kwargs):
            return branch
    return None


def apply_rules(db: Session) -> int:
    """Re-run every rule against its matching transactions. Returns how many
    transactions changed."""
    rules = db.execute(
        select(BankTransactionRule).order_by(BankTransactionRule.priority)
    ).scalars().all()
    updated = 0

    for rule in rules:
        pattern = (rule.counterparty_match or "").strip().casefold()
        if not pattern or not rule.branches:
            continue
        # SQLite's LOWER()/LIKE only case-fold ASCII, so a Cyrillic pattern
        # (the normal case — Monobank puts the counterparty's name in
        # `description` for P2P transfers) would silently never match at the
        # SQL level. Filter in Python instead, where .casefold() is correct.
        fair_game = db.execute(
            select(BankTransaction).where(
                or_(
                    BankTransaction.auto_rule_id == rule.id,
                    (BankTransaction.category_override_id.is_(None) & BankTransaction.ignore_reason.is_(None)),
                ),
            )
        ).scalars().all()
        candidates = [
            txn for txn in fair_game
            if txn.description and pattern in txn.description.casefold()
        ]

        by_month: dict[tuple[int, int], list[BankTransaction]] = defaultdict(list)
        for txn in candidates:
            occurred = as_utc(txn.occurred_at)
            by_month[(occurred.year, occurred.month)].append(txn)

        for group in by_month.values():
            group.sort(key=lambda t: as_utc(t.occurred_at))
            count = len(group)
            for position, txn in enumerate(group, start=1):
                branch = _pick_branch(
                    rule.branches, position=position, count=count, amount=float(txn.amount)
                )
                if branch is None:
                    continue
                new_category = branch.get("category_id") or None
                new_reason = branch.get("ignore_reason") or None
                changed = (
                    txn.category_override_id != new_category
                    or txn.ignore_reason != new_reason
                    or txn.auto_rule_id != rule.id
                )
                if not changed:
                    continue
                txn.category_override_id = new_category
                txn.ignore_reason = new_reason
                txn.auto_rule_id = rule.id
                if new_reason and txn.status != BankTransactionStatus.ignored:
                    txn.status = BankTransactionStatus.ignored
                    txn.linked_receipt_id = None
                updated += 1

    db.commit()
    return updated
