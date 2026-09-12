"""Immediate duplicate guard for the manual ingestion boundary only.

This is deliberately separate from the cross-channel duplicate worker.  It
protects Telegram/QR/XML/photo uploads and the manual form from repeated or
concurrent submissions, but never compares them to store-adapter receipts.
"""
from __future__ import annotations

import datetime as dt
import re
import threading
from contextlib import contextmanager

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, selectinload

from app.adapters.base import NormalizedReceipt
from app.core.time import KYIV, as_utc
from app.models.models import Receipt
from app.scanner import networks
from app.scanner.fiscal_qr import fiscal_identity

_LOCK = threading.RLock()

# How far apart two records of the same paper receipt may claim to have been
# bought. Generous because a misread hour is common: OCR reading 18:53 for
# 16:53 shifts the stored UTC time by two hours.
_WINDOW = dt.timedelta(days=2)


@contextmanager
def ingestion_lock():
    """Serialize the check-and-insert section in the single application process."""
    with _LOCK:
        yield


def _norm(value: str | None) -> str:
    return re.sub(r"[^\w]+", " ", (value or "").casefold(), flags=re.UNICODE).strip()


def _shop_key(name: str | None, address: str | None = None) -> str:
    network = networks.identify(f"{name or ''} {address or ''}")
    return f"network:{network.name.casefold()}" if network else _norm(f"{name or ''} {address or ''}")


def _item_signature(items) -> tuple[tuple[str, float, float], ...]:
    return tuple(sorted(
        (_norm(item.name), round(float(item.quantity or 1), 3), round(float(item.total_price), 2))
        for item in items if _norm(item.name)
    ))


def _is_manual_scope(receipt: Receipt) -> bool:
    raw = receipt.raw or {}
    return receipt.store_code == "scanner" or raw.get("ingest_scope") == "manual" or raw.get("source") == "manual"


def _ocr_text(receipt_or_raw) -> str:
    raw = receipt_or_raw if isinstance(receipt_or_raw, dict) else (receipt_or_raw.raw or {})
    return str(raw.get("ocr_excerpt") or "")


def _searchable(text: str) -> str:
    """OCR text with the noise that breaks literal matching taken out.

    Thermal-print OCR routinely splits numbers ("633, 50 грн") and varies
    spacing, so digits are glued back together and commas normalised before
    anything is looked up inside.
    """
    text = text.casefold().replace(",", ".")
    # "633. 50" / "633 . 50" -> "633.50", and "1 234.00" -> "1234.00".
    text = re.sub(r"(\d)[ \t ]*\.[ \t ]*(\d)", r"\1.\2", text)
    text = re.sub(r"(\d)[ \t ]+(\d{3})\b", r"\1\2", text)
    return re.sub(r"[ \t]+", " ", text)


def _same_amount(left, right) -> bool:
    """Money equality across the Numeric column's Decimal and a parser's float —
    `Decimal("7061.43") == 7061.43` is False, so neither side may be compared raw."""
    if left is None or right is None:
        return False
    return abs(float(left) - float(right)) < 0.005


def _amount_appears(text: str, amount: float | None) -> bool:
    if not amount:
        return False
    return re.search(rf"(?<!\d){re.escape(f'{float(amount):.2f}')}(?!\d)", text) is not None


def _date_appears(text: str, moment: dt.datetime | None) -> bool:
    if moment is None:
        return False
    local = as_utc(moment).astimezone(KYIV).date()
    return any(local.strftime(fmt) in text for fmt in ("%d.%m.%Y", "%d.%m.%y"))


def _shop_appears(text: str, name: str | None) -> bool:
    """True when a distinctive word of the shop name shows up in the text."""
    words = [w for w in _norm(name).split() if len(w) >= 5]
    return any(w in text for w in words)


def _same_paper_by_text(ocr_text: str, *, amount: float | None,
                        moment: dt.datetime | None, shop: str | None) -> bool:
    """Whether an OCR transcript is of the receipt described by the other side.

    A parse that failed still stored the receipt's raw text, and that text holds
    the printed total, date and shop of the very receipt a better parse later
    reports. Requiring all three to appear keeps an unrelated receipt — which
    would have to share the shop, the day and the exact printed total — out.
    """
    if len(ocr_text) < 40:
        return False
    text = _searchable(ocr_text)
    return (_amount_appears(text, amount)
            and _date_appears(text, moment)
            and _shop_appears(text, shop))


def find_receipt_by_fiscal(db: Session, fiscal) -> Receipt | None:
    """The receipt already filed for this ДПС document, if there is one.

    A decoded fiscal QR names the document itself, so this settles the question
    outright — no comparing totals or timestamps. The identity is looked for
    everywhere it can end up: as a receipt's `external_id` when the QR created
    it, in its stored ДПС link, and on any earlier scan of the same document
    (a receipt imported from XML carries no fiscal number of its own, but the
    scan that the import was started from does).
    """
    from app.models.scanner import ScanJob

    wanted = fiscal.dedup_key if fiscal else None
    if not wanted:
        return None

    receipt = db.execute(
        select(Receipt).where(Receipt.external_id == wanted)
    ).scalars().first()
    if receipt is not None and _is_manual_scope(receipt):
        return receipt

    for row in db.execute(select(Receipt).where(Receipt.receipt_url.is_not(None))).scalars():
        if _is_manual_scope(row) and fiscal_identity(row.receipt_url) == wanted:
            return row
        if _is_manual_scope(row) and fiscal_identity((row.raw or {}).get("fiscal_url")) == wanted:
            return row

    for job in db.execute(select(ScanJob).where(ScanJob.receipt_id.is_not(None))).scalars():
        if fiscal_identity((job.raw_ocr or {}).get("fiscal_url")) != wanted:
            continue
        earlier = db.get(Receipt, job.receipt_id)
        if earlier is not None and _is_manual_scope(earlier):
            return earlier
    return None


def find_duplicate(db: Session, incoming: NormalizedReceipt) -> Receipt | None:
    """Return an existing manual-scope receipt only for a conservative match."""
    incoming_time = as_utc(incoming.purchased_at)
    # Two records of one receipt often disagree on the total (a failed parse
    # invents one) and on the timestamp (OCR misreads an hour), so neither can
    # gate which rows are even looked at — only the pair of rules below decides.
    candidates = db.execute(
        select(Receipt)
        .options(selectinload(Receipt.items))
        .where(or_(
            and_(Receipt.currency == incoming.currency,
                 Receipt.total_amount == incoming.total_amount),
            and_(Receipt.purchased_at >= incoming_time - _WINDOW,
                 Receipt.purchased_at <= incoming_time + _WINDOW),
        ))
    ).scalars().all()
    incoming_items = _item_signature(incoming.items)
    incoming_shop = _shop_key(incoming.shop_name, incoming.shop_address)
    incoming_text = _ocr_text(incoming.raw or {})

    for receipt in candidates:
        if not _is_manual_scope(receipt):
            continue
        # Fiscal QR/link identity and image-content hashes are strong IDs.
        if incoming.external_id and receipt.external_id == incoming.external_id:
            return receipt

        existing_time = as_utc(receipt.purchased_at)
        seconds = abs((incoming_time - existing_time).total_seconds())
        same_total = (receipt.currency == incoming.currency
                      and _same_amount(receipt.total_amount, incoming.total_amount))
        if same_total:
            # Two different purchases landing on the same total *and* the same
            # second is not something that happens; when both agree that
            # precisely, neither the shop nor the basket needs to corroborate —
            # which matters because a weak parse often resolves no shop at all.
            if seconds <= 1:
                return receipt
            existing_shop = _shop_key(receipt.shop_name, receipt.shop_address)
            if seconds <= 120 and incoming_shop and incoming_shop == existing_shop:
                existing_items = _item_signature(receipt.items)
                # Repeated form submissions have the same basket.
                if incoming_items and existing_items and incoming_items == existing_items:
                    return receipt

        # Re-photographing a receipt whose first scan came out garbled produces
        # a row that agrees with the new one on nothing — not the total, not the
        # time, not even the shop, which the failed parse never resolved. Its
        # stored OCR transcript is the one thing that still describes the paper.
        if _same_paper_by_text(_ocr_text(receipt), amount=incoming.total_amount,
                               moment=incoming.purchased_at, shop=incoming.shop_name):
            return receipt
        if _same_paper_by_text(incoming_text, amount=receipt.total_amount,
                               moment=receipt.purchased_at, shop=receipt.shop_name):
            return receipt
    return None
