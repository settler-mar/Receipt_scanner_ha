"""Fill in what a ПРРО document does not print: the shop behind the till.

A fiscal XML from ДПС names the seller only by ПН/ЄДРПОУ and the till by its
fiscal number (ФН) + serial (ZN). The shop sign, its address and the retail
network it belongs to are printed on the paper receipt, not in the document —
so an XML imported on its own lands with an empty `shop_name`.

Everything needed is already in the user's own history: the same till shows up
on receipts collected from the store's app, which do carry the shop name, the
address and the network. This module matches the two on the till identity:

    ФН РРО / ZN  →  that exact shop (name + address + network)
    ПН / ЄДРПОУ  →  the network only (many shops share one legal entity)

`networks.py` remains the offline fallback: it knows the brand for a ПН even
when no earlier receipt from that network exists.
"""
from __future__ import annotations

import dataclasses
import logging
import re

from sqlalchemy import String, cast, select
from sqlalchemy.orm import Session

from app.adapters.base import NormalizedReceipt
from app.models.models import Receipt
from app.scanner import networks

logger = logging.getLogger(__name__)

# The ingest channels this enrichment is for: documents that come from ДПС and
# therefore never carry a shop sign of their own.
FISCAL_METHODS = ("fiscal_check", "cabinet_check_xml", "tax_cabinet")

# The scanner account is the ingest channel, not a shop — never learn from it
# as if it were a store, and always allow it to be replaced by a real network.
_INGEST_CODES = ("scanner",)


@dataclasses.dataclass
class SellerHint:
    name: str | None = None
    address: str | None = None
    store_code: str | None = None
    store_number: str | None = None
    matched_by: str = ""              # "rro" | "rro_serial" | "tax_number" | "network"
    source_receipt_id: str | None = None

    def as_raw(self) -> dict:
        return {k: v for k, v in dataclasses.asdict(self).items() if v}


def _fiscal_number(nr: NormalizedReceipt) -> str | None:
    """The till's ФН, as `fiscal:<ФН>:<документ>` or the viewer URL keeps it."""
    if m := re.match(r"fiscal:(\d{6,}):", nr.external_id or ""):
        return m.group(1)
    for source in (nr.receipt_url or "", (nr.raw or {}).get("fiscal_url") or ""):
        if m := re.search(r"[?&]fn=(\d{6,})", source):
            return m.group(1)
    return None


def _candidate_hint(row: Receipt) -> SellerHint:
    """Read a shop out of a receipt from any channel. Store-app receipts keep it
    under `shop`; everything else has it on the receipt columns."""
    raw = row.raw or {}
    shop = raw.get("shop") if isinstance(raw.get("shop"), dict) else {}
    return SellerHint(
        name=row.shop_name or (shop.get("name") or None),
        address=row.shop_address or (shop.get("addr") or None),
        store_code=row.store_code if row.store_code not in _INGEST_CODES else None,
        store_number=str(shop.get("num")) if shop.get("num") else None,
        source_receipt_id=row.id,
    )


def _search(db: Session, needle: str, limit: int = 25) -> list[Receipt]:
    if not needle:
        return []
    return list(db.execute(
        select(Receipt)
        .where(cast(Receipt.raw, String).like(f"%{needle}%"))
        .order_by(Receipt.purchased_at.desc())
        .limit(limit)
    ).scalars())


def _till_match(row: Receipt, fiscal_number: str | None, rro_serial: str | None) -> str | None:
    """Confirm the hit really is the same till and not an incidental substring."""
    raw = row.raw or {}
    rcpt = raw.get("rcpt") if isinstance(raw.get("rcpt"), dict) else {}
    if fiscal_number:
        if str(rcpt.get("rroFiscal") or "") == fiscal_number:
            return "rro"
        if re.search(rf"[?&]fn={fiscal_number}(?!\d)", str(raw.get("fiscal_url") or "")):
            return "rro"
    if rro_serial:
        if str(rcpt.get("rroSerial") or "") == rro_serial:
            return "rro_serial"
        if str(raw.get("rro_serial") or "") == rro_serial:
            return "rro_serial"
    return None


def resolve(db: Session, nr: NormalizedReceipt) -> SellerHint | None:
    """Best hint for this fiscal receipt: the exact shop when the till is known,
    otherwise the network the seller's ПН belongs to."""
    raw = nr.raw or {}
    fiscal_number = _fiscal_number(nr)
    rro_serial = (raw.get("rro_serial") or "").strip() or None
    tax_number = (raw.get("seller_tax_number") or "").strip() or None
    edrpou = (raw.get("seller_edrpou") or "").strip() or None

    for needle in (fiscal_number, rro_serial):
        for row in _search(db, needle or ""):
            if row.external_id == nr.external_id:
                continue
            matched_by = _till_match(row, fiscal_number, rro_serial)
            if not matched_by:
                continue
            hint = _candidate_hint(row)
            if hint.name or hint.address or hint.store_code:
                hint.matched_by = matched_by
                return hint

    # A ПН covers the whole legal entity, so it may not name the shop or carry
    # its address — only the network is safe to take from it.
    network = None
    if identity := networks.by_tax_number(tax_number or ""):
        network = identity[0]
    elif edrpou:
        network = networks.by_edrpou(edrpou)
    if network:
        return SellerHint(name=network.name, store_code=network.store_code,
                          matched_by="network")
    for needle in (tax_number, edrpou):
        for row in _search(db, needle or ""):
            code = row.store_code if row.store_code not in _INGEST_CODES else None
            if code:
                return SellerHint(store_code=code, matched_by="tax_number",
                                  source_receipt_id=row.id)
    return None


def enrich(db: Session, nr: NormalizedReceipt) -> SellerHint | None:
    """Fill the gaps a ДПС document leaves. Never overwrites what the document
    (or the user) already stated — only fills empty fields."""
    if (nr.raw or {}).get("method") not in FISCAL_METHODS:
        return None
    try:
        hint = resolve(db, nr)
    except Exception:  # noqa: BLE001 — enrichment must never block an import
        logger.exception("seller lookup failed")
        return None
    if hint is None:
        return None
    if hint.name and (not nr.shop_name or hint.matched_by in ("rro", "rro_serial")):
        nr.shop_name = hint.name
    if hint.address and not nr.shop_address:
        nr.shop_address = hint.address
    if hint.store_code:
        nr.store_code = nr.store_code or hint.store_code
    nr.raw = {**(nr.raw or {}), "seller_match": hint.as_raw()}
    return hint
