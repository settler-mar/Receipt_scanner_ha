"""Persistence + matching for bank statement feeds (Monobank first, more bank
adapters can follow the same BaseAdapter.fetch_transactions contract).

Mirrors services/collector.py's split for receipts: adapters are pure API
clients (adapters/monobank.py), this module owns turning their normalized
output into rows and reconciling them against Receipt.

Matching (v1, per the locked design) is 1:1 only: a transaction with exactly
one plausible receipt candidate is linked automatically, zero candidates is
left `unmatched`, and more than one is left `needs_review` for a person to
resolve by hand — no split/merge (group) matching yet.
"""
from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.base import AdapterError, NormalizedBankTransaction
from app.adapters.registry import get_adapter
from app.core.security import decrypt
from app.models.models import (
    Account,
    BankTransaction,
    BankTransactionStatus,
    CollectionRun,
    Receipt,
    RunStatus,
)

logger = logging.getLogger(__name__)

# Amount tolerance for matching (kopecks rounding, tips) and how far apart in
# time a transaction and a receipt may be and still be considered the same
# purchase (a fiscal receipt can be printed a little before/after the card
# authorization actually settles).
AMOUNT_TOLERANCE = Decimal("0.05")
MATCH_WINDOW = dt.timedelta(hours=24)
SHOP_NAME_FUZZY_THRESHOLD = 0.6

# Non-secret bookkeeping key in Credential.extra — when the account's last
# successful poll's window ended, so the next poll picks up right after it.
LAST_POLLED_UNTIL_KEY = "bank_last_polled_until"


def ingest_transactions(
    db: Session, account: Account, items: list[NormalizedBankTransaction]
) -> int:
    """Upsert normalized statement lines, keyed by (account, external_id).
    Returns how many were newly created."""
    if not items:
        return 0
    existing_ids = {
        row
        for row in db.execute(
            select(BankTransaction.external_id).where(
                BankTransaction.account_id == account.id,
                BankTransaction.external_id.in_([item.external_id for item in items]),
            )
        ).scalars()
    }
    created = 0
    for item in items:
        if item.external_id in existing_ids:
            continue
        db.add(
            BankTransaction(
                account_id=account.id,
                store_code=account.store.code,
                external_id=item.external_id,
                amount=abs(item.amount),
                currency=item.currency,
                mcc=item.mcc,
                description=item.description,
                counterparty_raw=item.counterparty,
                occurred_at=item.occurred_at,
                raw=item.raw,
                # Money coming in (a refund, a transfer in) never gets a
                # receipt issued for it — file it straight to ignored instead
                # of leaving it in the matching queue forever.
                status=(
                    BankTransactionStatus.ignored
                    if item.amount > 0
                    else BankTransactionStatus.unmatched
                ),
                ignore_reason="incoming_transfer" if item.amount > 0 else None,
            )
        )
        created += 1
    db.commit()
    return created


def _shop_name_ratio(description: str | None, shop_name: str | None) -> float:
    if not description or not shop_name:
        return 0.0
    return SequenceMatcher(None, description.casefold(), shop_name.casefold()).ratio()


def match_transaction(db: Session, txn: BankTransaction) -> None:
    """Look for receipts whose amount and purchase time plausibly correspond to
    this transaction. Exactly one candidate links it; zero leaves it
    unmatched; more than one is left for manual review."""
    txn_amount = Decimal(str(txn.amount))
    low = txn_amount - AMOUNT_TOLERANCE
    high = txn_amount + AMOUNT_TOLERANCE
    window_start = txn.occurred_at - MATCH_WINDOW
    window_end = txn.occurred_at + MATCH_WINDOW
    candidates = db.execute(
        select(Receipt).where(
            Receipt.total_amount.between(low, high),
            Receipt.purchased_at.between(window_start, window_end),
        )
    ).scalars().all()

    if not candidates:
        txn.status = BankTransactionStatus.unmatched
        txn.linked_receipt_id = None
    elif len(candidates) == 1:
        txn.status = BankTransactionStatus.matched
        txn.linked_receipt_id = candidates[0].id
    else:
        # A fuzzy match against the merchant name can still resolve the tie
        # when several receipts share an amount/time window (e.g. two
        # same-priced fuel fill-ups the same evening).
        scored = [(c, _shop_name_ratio(txn.description, c.shop_name)) for c in candidates]
        strong = [c for c, ratio in scored if ratio >= SHOP_NAME_FUZZY_THRESHOLD]
        if len(strong) == 1:
            txn.status = BankTransactionStatus.matched
            txn.linked_receipt_id = strong[0].id
        else:
            txn.status = BankTransactionStatus.needs_review
            txn.linked_receipt_id = None
    db.commit()


async def collect_transactions_for_account(
    db: Session, account: Account, *, since: dt.datetime | None = None, trigger: str = "manual"
) -> dict:
    """Fetch since `since` — or, when not given, this account's last successful
    poll cursor (or the last 24h on first run) — ingest, and match every
    unmatched transaction across the whole table (matching isn't scoped to one
    bank account — a receipt could plausibly come from any of them).

    An explicit `since` (a deep-history backfill) never moves the stored poll
    cursor backward: ingestion is idempotent on (account, external_id), so
    re-fetching an overlapping range is harmless, and the cursor still only
    ever advances to "now" at the end of a successful run.

    Logs a CollectionRun the same way collector.py's receipt collection does —
    the account page's "Історія запусків" table is generic over both, no
    separate bank-transaction run log needed. `receipts_found`/`receipts_new`
    hold fetched/created counts here; the field names are receipt-era but the
    table is just a per-account job log.
    """
    run = CollectionRun(account_id=account.id, trigger=trigger, status=RunStatus.running)
    db.add(run)
    db.commit()
    db.refresh(run)

    try:
        if account.credential is None or not account.credential.access_token_enc:
            raise AdapterError("Немає токена — спершу вставте токен для цього акаунта")
        adapter = get_adapter(account.store.code)
        access_token = decrypt(account.credential.access_token_enc)
        extra = dict(account.credential.extra or {})
        if since is None:
            since_raw = extra.get(LAST_POLLED_UNTIL_KEY)
            since = (
                dt.datetime.fromisoformat(since_raw)
                if since_raw
                else dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)
            )
        now = dt.datetime.now(dt.timezone.utc)

        def _on_progress(done: int, total: int) -> None:
            # A separate request polling GET /accounts/{id}/runs must see this
            # while the fetch is still mid-flight, so commit right away rather
            # than waiting for the run to finish.
            run.progress_current = done
            run.progress_total = total
            db.commit()

        try:
            items = await adapter.fetch_transactions(
                access_token, extra, since=since, on_progress=_on_progress
            )
        except AdapterError:
            raise
        except Exception as exc:  # noqa: BLE001 — surfaced to the UI, not a raw 500
            logger.warning("Transaction fetch failed for account %s", account.id, exc_info=True)
            raise AdapterError(str(exc) or exc.__class__.__name__) from exc
        created = ingest_transactions(db, account, items)
        # Rules first: a transaction a rule confidently files as "rent"/
        # "utilities"/etc. is filed straight to ignored and never enters the
        # receipt-matching pass below at all — it was never going to have a
        # receipt, and skipping it avoids a coincidental amount/time match.
        from app.services.bank_transaction_rules import apply_rules

        apply_rules(db)
        pending = db.execute(
            select(BankTransaction).where(BankTransaction.status == BankTransactionStatus.unmatched)
        ).scalars().all()
        for txn in pending:
            match_transaction(db, txn)

        extra[LAST_POLLED_UNTIL_KEY] = now.isoformat()
        account.credential.extra = extra
        run.status = RunStatus.success
        run.receipts_found = len(items)
        run.receipts_new = created
        run.finished_at = dt.datetime.now(dt.timezone.utc)
        db.commit()
        return {"fetched": len(items), "created": created, "matched_checked": len(pending)}
    except Exception as exc:
        db.rollback()
        run = db.get(CollectionRun, run.id)
        run.status = RunStatus.error
        run.error_message = str(exc) or exc.__class__.__name__
        run.finished_at = dt.datetime.now(dt.timezone.utc)
        db.commit()
        raise


def run_deep_sync_in_background(account_id: str, since: dt.datetime) -> None:
    """A wide date range means many 31-day/60-second-throttled Monobank
    requests — this can take minutes, so the API endpoint hands it off here
    (FastAPI BackgroundTasks) instead of holding the HTTP request open.
    Progress isn't streamed; the journal entry below and the transactions
    themselves (once ingested) are how the user finds out it's done."""
    import asyncio

    from app.db import SessionLocal
    from app.services import event_log

    with SessionLocal() as db:
        account = db.get(Account, account_id)
        if account is None:
            return
        try:
            result = asyncio.run(
                collect_transactions_for_account(db, account, since=since, trigger="manual_all")
            )
            event_log.record(
                db, source="bank_transactions",
                title=f"Глибока історія «{account.label}» завантажена",
                message=f"Отримано {result['fetched']}, нових {result['created']}.",
            )
        except Exception as exc:  # noqa: BLE001 — never let a background task crash silently
            db.rollback()
            logger.warning("Deep bank-transaction sync failed for account %s", account_id, exc_info=True)
            event_log.record(
                db, source="bank_transactions", level=event_log.ERROR,
                title=f"Глибока історія «{account.label}» не завантажилась",
                message=str(exc) or exc.__class__.__name__,
            )
