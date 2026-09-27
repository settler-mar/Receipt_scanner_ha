from __future__ import annotations

import datetime as dt
import hashlib
import re
import shutil
import threading
import unicodedata
from collections import defaultdict
from decimal import Decimal
from difflib import SequenceMatcher
from itertools import combinations
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.time import as_utc
from app.core.config import BASE_DIR
from app.models.models import (
    DuplicateCandidate,
    DuplicateGroup,
    DuplicateRun,
    DuplicateRunStatus,
    DuplicateStatus,
    Receipt,
    ReceiptItem,
    ReceiptTag,
)

_LOCK = threading.Lock()
PHOTO_DIR = BASE_DIR / "data" / "photos"
_CONFUSABLES = str.maketrans({
    "A": "А", "a": "а",
    "B": "В", "b": "в",
    "C": "С", "c": "с",
    "E": "Е", "e": "е",
    "H": "Н", "h": "н",
    "I": "І", "i": "і",
    "K": "К", "k": "к",
    "M": "М", "m": "м",
    "O": "О", "o": "о",
    "P": "Р", "p": "р",
    "T": "Т", "t": "т",
    "X": "Х", "x": "х",
    "Y": "У", "y": "у",
})


class DuplicateRunAlreadyRunning(RuntimeError):
    pass


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _digits(value: Any) -> str | None:
    out = re.sub(r"\D", "", str(value or ""))
    return out or None


def _norm(value: Any) -> str | None:
    s = re.sub(r"\s+", " ", str(value or "").casefold()).strip()
    return s or None


def _compare_key(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        value = as_utc(value).isoformat()
    if isinstance(value, (list, tuple)):
        value = " ".join(str(v) for v in value)
    text = unicodedata.normalize("NFKC", str(value)).translate(_CONFUSABLES).casefold()
    return "".join(ch for ch in text if ch.isalnum())


def _money(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _amount_cents(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _fingerprint(receipt_ids: list[str]) -> str:
    material = "|".join(sorted(receipt_ids))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _raw_values(raw: dict, *keys: str) -> list[Any]:
    values: list[Any] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k.casefold() in keys and v not in (None, "", []):
                    values.append(v)
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(raw or {})
    return values


def _first_raw(raw: dict, *keys: str) -> str | None:
    keys_cf = tuple(k.casefold() for k in keys)
    for value in _raw_values(raw, *keys_cf):
        if isinstance(value, (str, int, float)):
            return str(value)
    return None


def _fiscal_key(receipt: Receipt) -> str | None:
    external = receipt.external_id or ""
    if external.startswith("fiscal:"):
        return external
    raw = receipt.raw or {}
    url = str(raw.get("fiscal_url") or raw.get("receipt_url") or receipt.receipt_url or "")
    fn = _first_raw(raw, "fn", "fiscal_number", "fiscalNumber")
    doc = _first_raw(raw, "id", "document_id", "documentId")
    if not fn:
        m = re.search(r"[?&]fn=([^&]+)", url)
        fn = m.group(1) if m else None
    if not doc:
        m = re.search(r"[?&](?:id|nfd)=([^&]+)", url)
        doc = m.group(1) if m else None
    if fn and doc:
        return f"fiscal:{fn}:{doc}"
    return None


def _receipt_evidence(receipt: Receipt) -> dict[str, Any]:
    raw = receipt.raw or {}
    item_names = [_norm(i.name) for i in receipt.items if _norm(i.name)]
    total_qty_by_unit = defaultdict(float)
    fuel_volume = None
    for item in receipt.items:
        unit = _norm(item.unit) or ""
        qty = _money(item.quantity) or 0
        total_qty_by_unit[unit] += qty
        if unit in {"л", "l", "литр", "літр"}:
            fuel_volume = (fuel_volume or 0) + qty
    return {
        "source": f"{receipt.store_code}:{receipt.channel}",
        "external_id": receipt.external_id,
        "fiscal_key": _fiscal_key(receipt),
        "rrn": _digits(_first_raw(raw, "rrn", "payment_rrn", "terminal_rrn")),
        "card_last4": (_digits(receipt.payment_type) or _digits(_first_raw(raw, "card_last4", "cardLast4", "pan"))) and (_digits(receipt.payment_type) or _digits(_first_raw(raw, "card_last4", "cardLast4", "pan")))[-4:],
        "seller_id": _digits(_first_raw(raw, "edrpou", "seller_edrpou", "sellerEdrpou", "tin", "tax_number")),
        "network": _norm(receipt.store_code) or _norm(receipt.shop_name),
        "shop_name": _norm(receipt.shop_name),
        "shop_address": _norm(receipt.shop_address),
        "bonus_card": _digits(_first_raw(raw, "bonus_card", "loyalty_card", "card_number", "loyaltyId")),
        "items": sorted(set(item_names)),
        "item_count": len(receipt.items),
        "fuel_volume": fuel_volume,
        "unit_quantities": dict(total_qty_by_unit),
    }


def _is_pharmacy_preorder(receipt: Receipt) -> bool:
    """Site reservation whose real purchase is later represented by a fiscal
    receipt from the pharmacy till.

    Keep the fallback for rows saved before the explicit source marker was
    added: Liki online orders have numeric order IDs and are never fiscal IDs.
    """
    raw = receipt.raw or {}
    if raw.get("source") == "pharmacy_preorder":
        return True
    if receipt.store_code in {"liki", "tabletki"} and raw.get("reservation_type"):
        return True
    return (
        receipt.store_code == "liki"
        and receipt.channel == "online"
        and bool(receipt.external_id)
        and not receipt.external_id.startswith("fiscal:")
    )


def _item_similarity(a: Receipt, b: Receipt) -> float:
    names_a = [_norm(i.name) for i in a.items if _norm(i.name)]
    names_b = [_norm(i.name) for i in b.items if _norm(i.name)]
    if not names_a or not names_b:
        return 0
    set_a, set_b = set(names_a), set(names_b)
    overlap = len(set_a & set_b) / max(len(set_a | set_b), 1)
    text_ratio = SequenceMatcher(None, " ".join(sorted(set_a)), " ".join(sorted(set_b))).ratio()
    similarity = max(overlap, text_ratio * 0.8)

    # Adapter and fiscal OCR names often abbreviate the same product
    # differently ("сусп."/"суспензія", "фл."/"флакон"). Matching the
    # line price first gives the name comparison the right pair to compare and
    # avoids treating a reordered basket as unrelated.
    used_b: set[int] = set()
    price_matched: list[float] = []
    for item_a in [i for i in a.items if _norm(i.name)]:
        choices = [
            (idx, item_b) for idx, item_b in enumerate(b.items)
            if idx not in used_b
            and _norm(item_b.name)
            and abs(float(item_a.total_price) - float(item_b.total_price)) <= 0.01
            and abs(float(item_a.quantity or 1) - float(item_b.quantity or 1)) <= 0.01
        ]
        if not choices:
            continue
        idx, item_b = max(
            choices,
            key=lambda pair: SequenceMatcher(
                None, _norm(item_a.name), _norm(pair[1].name)
            ).ratio(),
        )
        used_b.add(idx)
        price_matched.append(SequenceMatcher(
            None, _norm(item_a.name), _norm(item_b.name)
        ).ratio())
    common_words = 0
    for item_a in [i for i in a.items if _norm(i.name)]:
        choices = [
            item_b for item_b in b.items
            if abs(float(item_a.total_price) - float(item_b.total_price)) <= 0.01
            and abs(float(item_a.quantity or 1) - float(item_b.quantity or 1)) <= 0.01
        ]
        if not choices:
            continue
        item_b = max(choices, key=lambda item: SequenceMatcher(
            None, _norm(item_a.name), _norm(item.name)
        ).ratio())
        words_a = {word for word in re.findall(r"\w+", _norm(item_a.name)) if len(word) >= 4 and not word.isdigit()}
        words_b = {word for word in re.findall(r"\w+", _norm(item_b.name)) if len(word) >= 4 and not word.isdigit()}
        common_words += len(words_a & words_b)
    if price_matched and len(price_matched) == min(len(names_a), len(names_b)) and common_words >= 3:
        similarity = max(similarity, sum(price_matched) / len(price_matched))
    return similarity


def _score_pair(a: Receipt, b: Receipt) -> tuple[int, list[str], str, dict[str, Any]]:
    ea, eb = _receipt_evidence(a), _receipt_evidence(b)
    score = 0
    reasons: list[str] = []
    strong = False
    independent = 0

    base_reasons = ["exact amount", "time within 10m"]
    if ea["fiscal_key"] and ea["fiscal_key"] == eb["fiscal_key"]:
        return 100, base_reasons + ["same fiscal fn+id"], "pending", {"a": ea, "b": eb}
    if ea["rrn"] and ea["rrn"] == eb["rrn"]:
        return 100, base_reasons + ["same RRN"], "pending", {"a": ea, "b": eb}

    exact_amount = _amount_cents(a.total_amount) == _amount_cents(b.total_amount) and a.currency == b.currency
    if exact_amount:
        score += 30
        reasons.append("exact amount")

    minutes = abs((as_utc(a.purchased_at) - as_utc(b.purchased_at)).total_seconds()) / 60
    if minutes <= 10:
        score += 30
        reasons.append("time within 10m")

    for key, weight, reason in (
        ("card_last4", 20, "same card last4"),
        ("seller_id", 25, "same seller tax id"),
        ("network", 15, "same recognized network"),
        ("bonus_card", 20, "same loyalty card"),
    ):
        if ea[key] and ea[key] == eb[key]:
            score += weight
            independent += 1
            reasons.append(reason)

    if ea["shop_address"] and eb["shop_address"]:
        ratio = SequenceMatcher(None, ea["shop_address"], eb["shop_address"]).ratio()
        if ratio >= 0.82:
            score += 15
            independent += 1
            reasons.append("similar shop address")

    item_ratio = _item_similarity(a, b)
    if item_ratio >= 0.65:
        score += 25
        independent += 1
        reasons.append("similar item composition")
    elif item_ratio >= 0.35:
        score += 10
        reasons.append("partially similar item composition")

    if ea["fuel_volume"] is not None and eb["fuel_volume"] is not None and abs(ea["fuel_volume"] - eb["fuel_volume"]) <= 0.01:
        score += 25
        independent += 1
        reasons.append("same fuel volume")

    if ea["fiscal_key"] or eb["fiscal_key"] or ea["rrn"] or eb["rrn"]:
        strong = True

    if score >= 85 and (strong or (exact_amount and minutes <= 10 and independent >= 1)):
        status = "pending"
    elif score >= 60 and exact_amount and minutes <= 10:
        status = "needs_review"
    elif (score >= 50 and exact_amount and minutes <= 48 * 60
          and item_ratio >= 0.65):
        status = "needs_review"
    else:
        status = "rejected"
    return min(score, 100), reasons, status, {"a": ea, "b": eb, "minutes": minutes, "item_similarity": item_ratio}


def _candidate_window(db: Session) -> list[Receipt]:
    return db.execute(
        select(Receipt)
        .options(selectinload(Receipt.items), selectinload(Receipt.tags), selectinload(Receipt.account))
        .order_by(Receipt.purchased_at.desc())
    ).scalars().all()


def start_duplicate_search(db: Session, *, trigger: str = "manual") -> DuplicateRun:
    if not _LOCK.acquire(blocking=False):
        raise DuplicateRunAlreadyRunning("Duplicate search is already running")
    run = DuplicateRun(trigger=trigger, status=DuplicateRunStatus.running)
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _execute_duplicate_search(db: Session, run: DuplicateRun) -> DuplicateRun:
    try:
        receipts = [r for r in _candidate_window(db) if not (r.raw or {}).get("merged_into_receipt_id")]
        run.scanned_receipts = len(receipts)
        found = 0
        new = 0
        # Amount and currency are mandatory duplicate signals below.  Bucket
        # first so the common case does not compare every receipt with every
        # other receipt (the old implementation was O(n²) over the whole DB).
        buckets: dict[tuple[str, Decimal], list[Receipt]] = defaultdict(list)
        for receipt in receipts:
            buckets[(receipt.currency or "UAH", _amount_cents(receipt.total_amount))].append(receipt)
        # Keep the identity lookups in memory for this run.  The old loop did
        # two SELECTs for every qualifying pair, which made a duplicate scan
        # increasingly database-bound after the amount bucketing above.
        group_by_fingerprint = {
            row.fingerprint: row
            for row in db.execute(select(DuplicateGroup)).scalars()
        }
        candidate_by_fingerprint = {
            row.fingerprint: row
            for row in db.execute(select(DuplicateCandidate)).scalars()
        }
        for bucket in buckets.values():
            for a, b in combinations(bucket, 2):
                if a.id == b.id or a.store_code == b.store_code:
                    continue
                if _amount_cents(a.total_amount) != _amount_cents(b.total_amount):
                    continue
                minutes_apart = abs((as_utc(a.purchased_at) - as_utc(b.purchased_at)).total_seconds()) / 60
                # Pharmacy websites record the reservation time, while the
                # fiscal scan records collection/payment time. Only this
                # explicitly identified preorder-vs-receipt pair gets the
                # pharmacy's 48-hour reconciliation window.
                preorder_pair = _is_pharmacy_preorder(a) != _is_pharmacy_preorder(b)
                if minutes_apart > 10 and (not preorder_pair or minutes_apart > 48 * 60):
                    continue
                if minutes_apart > 10 and _item_similarity(a, b) < 0.65:
                    continue
                score, reasons, status, evidence = _score_pair(a, b)
                if status == "rejected" and score < 60:
                    continue
                found += 1
                fp = _fingerprint([a.id, b.id])
                group = group_by_fingerprint.get(fp)
                if group is None:
                    group = DuplicateGroup(
                        fingerprint=fp,
                        status=DuplicateStatus(status),
                        score=score,
                        reasons=reasons,
                        receipt_ids=sorted([a.id, b.id]),
                        created_by_run_id=run.id,
                        last_seen_run_id=run.id,
                    )
                    db.add(group)
                    db.flush()
                    group_by_fingerprint[fp] = group
                    new += 1
                elif group.status in {DuplicateStatus.pending, DuplicateStatus.needs_review}:
                    group.status = DuplicateStatus(status)
                    group.score = score
                    group.reasons = reasons
                    group.last_seen_run_id = run.id
                    group.last_seen_at = _now()
                candidate = candidate_by_fingerprint.get(fp)
                if candidate is None:
                    candidate = DuplicateCandidate(
                        group_id=group.id,
                        fingerprint=fp,
                        status=DuplicateStatus(status),
                        score=score,
                        reasons=reasons,
                        receipt_ids=sorted([a.id, b.id]),
                        evidence=evidence,
                        created_by_run_id=run.id,
                        last_seen_run_id=run.id,
                    )
                    db.add(candidate)
                    candidate_by_fingerprint[fp] = candidate
                elif candidate.status in {DuplicateStatus.pending, DuplicateStatus.needs_review}:
                    candidate.status = DuplicateStatus(status)
                    candidate.score = score
                    candidate.reasons = reasons
                    candidate.evidence = evidence
                    candidate.last_seen_run_id = run.id
                    candidate.last_seen_at = _now()
        run.candidates_found = found
        run.candidates_new = new
        run.status = DuplicateRunStatus.success
    except Exception as exc:
        db.rollback()
        run = db.get(DuplicateRun, run.id) or run
        run.status = DuplicateRunStatus.error
        run.error_message = str(exc)
    finally:
        run.finished_at = _now()
        db.commit()
        db.refresh(run)
        _LOCK.release()
    return run


def run_duplicate_search(db: Session, *, trigger: str = "manual") -> DuplicateRun:
    """Run synchronously for the scheduler and existing service callers."""
    run = start_duplicate_search(db, trigger=trigger)
    return _execute_duplicate_search(db, run)


def finish_duplicate_search(run_id: str) -> None:
    """Finish a previously created run in an isolated worker/session."""
    from app.db import SessionLocal

    try:
        with SessionLocal() as db:
            run = db.get(DuplicateRun, run_id)
            if run is not None and run.status == DuplicateRunStatus.running:
                _execute_duplicate_search(db, run)
            else:
                if _LOCK.locked():
                    _LOCK.release()
    except Exception:
        # _execute_duplicate_search records normal failures on the run.  This
        # guard is for failures before a usable session/run could be loaded.
        if _LOCK.locked():
            _LOCK.release()
        raise


def canonical_receipt(receipts: list[Receipt]) -> Receipt:
    source_rank = {"metro": 100, "silpo": 90, "fozzy": 90, "fora": 90, "atb": 90, "scanner": 60}

    def rank(r: Receipt) -> tuple[int, int, int]:
        raw = r.raw or {}
        structured = 20 if raw.get("method") in {"fiscal_check", "api"} or r.store_code != "scanner" else 0
        return (source_rank.get(r.store_code, 70) + structured, len(r.items), 1 if r.receipt_url else 0)

    return max(receipts, key=rank)


def _receipt_snapshot(receipt: Receipt) -> dict[str, Any]:
    return {
        "fields": {
            "purchased_at": as_utc(receipt.purchased_at).isoformat(),
            "total_amount": float(receipt.total_amount),
            "currency": receipt.currency,
            "exchange_rate_to_uah": float(receipt.exchange_rate_to_uah) if receipt.exchange_rate_to_uah is not None else None,
            "shop_name": receipt.shop_name,
            "shop_address": receipt.shop_address,
            "payment_type": receipt.payment_type,
            "channel": receipt.channel,
            "total_discount": float(receipt.total_discount or 0),
            "bonuses": float(receipt.bonuses) if receipt.bonuses is not None else None,
            "bonuses_used": float(receipt.bonuses_used) if receipt.bonuses_used is not None else None,
            "receipt_url": receipt.receipt_url,
            "order_status": receipt.order_status,
            "tracking_numbers": receipt.tracking_numbers or [],
            "photo_filename": receipt.photo_filename,
            "has_warranty": receipt.has_warranty,
            "warranty_expires_at": as_utc(receipt.warranty_expires_at).isoformat() if receipt.warranty_expires_at else None,
            "warranty_pdf_filename": receipt.warranty_pdf_filename,
            "raw": receipt.raw or {},
        },
        "items": [
            {
                "name": i.name,
                "barcode": i.barcode,
                "product_code": i.product_code,
                "category_raw": i.category_raw,
                "category_id": i.category_id,
                "quantity": float(i.quantity),
                "unit": i.unit,
                "unit_price": float(i.unit_price) if i.unit_price is not None else None,
                "total_price": float(i.total_price),
                "discount": float(i.discount or 0),
                "product_url": i.product_url,
                "product_image_url": i.product_image_url,
                "product_image_source_url": i.product_image_source_url,
                "merchant_name": i.merchant_name,
                "product_image_urls": i.product_image_urls or [],
                "has_warranty": i.has_warranty,
                "warranty_text": i.warranty_text,
                "warranty_expires_at": as_utc(i.warranty_expires_at).isoformat() if i.warranty_expires_at else None,
                "warranty_checked_at": as_utc(i.warranty_checked_at).isoformat() if i.warranty_checked_at else None,
            }
            for i in receipt.items
        ],
        "tags": [t.tag for t in receipt.tags],
    }


def _clone_item(item: ReceiptItem) -> ReceiptItem:
    return ReceiptItem(
        name=item.name,
        barcode=item.barcode,
        product_code=item.product_code,
        category_raw=item.category_raw,
        category_id=item.category_id,
        quantity=item.quantity,
        unit=item.unit,
        unit_price=item.unit_price,
        total_price=item.total_price,
        discount=item.discount,
        product_url=item.product_url,
        product_image_url=item.product_image_url,
        product_image_source_url=item.product_image_source_url,
        merchant_name=item.merchant_name,
        product_image_urls=item.product_image_urls,
        has_warranty=item.has_warranty,
        warranty_text=item.warranty_text,
        warranty_expires_at=item.warranty_expires_at,
        warranty_checked_at=item.warranty_checked_at,
    )


def _item_value(item: ReceiptItem, field: str) -> Any:
    value = getattr(item, field)
    if isinstance(value, Decimal):
        return float(value)
    return value


def _item_summary(item: ReceiptItem, receipt: Receipt | None = None) -> dict[str, Any]:
    return {
        "receipt_id": receipt.id if receipt else item.receipt_id,
        "store_code": receipt.store_code if receipt else None,
        "name": item.name,
        "quantity": float(item.quantity),
        "unit": item.unit,
        "unit_price": float(item.unit_price) if item.unit_price is not None else None,
        "total_price": float(item.total_price),
        "discount": float(item.discount or 0),
        "barcode": item.barcode,
        "product_code": item.product_code,
        "category_id": item.category_id,
        "product_url": item.product_url,
        "product_image_url": item.product_image_url,
        "merchant_name": item.merchant_name,
    }


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dt.datetime):
        return as_utc(value).isoformat()
    return value


def _receipt_field_source(receipt: Receipt, field: str) -> dict[str, Any]:
    return {
        "receipt_id": receipt.id,
        "store_code": receipt.store_code,
        "value": _json_value(getattr(receipt, field)),
    }


def _materialize_canonical_photo(receipt: Receipt) -> None:
    """Give the merged receipt its own stable photo file.

    A source photo is normally named after the source receipt ID. Keeping that
    filename happened to work until the source row/file was cleaned up, after
    which the visible canonical receipt had a dangling image reference.
    """
    if not receipt.photo_filename:
        return
    source = PHOTO_DIR / receipt.photo_filename
    if not source.is_file():
        return
    suffix = source.suffix.lower() or ".jpg"
    target = PHOTO_DIR / f"{receipt.id}{suffix}"
    if source != target and not target.exists():
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    receipt.photo_filename = target.name


def _field_differs(sources: list[dict[str, Any]]) -> bool:
    return len({_compare_key(source["value"]) for source in sources}) > 1


def _receipt_field_plan(field: str, receipts: list[Receipt], chooser) -> dict[str, Any]:
    best = chooser(receipts)
    sources = [_receipt_field_source(receipt, field) for receipt in receipts]
    return {
        "field": field,
        "result": _json_value(getattr(best, field)),
        "from_receipt_id": best.id,
        "sources": sources,
        "changed": _field_differs(sources),
    }


def _receipt_fields_plan(receipts: list[Receipt], canonical: Receipt) -> list[dict[str, Any]]:
    return [
        _receipt_field_plan("purchased_at", receipts, lambda _rs: canonical),
        _receipt_field_plan("total_amount", receipts, lambda _rs: canonical),
        _receipt_field_plan("currency", receipts, lambda _rs: canonical),
        _receipt_field_plan("exchange_rate_to_uah", receipts, lambda rs: max(rs, key=lambda r: float(r.exchange_rate_to_uah or 0))),
        _receipt_field_plan("shop_name", receipts, lambda rs: max(rs, key=lambda r: len(r.shop_name or ""))),
        _receipt_field_plan("shop_address", receipts, lambda rs: max(rs, key=lambda r: len(r.shop_address or ""))),
        _receipt_field_plan("payment_type", receipts, lambda rs: max(rs, key=lambda r: len(r.payment_type or ""))),
        _receipt_field_plan("total_discount", receipts, lambda rs: max(rs, key=lambda r: float(r.total_discount or 0))),
        _receipt_field_plan("bonuses", receipts, lambda rs: max(rs, key=lambda r: float(r.bonuses or 0))),
        _receipt_field_plan("bonuses_used", receipts, lambda rs: max(rs, key=lambda r: float(r.bonuses_used or 0))),
        _receipt_field_plan("receipt_url", receipts, lambda rs: max(rs, key=lambda r: len(r.receipt_url or ""))),
        _receipt_field_plan("order_status", receipts, lambda rs: max(rs, key=lambda r: len(r.order_status or ""))),
        _receipt_field_plan("tracking_numbers", receipts, lambda rs: max(rs, key=lambda r: len(r.tracking_numbers or []))),
        _receipt_field_plan("photo_filename", receipts, lambda rs: max(rs, key=lambda r: 1 if r.photo_filename else 0)),
        _receipt_field_plan("has_warranty", receipts, lambda rs: max(rs, key=lambda r: 1 if r.has_warranty else 0)),
        _receipt_field_plan("warranty_expires_at", receipts, lambda rs: max(rs, key=lambda r: as_utc(r.warranty_expires_at) or dt.datetime.min.replace(tzinfo=dt.timezone.utc))),
        _receipt_field_plan("warranty_pdf_filename", receipts, lambda rs: max(rs, key=lambda r: 1 if r.warranty_pdf_filename else 0)),
    ]


def _name_quality(receipt: Receipt, item: ReceiptItem) -> tuple[int, int, int]:
    name = item.name.strip()
    trailing_noise = 1 if name.endswith((".", ",", ";", ":")) else 0
    return (
        1 if receipt.store_code != "scanner" else 0,
        1 if not trailing_noise else 0,
        len(_norm(name) or ""),
    )


def _identity_quality(receipt: Receipt, item: ReceiptItem) -> tuple[int, int, int, int, int]:
    return (
        1 if item.barcode else 0,
        1 if item.category_id else 0,
        *_name_quality(receipt, item),
    )


def _money_quality(receipt: Receipt, item: ReceiptItem) -> tuple[int, int, int, int]:
    return (
        1 if item.discount and float(item.discount) > 0 else 0,
        1 if receipt.total_discount and float(receipt.total_discount) > 0 else 0,
        1 if item.unit_price is not None else 0,
        1 if receipt.store_code != "scanner" else 0,
    )


def _merge_item_fields(target: ReceiptItem, source: ReceiptItem, *, include_money: bool = False) -> list[str]:
    changed: list[str] = []
    fields = [
        "barcode",
        "product_code",
        "category_raw",
        "category_id",
        "unit",
        "product_url",
        "product_image_url",
        "product_image_source_url",
        "merchant_name",
        "has_warranty",
        "warranty_text",
        "warranty_expires_at",
        "warranty_checked_at",
    ]
    if include_money:
        fields.extend(["unit_price", "total_price", "discount"])
    for field in fields:
        if getattr(target, field) in (None, "", []) and getattr(source, field) not in (None, "", []):
            setattr(target, field, getattr(source, field))
            changed.append(field)
    if not target.product_image_urls and source.product_image_urls:
        target.product_image_urls = source.product_image_urls
        changed.append("product_image_urls")
    return changed


def _items_line_up(a: ReceiptItem, b: ReceiptItem) -> bool:
    same_qty = abs(float(a.quantity or 0) - float(b.quantity or 0)) <= 0.001
    if not same_qty:
        return False
    if _compare_key(a.name) == _compare_key(b.name):
        return True
    names = SequenceMatcher(None, _norm(a.name) or "", _norm(b.name) or "").ratio()
    return names >= 0.72


def _match_item_for(base: ReceiptItem, candidates: list[ReceiptItem], used: set[int]) -> tuple[int, ReceiptItem] | None:
    best: tuple[float, int, ReceiptItem] | None = None
    for idx, item in enumerate(candidates):
        if idx in used:
            continue
        if abs(float(base.quantity or 0) - float(item.quantity or 0)) > 0.001:
            continue
        if _compare_key(base.name) == _compare_key(item.name):
            return idx, item
        ratio = SequenceMatcher(None, _norm(base.name) or "", _norm(item.name) or "").ratio()
        if ratio >= 0.72 and (best is None or ratio > best[0]):
            best = (ratio, idx, item)
    if best is None:
        return None
    return best[1], best[2]


def _source_by_receipt_id(
    sources: list[tuple[Receipt, ReceiptItem]],
    receipt_id: str | None,
) -> tuple[Receipt, ReceiptItem] | None:
    if not receipt_id:
        return None
    return next((pair for pair in sources if pair[0].id == receipt_id), None)


def _apply_item_merge(
    target: ReceiptItem,
    sources: list[tuple[Receipt, ReceiptItem]],
    *,
    identity_receipt_id: str | None = None,
    name_receipt_id: str | None = None,
    barcode_receipt_id: str | None = None,
    money_receipt_id: str | None = None,
    price_receipt_id: str | None = None,
    discount_receipt_id: str | None = None,
) -> dict[str, Any]:
    before = _item_summary(target)
    identity_receipt, identity_item = (
        _source_by_receipt_id(sources, identity_receipt_id)
        or max(sources, key=lambda pair: _identity_quality(pair[0], pair[1]))
    )
    money_receipt, money_item = (
        _source_by_receipt_id(sources, money_receipt_id)
        or max(sources, key=lambda pair: _money_quality(pair[0], pair[1]))
    )
    name_receipt, name_item = _source_by_receipt_id(sources, name_receipt_id) or (identity_receipt, identity_item)
    barcode_receipt, barcode_item = _source_by_receipt_id(sources, barcode_receipt_id) or (identity_receipt, identity_item)
    price_receipt, price_item = _source_by_receipt_id(sources, price_receipt_id) or (money_receipt, money_item)
    discount_receipt, discount_item = _source_by_receipt_id(sources, discount_receipt_id) or (money_receipt, money_item)

    target.name = name_item.name.strip().rstrip(" .,;:")
    target.barcode = barcode_item.barcode
    target.product_code = identity_item.product_code or barcode_item.product_code
    target.category_raw = identity_item.category_raw
    target.category_id = identity_item.category_id
    target.product_url = identity_item.product_url
    target.product_image_url = identity_item.product_image_url
    target.product_image_source_url = identity_item.product_image_source_url
    target.merchant_name = identity_item.merchant_name
    target.product_image_urls = identity_item.product_image_urls or []
    target.has_warranty = identity_item.has_warranty
    target.warranty_text = identity_item.warranty_text
    target.warranty_expires_at = identity_item.warranty_expires_at
    target.warranty_checked_at = identity_item.warranty_checked_at
    target.quantity = price_item.quantity
    target.unit = price_item.unit or target.unit
    target.unit_price = price_item.unit_price
    target.total_price = price_item.total_price
    target.discount = discount_item.discount or 0

    changed_fields: set[str] = set()
    if before["name"] != target.name:
        changed_fields.add("name")
    for field in (
        "quantity",
        "unit",
        "unit_price",
        "total_price",
        "discount",
        "barcode",
        "product_code",
        "category_id",
        "product_url",
        "product_image_url",
        "merchant_name",
    ):
        if before.get(field) != _item_value(target, field):
            changed_fields.add(field)

    for receipt, source in sources:
        if identity_receipt_id and receipt.id != identity_receipt_id:
            continue
        changed_fields.update(_merge_item_fields(target, source))

    after = _item_summary(target)
    return {
        "result": after,
        "sources": [_item_summary(item, receipt) for receipt, item in sources],
        "identity_from_receipt_id": identity_receipt.id,
        "name_from_receipt_id": name_receipt.id,
        "barcode_from_receipt_id": barcode_receipt.id,
        "money_from_receipt_id": money_receipt.id,
        "price_from_receipt_id": price_receipt.id,
        "discount_from_receipt_id": discount_receipt.id,
        "changed_fields": sorted(changed_fields),
    }


def _merge_matching_item_fields(canonical: Receipt, sources: list[Receipt], choices: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if not canonical.items:
        return []
    plans: list[dict[str, Any]] = []
    used_by_receipt: dict[str, set[int]] = {receipt.id: set() for receipt in sources}
    for idx, target in enumerate(canonical.items):
        item_sources: list[tuple[Receipt, ReceiptItem]] = [(canonical, target)]
        for receipt in sources:
            source_item = receipt.items[idx] if idx < len(receipt.items) and _items_line_up(target, receipt.items[idx]) else None
            source_idx = idx if source_item is not None else None
            if source_item is None:
                matched = _match_item_for(target, receipt.items, used_by_receipt[receipt.id])
                if matched is not None:
                    source_idx, source_item = matched
            if source_item is not None and source_idx is not None:
                used_by_receipt[receipt.id].add(source_idx)
                item_sources.append((receipt, source_item))
        idx_key = str(idx)
        plan = _apply_item_merge(
            target,
            item_sources,
            identity_receipt_id=(choices or {}).get("item_identity", {}).get(idx_key),
            name_receipt_id=(choices or {}).get("item_name", {}).get(idx_key),
            barcode_receipt_id=(choices or {}).get("item_barcode", {}).get(idx_key),
            money_receipt_id=(choices or {}).get("item_money", {}).get(idx_key),
            price_receipt_id=(choices or {}).get("item_price", {}).get(idx_key),
            discount_receipt_id=(choices or {}).get("item_discount", {}).get(idx_key),
        )
        plan["index"] = idx
        plan["matched"] = len(item_sources)
        plans.append(plan)
    return plans


def build_merge_plan(db: Session, group: DuplicateGroup) -> dict[str, Any]:
    receipts = db.execute(
        select(Receipt)
        .where(Receipt.id.in_(group.receipt_ids))
        .options(selectinload(Receipt.items), selectinload(Receipt.tags), selectinload(Receipt.account))
    ).scalars().all()
    if len(receipts) < 2:
        return {"warnings": ["Duplicate group must contain at least two receipts"], "items": []}

    canonical = canonical_receipt(receipts)
    item_source = max(receipts, key=lambda r: len(r.items))
    base_items = item_source.items if len(item_source.items) > len(canonical.items) else canonical.items
    warnings = []
    if len({len(r.items) for r in receipts}) > 1:
        warnings.append("Різна кількість позицій: базою стане найдовший список, інші позиції тільки збагачують збіги.")

    item_plan = []
    used_by_receipt: dict[str, set[int]] = {receipt.id: set() for receipt in receipts}
    for idx, base_item in enumerate(base_items):
        item_sources: list[tuple[Receipt, ReceiptItem]] = [(item_source if base_item in item_source.items else canonical, base_item)]
        for receipt in receipts:
            if receipt.id == item_sources[0][0].id:
                continue
            source_item = receipt.items[idx] if idx < len(receipt.items) and _items_line_up(base_item, receipt.items[idx]) else None
            source_idx = idx if source_item is not None else None
            if source_item is None:
                matched = _match_item_for(base_item, receipt.items, used_by_receipt[receipt.id])
                if matched is not None:
                    source_idx, source_item = matched
            if source_item is not None and source_idx is not None:
                used_by_receipt[receipt.id].add(source_idx)
                item_sources.append((receipt, source_item))

        preview = _clone_item(base_item)
        decision = _apply_item_merge(preview, item_sources)
        decision["index"] = idx
        decision["matched"] = len(item_sources)
        item_plan.append(decision)

    return {
        "canonical_receipt_id": canonical.id,
        "base_items_receipt_id": item_source.id,
        "warnings": warnings,
        "fields": _receipt_fields_plan(receipts, canonical),
        "items": item_plan,
    }


def _parse_dt(value: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(value) if value else None


def merge_group(db: Session, group_id: str, *, choices: dict[str, Any] | None = None) -> DuplicateGroup:
    group = db.get(DuplicateGroup, group_id)
    if group is None:
        raise ValueError("Duplicate group not found")
    receipts = db.execute(
        select(Receipt)
        .where(Receipt.id.in_(group.receipt_ids))
        .options(selectinload(Receipt.items), selectinload(Receipt.tags))
    ).scalars().all()
    if len(receipts) < 2:
        raise ValueError("Duplicate group must contain at least two receipts")
    canonical = canonical_receipt(receipts)
    sources = [r for r in receipts if r.id != canonical.id]
    snapshots = {r.id: _receipt_snapshot(r) for r in receipts}
    provenance = (canonical.raw or {}).get("field_provenance", {})
    log: list[dict[str, Any]] = []

    def take(field: str, chooser) -> None:
        nonlocal provenance
        selected_id = (choices or {}).get("receipt_fields", {}).get(field)
        best = next((r for r in receipts if r.id == selected_id), None) or chooser(receipts)
        value = getattr(best, field)
        if value not in (None, "", []):
            old = getattr(canonical, field)
            setattr(canonical, field, value)
            provenance[field] = best.id
            if old != value:
                log.append({"field": field, "from_receipt_id": best.id})

    take("shop_name", lambda rs: max(rs, key=lambda r: len(r.shop_name or "")))
    take("shop_address", lambda rs: max(rs, key=lambda r: len(r.shop_address or "")))
    take("payment_type", lambda rs: max(rs, key=lambda r: len(r.payment_type or "")))
    take("exchange_rate_to_uah", lambda rs: max(rs, key=lambda r: float(r.exchange_rate_to_uah or 0)))
    take("receipt_url", lambda rs: max(rs, key=lambda r: len(r.receipt_url or "")))
    take("total_discount", lambda rs: max(rs, key=lambda r: float(r.total_discount or 0)))
    take("bonuses", lambda rs: max(rs, key=lambda r: float(r.bonuses or 0)))
    take("bonuses_used", lambda rs: max(rs, key=lambda r: float(r.bonuses_used or 0)))
    take("order_status", lambda rs: max(rs, key=lambda r: len(r.order_status or "")))
    take("tracking_numbers", lambda rs: max(rs, key=lambda r: len(r.tracking_numbers or [])))
    take("photo_filename", lambda rs: max(rs, key=lambda r: 1 if r.photo_filename else 0))
    take("warranty_pdf_filename", lambda rs: max(rs, key=lambda r: 1 if r.warranty_pdf_filename else 0))
    take("has_warranty", lambda rs: max(rs, key=lambda r: 1 if r.has_warranty else 0))
    take("warranty_expires_at", lambda rs: max(rs, key=lambda r: r.warranty_expires_at or dt.datetime.min))
    item_source = max(receipts, key=lambda r: len(r.items))
    if item_source.id != canonical.id and len(item_source.items) > len(canonical.items):
        canonical.items = [_clone_item(i) for i in item_source.items]
        db.flush()
        provenance["items"] = item_source.id
        log.append({"field": "items", "from_receipt_id": item_source.id, "count": len(item_source.items)})
    item_plan = _merge_matching_item_fields(canonical, sources, choices)
    changed_item_plan = [item for item in item_plan if item["changed_fields"]]
    if changed_item_plan:
        provenance["item_merge"] = changed_item_plan
        log.append({
            "field": "items",
            "strategy": "matched-field-merge",
            "count": len(changed_item_plan),
            "fields": sorted({field for item in changed_item_plan for field in item["changed_fields"]}),
        })
    existing_tags = {t.tag for t in canonical.tags}
    for receipt in receipts:
        for tag in receipt.tags:
            if tag.tag not in existing_tags:
                db.add(ReceiptTag(receipt_id=canonical.id, tag=tag.tag))
                existing_tags.add(tag.tag)
    external_refs = []
    raw_sources = {}
    for receipt in receipts:
        external_refs.append({
            "receipt_id": receipt.id,
            "account_id": receipt.account_id,
            "store_code": receipt.store_code,
            "external_id": receipt.external_id,
            "receipt_url": receipt.receipt_url,
            "photo_filename": receipt.photo_filename,
        })
        raw_sources[receipt.id] = receipt.raw or {}
        if receipt.id != canonical.id:
            receipt.raw = {
                **(receipt.raw or {}),
                "merged_into_receipt_id": canonical.id,
                "duplicate_group_id": group.id,
                "merged_at": _now().isoformat(),
            }
    canonical.raw = {
        **(canonical.raw or {}),
        "duplicate_group_id": group.id,
        "source_receipts": external_refs,
        "source_payloads": raw_sources,
        "field_provenance": provenance,
        "merge_plan": build_merge_plan(db, group),
    }
    _materialize_canonical_photo(canonical)
    group.status = DuplicateStatus.auto_merged
    group.canonical_receipt_id = canonical.id
    group.merged_at = _now()
    group.merge_log = log
    group.rollback_snapshot = snapshots
    for candidate in group.candidates:
        candidate.status = DuplicateStatus.auto_merged
    db.commit()
    db.refresh(group)
    return group


def reject_group(db: Session, group_id: str) -> DuplicateGroup:
    group = db.get(DuplicateGroup, group_id)
    if group is None:
        raise ValueError("Duplicate group not found")
    group.status = DuplicateStatus.rejected
    for candidate in group.candidates:
        candidate.status = DuplicateStatus.rejected
    db.commit()
    db.refresh(group)
    return group


def rollback_group(db: Session, group_id: str) -> DuplicateGroup:
    group = db.get(DuplicateGroup, group_id)
    if group is None or not group.rollback_snapshot:
        raise ValueError("Duplicate group has no rollback snapshot")
    receipts = db.execute(
        select(Receipt)
        .where(Receipt.id.in_(list(group.rollback_snapshot.keys())))
        .options(selectinload(Receipt.items), selectinload(Receipt.tags))
    ).scalars().all()
    by_id = {r.id: r for r in receipts}
    for receipt_id, snap in group.rollback_snapshot.items():
        receipt = by_id.get(receipt_id)
        if receipt is None:
            continue
        fields = snap["fields"]
        for key, value in fields.items():
            if key in {"purchased_at", "warranty_expires_at"}:
                value = _parse_dt(value)
            elif key in {"total_amount", "total_discount", "bonuses", "bonuses_used", "exchange_rate_to_uah"} and value is not None:
                value = Decimal(str(value))
            setattr(receipt, key, value)
        receipt.items = [
            ReceiptItem(
                name=i["name"],
                barcode=i["barcode"],
                product_code=i.get("product_code"),
                category_raw=i["category_raw"],
                category_id=i["category_id"],
                quantity=Decimal(str(i["quantity"])),
                unit=i["unit"],
                unit_price=Decimal(str(i["unit_price"])) if i["unit_price"] is not None else None,
                total_price=Decimal(str(i["total_price"])),
                discount=Decimal(str(i["discount"])),
                product_url=i["product_url"],
                product_image_url=i["product_image_url"],
                product_image_source_url=i["product_image_source_url"],
                merchant_name=i["merchant_name"],
                product_image_urls=i["product_image_urls"],
                has_warranty=i["has_warranty"],
                warranty_text=i["warranty_text"],
                warranty_expires_at=_parse_dt(i["warranty_expires_at"]),
                warranty_checked_at=_parse_dt(i["warranty_checked_at"]),
            )
            for i in snap["items"]
        ]
        receipt.tags.clear()
        db.flush()
        receipt.tags = [ReceiptTag(receipt_id=receipt.id, tag=tag) for tag in snap["tags"]]
    group.status = DuplicateStatus.rolled_back
    group.rolled_back_at = _now()
    for candidate in group.candidates:
        candidate.status = DuplicateStatus.rolled_back
    db.commit()
    db.refresh(group)
    return group


def group_payload(db: Session, group: DuplicateGroup) -> dict[str, Any]:
    receipt_ids = group.receipt_ids or []
    if group.status == DuplicateStatus.auto_merged and group.canonical_receipt_id:
        receipt_ids = [group.canonical_receipt_id]
    receipts = db.execute(
        select(Receipt)
        .where(Receipt.id.in_(receipt_ids))
        .options(selectinload(Receipt.items), selectinload(Receipt.tags), selectinload(Receipt.account))
    ).scalars().all()
    return {
        "id": group.id,
        "status": group.status.value,
        "score": group.score,
        "reasons": group.reasons or [],
        "receipt_ids": group.receipt_ids or [],
        "canonical_receipt_id": group.canonical_receipt_id,
        "first_seen_at": group.first_seen_at,
        "last_seen_at": group.last_seen_at,
        "merged_at": group.merged_at,
        "rolled_back_at": group.rolled_back_at,
        "merge_log": group.merge_log or [],
        "merge_plan": build_merge_plan(db, group) if group.status != DuplicateStatus.auto_merged else (receipts[0].raw or {}).get("merge_plan") if receipts else None,
        "receipts": [_receipt_summary(r) for r in receipts],
    }


def _receipt_summary(r: Receipt) -> dict[str, Any]:
    return {
        "id": r.id,
        "account_id": r.account_id,
        "account_label": r.account.label if r.account else "",
        "store_code": r.store_code,
        "external_id": r.external_id,
        "purchased_at": r.purchased_at,
        "total_amount": float(r.total_amount),
        "currency": r.currency,
        "exchange_rate_to_uah": float(r.exchange_rate_to_uah) if r.exchange_rate_to_uah is not None else None,
        "shop_name": r.shop_name,
        "shop_address": r.shop_address,
        "payment_type": r.payment_type,
        "channel": r.channel,
        "receipt_url": r.receipt_url,
        "tags": [t.tag for t in r.tags],
        "items": [
            {
                "name": i.name,
                "quantity": float(i.quantity),
                "unit": i.unit,
                "unit_price": float(i.unit_price) if i.unit_price is not None else None,
                "total_price": float(i.total_price),
                "discount": float(i.discount or 0),
                "barcode": i.barcode,
                "category_id": i.category_id,
                "product_url": i.product_url,
                "product_image_url": i.product_image_url,
                "merchant_name": i.merchant_name,
            }
            for i in r.items
        ],
        "raw": r.raw or {},
    }


def list_groups(db: Session, *, status: str | None = None) -> list[dict[str, Any]]:
    stmt = select(DuplicateGroup).options(selectinload(DuplicateGroup.candidates)).order_by(
        DuplicateGroup.last_seen_at.desc()
    )
    if status:
        stmt = stmt.where(DuplicateGroup.status == DuplicateStatus(status))
    return [group_payload(db, g) for g in db.execute(stmt).scalars().all()]


def latest_runs(db: Session, limit: int = 10) -> list[DuplicateRun]:
    return db.execute(select(DuplicateRun).order_by(DuplicateRun.started_at.desc()).limit(limit)).scalars().all()
