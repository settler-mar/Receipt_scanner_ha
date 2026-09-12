"""Stage orchestrator (ROADMAP.md Крок 3 "Розпізнавання чеків").

`scan_receipt_bytes` runs the stages in order and stops at the first result that
reconciles (reconcile.py). It is deliberately DB-free and side-effect-free — the
AI stage is passed in as a coroutine factory so this module never imports
app.services.ai directly and stays trivially unit-testable. app/api/inbox.py is
the thin layer that supplies the AI callable and persists the outcome.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import re
import logging
from typing import Awaitable, Callable

from app.adapters.base import NormalizedItem, NormalizedReceipt
from app.core.time import store_local_to_utc
from app.scanner import generic, networks, tax_cabinet
from app.scanner import fiscal_qr as fiscal_qr_module
from app.scanner.fiscal_qr import FiscalQr, scan_qr
from app.scanner.ocr import OcrUnavailable, run_ocr
from app.scanner.reconcile import Reconciliation, reconcile
from app.scanner.templates import identify_template, parse_with_template

logger = logging.getLogger(__name__)

AiOcr = Callable[[], Awaitable[dict | None]]

Method = str  # "qr" | "tax_cabinet" | "template:<code>" | "generic" | "ai" | "none"


@dataclasses.dataclass
class ScanOutcome:
    method: Method = "none"
    # "high"  — reconciled, safe to persist automatically
    # "low"   — a draft worth showing the user for manual review
    # "none"  — nothing usable
    confidence: str = "none"
    receipt: NormalizedReceipt | None = None
    reconciliation: Reconciliation | None = None
    fiscal: FiscalQr | None = None
    ocr_available: bool = False
    # fiscal_qr.QR_NONE | QR_UNREADABLE | QR_DECODED — what the photo's QR did.
    qr_state: str = fiscal_qr_module.QR_NONE
    # Set when the QR named a ДПС document that is already on file: the run
    # stops there, since re-reading the paper cannot beat the filed receipt.
    duplicate_receipt_id: str | None = None
    notes: list[str] = dataclasses.field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.confidence == "high"


def _content_key(image: bytes) -> str:
    return "scan:" + hashlib.sha1(image).hexdigest()[:20]


def _finalize(receipt: NormalizedReceipt, fiscal: FiscalQr | None, image: bytes) -> NormalizedReceipt:
    if not receipt.external_id:
        receipt.external_id = (fiscal.dedup_key if fiscal else None) or _content_key(image)
    if fiscal is not None:
        # QR fields come from the fiscal payload, so they are more reliable than
        # OCR/vision guesses. This is especially important for screenshots of a
        # bank transaction: the screen may show item/total but not a printed
        # timestamp, causing vision to default to "now".
        if fiscal.issued_at is not None:
            receipt.purchased_at = fiscal.issued_at
        if fiscal.total_amount is not None:
            receipt.total_amount = fiscal.total_amount
        if not receipt.receipt_url:
            receipt.receipt_url = fiscal.raw_url
        receipt.raw = {**(receipt.raw or {}), "fiscal_url": fiscal.raw_url}
    return receipt


def _receipt_from_qr(fiscal: FiscalQr) -> NormalizedReceipt:
    return NormalizedReceipt(
        external_id=fiscal.dedup_key or "",
        purchased_at=fiscal.issued_at or store_local_to_utc(dt.datetime.now()),
        total_amount=fiscal.total_amount or 0.0,
        raw={"method": "qr", "fiscal_url": fiscal.raw_url},
    )


def _receipt_from_ai(data: dict) -> NormalizedReceipt | None:
    if not isinstance(data, dict):
        return None
    items: list[NormalizedItem] = []
    for row in data.get("items") or []:
        try:
            name = str(row.get("name") or "").strip()
            total = float(row.get("total_price"))
        except (TypeError, ValueError, AttributeError):
            continue
        if not name:
            continue
        try:
            qty = float(row.get("quantity") or 1) or 1.0
        except (TypeError, ValueError):
            qty = 1.0
        try:
            unit_price = float(row["unit_price"]) if row.get("unit_price") is not None else None
        except (TypeError, ValueError):
            unit_price = None
        items.append(NormalizedItem(name=name, total_price=round(total, 2),
                                    quantity=qty, unit_price=unit_price))
    purchased_at = None
    raw_dt = data.get("purchased_at")
    if isinstance(raw_dt, str):
        try:
            purchased_at = store_local_to_utc(dt.datetime.fromisoformat(raw_dt.replace("Z", "")))
        except ValueError:
            purchased_at = None
    try:
        total_amount = float(data.get("total_amount"))
    except (TypeError, ValueError):
        total_amount = round(sum(it.total_price for it in items), 2)
    if not items and not total_amount:
        return None
    return NormalizedReceipt(
        external_id="",
        purchased_at=purchased_at or store_local_to_utc(dt.datetime.now()),
        total_amount=total_amount,
        currency=str(data.get("currency") or "UAH"),
        shop_name=(str(data["shop_name"]).strip() if data.get("shop_name") else None),
        shop_address=(str(data["shop_address"]).strip() if data.get("shop_address") else None),
        payment_type=(str(data["payment_type"]).strip() if data.get("payment_type") else None),
        items=items,
        raw={"method": "ai", "vision_complete": data.get("complete") is not False,
             "seller_name": data.get("seller_name")},
    )


async def scan_receipt_bytes(
    image: bytes,
    media_type: str = "image/jpeg",
    *,
    ai_ocr: AiOcr | None = None,
    enable_tax_cabinet: bool = True,
    tax_cabinet_token: str | None = None,
    fiscal_override: FiscalQr | None = None,
    ai_first: bool = False,
    known_receipt: "Callable[[FiscalQr], str | None] | None" = None,
) -> ScanOutcome:
    outcome = ScanOutcome()
    low_candidate: tuple[Method, NormalizedReceipt, Reconciliation] | None = None

    # --- Stage 1: fiscal QR -------------------------------------------------
    # Always read the image's own QR first, even when the caller supplies one:
    # the QR is the authoritative source, so every run gets a fresh chance at it
    # and `qr_state` keeps describing this image rather than the caller's hint.
    qr = scan_qr(image)
    fiscal, outcome.qr_state = qr.fiscal, qr.state
    if qr.channel:
        # Worth saying: the code was only readable once the photo was split into
        # colour channels, i.e. something coloured (a shop's stamp) sits on it.
        outcome.notes.append(f"QR прочитано в {qr.channel} каналі — на коді є кольорова позначка")
    if qr.state == fiscal_qr_module.QR_UNREADABLE:
        outcome.notes.append("a QR code is visible but too small/blurred to decode")
    elif qr.payloads and fiscal is None:
        outcome.notes.append("QR decoded but it is not a tax-cabinet receipt URL")
    if fiscal is None and fiscal_override is not None:
        # Nothing readable here — fall back to the QR a caller already knows,
        # from an earlier pass over this photo or from the receipt it belongs to.
        fiscal = fiscal_override
        outcome.notes.append(f"reusing a previously decoded fiscal QR: {fiscal.raw_url}")
    outcome.fiscal = fiscal
    if fiscal is not None:
        outcome.notes.append(f"fiscal QR: {fiscal.raw_url}")
        # The QR names the ДПС document, so if that document is already filed
        # this photo is another copy of it — settled before any fetch or model
        # call, neither of which could improve on the receipt already there.
        already = known_receipt(fiscal) if known_receipt else None
        if already:
            outcome.duplicate_receipt_id = already
            outcome.notes.append(f"already filed as receipt {already}")
            return outcome
        if enable_tax_cabinet:
            enriched = await tax_cabinet.fetch_structured(fiscal, api_token=tax_cabinet_token)
            if enriched is not None:
                enriched = _finalize(enriched, fiscal, image)
                rec = reconcile(enriched)
                if rec.ok:
                    return _done(outcome, "tax_cabinet", "high",
                                 enriched, rec)
                low_candidate = ("tax_cabinet", enriched, rec)
        qr_receipt = _receipt_from_qr(fiscal)
        if low_candidate is None:
            low_candidate = ("qr", qr_receipt, reconcile(qr_receipt))

    # --- Stage 2 & 3: local OCR → template, then generic ------------------
    # `ai_first` (a user asking to "recognise with AI") skips the local OCR and
    # template/generic parsers entirely and goes straight to vision.
    ocr_text: str | None = None
    net = None
    if ai_first:
        outcome.notes.append("ai_first: local OCR/templates skipped")
    else:
        try:
            ocr_text = run_ocr(image).text
            outcome.ocr_available = True
        except OcrUnavailable as exc:
            outcome.notes.append(f"local OCR unavailable: {exc}")

        net = networks.identify(ocr_text) if ocr_text else None
        if net is not None:
            outcome.notes.append(f"ЄДРПОУ {net.edrpou} → {net.name}")

    if ocr_text:
        collapsed_view = bool(re.search(r"показати\s+більше|показать\s+больше|show\s+more", ocr_text, re.I))
        if collapsed_view:
            outcome.notes.append("receipt view is collapsed and hides item lines")
        spec = identify_template(ocr_text)
        if spec is not None:
            outcome.notes.append(f"matched template {spec.code}")
            parsed = parse_with_template(spec, ocr_text)
            parsed = _finalize(parsed, fiscal, image)
            rec = reconcile(parsed)
            if rec.ok and not collapsed_view:
                return _done(outcome, f"template:{spec.code}", "high",
                             parsed, rec)
            low_candidate = _better(low_candidate, (f"template:{spec.code}", parsed, rec))

        drafted = generic.parse_generic(ocr_text)
        if drafted is not None:
            _stamp_network(drafted, net)
            drafted = _finalize(drafted, fiscal, image)
            rec = reconcile(drafted)
            if rec.ok and not collapsed_view:
                return _done(outcome, "generic", "high",
                             drafted, rec)
            low_candidate = _better(low_candidate, ("generic", drafted, rec))

    # --- Stage 4: AI vision (last resort) --------------------------------
    if ai_ocr is not None:
        try:
            data = await ai_ocr()
        except Exception as exc:  # noqa: BLE001
            outcome.notes.append(f"AI OCR failed: {exc}")
            data = None
        ai_receipt = _receipt_from_ai(data) if data else None
        if ai_receipt is not None:
            _stamp_network(ai_receipt, net)
            ai_receipt = _finalize(ai_receipt, fiscal, image)
            rec = reconcile(ai_receipt)
            complete = (ai_receipt.raw or {}).get("vision_complete", True)
            if not complete:
                outcome.notes.append("vision marked receipt as collapsed/incomplete")
            conf = "high" if rec.ok and complete else "low"
            # A vision answer is the one stage with nothing behind it: no QR, no
            # template, no OCR agreement. It has been seen to invent an entire
            # receipt (a different shop, a different year) for a photo it could
            # not read — and a one-line "receipt" balances against its own total
            # by construction, so `rec.ok` proves nothing there. Anything that
            # cannot be corroborated is filed as a draft for a human to look at.
            if conf == "high" and fiscal is None:
                for reason in _vision_doubts(ai_receipt, rec, ocr_text):
                    outcome.notes.append(reason)
                    conf = "low"
            return _done(outcome, "ai", conf, ai_receipt, rec)

    # --- Nothing reconciled: return the best draft we have ---------------
    if low_candidate is not None:
        method, receipt, rec = low_candidate
        _stamp_network(receipt, net)
        return _done(outcome, method, "low", _finalize(receipt, fiscal, image), rec)
    return outcome


def _vision_doubts(receipt: NormalizedReceipt, rec, ocr_text: str | None) -> list[str]:
    """Why an otherwise "balanced" vision reading should still be checked."""
    doubts: list[str] = []
    if len(receipt.items) <= 1:
        doubts.append("vision returned a single line equal to the total — "
                      "нічого перевіряти, чек піде в чернетку")
    if ocr_text:
        # The photo's own text is the cheapest fact-check there is: a total the
        # OCR never saw means the model read something that isn't on the paper.
        total = float(receipt.total_amount or 0)
        printed = {f"{total:.2f}", f"{total:.2f}".replace(".", ","),
                   f"{total:g}", f"{int(total)}" if total == int(total) else ""}
        if total > 0 and not any(form and form in ocr_text for form in printed):
            doubts.append(f"суми {total:.2f} немає в тексті, розпізнаному з фото — "
                          "можлива вигадка моделі")
    return doubts


def _stamp_network(receipt: NormalizedReceipt, net: "networks.NetworkRef | None") -> None:
    """Fill in the chain name / type from the ЄДРПОУ reference when the parse
    stage couldn't name the shop itself. Never overrides a name already set."""
    if net is None:
        return
    if not receipt.shop_name:
        receipt.shop_name = net.name
    receipt.raw = {**(receipt.raw or {}), "network_type": net.network_type,
                   "network_edrpou": net.edrpou}


def _done(outcome: ScanOutcome, method: Method, confidence: str,
          receipt: NormalizedReceipt, rec: Reconciliation) -> ScanOutcome:
    # The fiscal QR is the authoritative source for a Ukrainian receipt: it
    # yields the ДПС document itself rather than a reading of the paper. So when
    # one was visibly present and we failed to decode it, a guess from OCR or
    # vision is never trusted enough to file unattended — the user is asked to
    # re-shoot the QR instead, which turns a maybe-right parse into an exact one.
    if (outcome.qr_state == fiscal_qr_module.QR_UNREADABLE
            and method not in ("qr", "tax_cabinet")):
        confidence = "low"
        outcome.notes.append(
            "not auto-saved: a fiscal QR is present but unreadable, and it "
            "outranks this parse — re-shoot the QR close up"
        )
    outcome.method = method
    outcome.confidence = confidence
    outcome.receipt = receipt
    outcome.reconciliation = rec
    receipt.raw = {**(receipt.raw or {}), "method": method, "scan_confidence": confidence}
    return outcome


def _better(current, candidate):
    """Keep whichever draft has more line items (a proxy for completeness)."""
    if current is None:
        return candidate
    cur_items = len(current[1].items)
    new_items = len(candidate[1].items)
    return candidate if new_items > cur_items else current
