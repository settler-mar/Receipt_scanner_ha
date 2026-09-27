"""Fetch and break down a full Ukrainian fiscal receipt from the ДПС cabinet.

A decoded fiscal QR (`fiscal_qr.py`) gives us `fn` + `id` + `date` + `sum`.
For automation ДПС documents a token-authenticated public-register endpoint:

    GET https://cabinet.tax.gov.ua/ws/api_public/rro/chkAll
        ?id=<doc>&date=<...>&type=1&fn=<rro>&token=<open-part-token>

The token is created in the private cabinet under "Токени відкритої частини".
The browser viewer uses a separate endpoint protected by reCAPTCHA:

    GET https://cabinet.tax.gov.ua/ws/api_public/rro/chkAllWeb
        ?id=<doc>&date=<YYYY-MM-DD HH:MM:SS>&type=3&fn=<rro>&sm=<total>&captcha=<token>

The JSON it returns carries the receipt twice, base64-encoded:

    check     — the plain-text printed form (has the seller name/address)
    checkXml  — the structured ПРРО document (RQ/DAT/C…), windows-1251

`fetch_check` prefers the official token API. The reCAPTCHA endpoint remains a
fallback for an explicitly supplied browser token. Any failure returns None and
the pipeline keeps its QR-only result (same contract as before). The XML parser
(`parse_check_xml`) is pure and offline — feed it a file you downloaded from the
viewer and it works with no network at all.

"Рознесення" (breaking the receipt down): `FiscalCheck.allocate()` turns the raw
document into per-line amounts (gross − discount = net), tags fuel vs goods,
lifts the УКТЗЕД / VAT / excise data, and exposes the card / RRN / loyalty keys
needed to dedupe this receipt against the same purchase seen from the bank or the
fuel-network channel.

No AI, no stored credentials.
"""
from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import logging
import os
import json
import re
import xml.etree.ElementTree as ET

import httpx
from app.core.config import BASE_DIR

from app.adapters.base import NormalizedItem, NormalizedReceipt
from app.core.time import KYIV, as_utc, store_local_to_utc
from app.scanner import networks
from app.scanner.fiscal_qr import FiscalQr

logger = logging.getLogger(__name__)

_TOKEN_ENDPOINT = "https://cabinet.tax.gov.ua/ws/api_public/rro/chkAll"
_WEB_ENDPOINT = "https://cabinet.tax.gov.ua/ws/api_public/rro/chkAllWeb"
_GUARD_FILE = BASE_DIR / "data" / "dps_api_guard.json"
_DAILY_SOFT_LIMIT = 900  # keep 10% of the documented 1000/day limit in reserve

def _guard_load() -> dict:
    try: return json.loads(_GUARD_FILE.read_text("utf-8"))
    except Exception: return {}

def api_guard_status() -> dict:
    state=_guard_load(); today=dt.datetime.now(dt.timezone.utc).date().isoformat()
    if state.get("day") != today: state={"day":today,"count":0}
    return state

def _guard_before_request() -> bool:
    state=api_guard_status(); now=dt.datetime.now(dt.timezone.utc)
    blocked=state.get("blocked_until")
    if blocked:
        with __import__('contextlib').suppress(ValueError):
            if dt.datetime.fromisoformat(blocked) > now: return False
    if int(state.get("count",0)) >= _DAILY_SOFT_LIMIT: return False
    state["count"]=int(state.get("count",0))+1; state.pop("blocked_until",None)
    _GUARD_FILE.write_text(json.dumps(state,ensure_ascii=False),"utf-8"); return True

def _guard_failure(status: int | None, message: str) -> None:
    state=api_guard_status(); now=dt.datetime.now(dt.timezone.utc); cooldown=None
    low=message.lower()
    if status == 429: cooldown=dt.timedelta(hours=24)
    elif status in (401,403): cooldown=dt.timedelta(hours=6)
    elif status == 400 and ("воєнного стану" in low or "обмежено доступ" in low): cooldown=dt.timedelta(hours=6)
    elif status and status >= 500: cooldown=dt.timedelta(minutes=15)
    if cooldown: state["blocked_until"]=(now+cooldown).isoformat()
    state["last_status"]=status; state["last_error"]=message[:500]
    _GUARD_FILE.write_text(json.dumps(state,ensure_ascii=False),"utf-8")

# УКТЗЕД headings 2709/2710/2711 = crude oil, petroleum oils, petroleum gases.
_FUEL_UKTZED = ("2709", "2710", "2711")
_FUEL_NAME_RE = re.compile(
    r"\b(бензин|дизел|диз\.?пал|ДП|А-?9[25]|А-?10[05]|АІ-?9[25]|"
    r"газ\s+(нафтов|скраплен)|скраплен|пропан|бутан|LPG|СПБТ|ДТ)\b",
    re.IGNORECASE,
)
_SERVICE_NAME_RE = re.compile(r"\b(послуг|мийк|миття|шиномонтаж|оренд|доставк)\w*", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# structured model
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class CheckLine:
    position: int
    name: str
    kind: str = "goods"          # "goods" | "fuel" | "service"
    quantity: float = 1.0
    unit_price: float | None = None
    gross: float = 0.0           # line sum before its own discounts
    discount: float = 0.0        # positive = money off; negative = surcharge
    uktzed: str | None = None
    barcode: str | None = None
    excisable: bool = False
    article: str | None = None
    vat_rate: float | None = None
    tax_code: str | None = None

    @property
    def net(self) -> float:
        return round(self.gross - self.discount, 2)


@dataclasses.dataclass
class CheckPayment:
    method: str                  # "card" | "cash" | "other"
    amount: float
    label: str | None = None     # the receipt's own wording ("Б.КАРТКА")
    card_mask: str | None = None
    payment_system: str | None = None
    auth_code: str | None = None
    rrn: str | None = None
    acquirer: str | None = None
    terminal: str | None = None

    @property
    def card_last4(self) -> str | None:
        digits = re.sub(r"\D", "", self.card_mask or "")
        return digits[-4:] if len(digits) >= 4 else None


@dataclasses.dataclass
class ReconcileKeys:
    """Everything usable to match this receipt to the same purchase arriving from
    another channel (bank statement, fuel-network export)."""

    total: float
    issued_at: dt.datetime | None
    seller_edrpou: str | None = None
    card_last4: str | None = None
    rrn: str | None = None
    auth_code: str | None = None
    fuel_liters: float | None = None
    fuel_product: str | None = None
    loyalty_ids: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class FiscalCheck:
    fiscal_number: str | None
    document_id: str | None
    issued_at: dt.datetime | None          # aware UTC
    total: float
    total_discount: float = 0.0
    vat_total: float = 0.0
    vat_rate: float | None = None
    is_return: bool = False
    seller_name: str | None = None
    seller_address: str | None = None
    seller_edrpou: str | None = None
    seller_tax_number: str | None = None
    rro_serial: str | None = None
    device_model: str | None = None
    cashier: str | None = None
    store_number: str | None = None
    register: str | None = None
    shift: str | None = None
    lines: list[CheckLine] = dataclasses.field(default_factory=list)
    payments: list[CheckPayment] = dataclasses.field(default_factory=list)
    text_lines: list[str] = dataclasses.field(default_factory=list)
    header_lines: list[str] = dataclasses.field(default_factory=list)
    loyalty: dict = dataclasses.field(default_factory=dict)
    raw_url: str | None = None
    raw_xml: str | None = None
    mac: str | None = None

    # -- derived views ---------------------------------------------------- #
    @property
    def net_total(self) -> float:
        return round(sum(line.net for line in self.lines), 2)

    @property
    def reconciles(self) -> bool:
        """Do the broken-down lines add up to the printed total?"""
        return self.total > 0 and abs(self.net_total - self.total) <= 0.5 + 0.01 * self.total

    def fuel_lines(self) -> list[CheckLine]:
        return [line for line in self.lines if line.kind == "fuel"]

    def fuel_summary(self) -> dict | None:
        fuel = self.fuel_lines()
        if not fuel:
            return None
        liters = round(sum(line.quantity for line in fuel), 3)
        amount = round(sum(line.net for line in fuel), 2)
        prices = {line.unit_price for line in fuel if line.unit_price}
        return {
            "product": fuel[0].name,
            "liters": liters,
            "amount": amount,
            "price_per_liter": prices.pop() if len(prices) == 1 else (
                round(amount / liters, 2) if liters else None),
        }

    def primary_payment(self) -> CheckPayment | None:
        if not self.payments:
            return None
        return max(self.payments, key=lambda p: p.amount)

    def reconcile_keys(self) -> ReconcileKeys:
        pay = self.primary_payment()
        summary = self.fuel_summary()
        return ReconcileKeys(
            total=self.total,
            issued_at=self.issued_at,
            seller_edrpou=self.seller_edrpou,
            card_last4=pay.card_last4 if pay else None,
            rrn=pay.rrn if pay else None,
            auth_code=pay.auth_code if pay else None,
            fuel_liters=summary["liters"] if summary else None,
            fuel_product=summary["product"] if summary else None,
            loyalty_ids=[str(v) for v in self.loyalty.values() if v],
        )

    def allocate(self) -> list[dict]:
        """Flat, JSON-friendly per-line breakdown — the shape the inbox/UI uses
        to let the user file each line under a category."""
        return [
            {
                "position": line.position,
                "name": line.name,
                "kind": line.kind,
                "quantity": line.quantity,
                "unit_price": line.unit_price,
                "gross": round(line.gross, 2),
                "discount": round(line.discount, 2),
                "net": line.net,
                "uktzed": line.uktzed,
                "barcode": line.barcode,
                "excisable": line.excisable,
                "vat_rate": line.vat_rate,
            }
            for line in self.lines
        ]

    def to_normalized_receipt(self) -> NormalizedReceipt:
        pay = self.primary_payment()
        items = [
            NormalizedItem(
                name=line.name,
                total_price=line.net,
                quantity=line.quantity or 1.0,
                unit_price=line.unit_price,
                discount=round(max(line.discount, 0.0), 2),
                barcode=line.barcode,
                product_code=line.article,
            )
            for line in self.lines
        ]
        return NormalizedReceipt(
            external_id=(f"fiscal:{self.fiscal_number}:{self.document_id}"
                         if self.fiscal_number and self.document_id else ""),
            purchased_at=self.issued_at or store_local_to_utc(dt.datetime.now()),
            total_amount=self.total or self.net_total,
            currency="UAH",
            shop_name=self.seller_name,
            shop_address=self.seller_address,
            payment_type=pay.method if pay else None,
            items=items,
            total_discount=round(self.total_discount, 2) or None,
            receipt_url=self.raw_url,
            raw={
                "method": "fiscal_check",
                "fiscal_url": self.raw_url,
                "rro_serial": self.rro_serial,
                "device_model": self.device_model,
                "seller_edrpou": self.seller_edrpou,
                "seller_tax_number": self.seller_tax_number,
                "cashier": self.cashier,
                "store_number": self.store_number,
                "register": self.register,
                "shift": self.shift,
                "header_lines": self.header_lines,
                "vat_total": self.vat_total,
                "payments": [dataclasses.asdict(p) for p in self.payments],
                "loyalty": self.loyalty,
                "fuel": self.fuel_summary(),
                "mac": self.mac,
            },
        )


# --------------------------------------------------------------------------- #
# XML parsing (offline, pure)
# --------------------------------------------------------------------------- #
def _money(value) -> float:
    """ПРРО amounts are integer kopecks."""
    try:
        return round(int(str(value).strip()) / 100.0, 2)
    except (TypeError, ValueError):
        try:
            return round(float(str(value).replace(",", ".")), 2)
        except (TypeError, ValueError):
            return 0.0


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _classify(name: str, uktzed: str | None) -> str:
    if uktzed and uktzed[:4] in _FUEL_UKTZED:
        return "fuel"
    if _FUEL_NAME_RE.search(name or ""):
        return "fuel"
    if _SERVICE_NAME_RE.search(name or ""):
        return "service"
    return "goods"


def _parse_ts(value: str | None) -> dt.datetime | None:
    value = (value or "").strip()
    for fmt in ("%Y%m%d%H%M%S", "%Y%m%d%H%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return store_local_to_utc(dt.datetime.strptime(value[:len(fmt) + 4], fmt))
        except ValueError:
            continue
    return None


def parse_check_xml(data: bytes | str, *, raw_url: str | None = None,
                    plaintext: str | None = None) -> FiscalCheck:
    """Parse a ДПС fiscal document (RQ/DAT/C…). Accepts the raw windows-1251
    bytes straight from the viewer, or a decoded string."""
    if isinstance(data, str):
        data = data.encode("cp1251", "replace")
    root = ET.fromstring(data)                      # noqa: S314 — trusted gov source, no DTDs
    dat = root.find("DAT")
    body = dat.find("C") if dat is not None else None
    if dat is None or body is None:
        raise ValueError("not a fiscal check document (no DAT/C)")

    check = FiscalCheck(
        fiscal_number=(dat.get("FN") or "").strip() or None,
        document_id=None,
        issued_at=None,
        total=0.0,
        is_return=(body.get("T") or "0").strip() not in ("0", ""),
        seller_tax_number=_digits(dat.get("TN")) or None,
        rro_serial=(dat.get("ZN") or "").strip() or None,
        device_model=(root.get("NDv") or "").strip() or None,
        raw_url=raw_url,
        raw_xml=data.decode("cp1251", "replace"),
    )
    # An 8-digit TN can itself be an entity ID. A 10/12-digit VAT number is a
    # separate identifier and must never be truncated to manufacture ЄДРПОУ.
    if check.seller_tax_number and len(check.seller_tax_number) == 8:
        check.seller_edrpou = check.seller_tax_number

    lines: dict[int, CheckLine] = {}
    for el in body:
        tag = el.tag
        if tag in ("P", "PP"):
            pos = int(el.get("N") or len(lines) + 1)
            gross = _money(el.get("SM"))
            uktzed = (el.get("CZD") or "").strip() or None
            unit_price = _money(el.get("PRC")) if el.get("PRC") else None
            if unit_price:
                qty = round(gross / unit_price, 3) if unit_price else 1.0
            elif el.get("Q"):
                qty = round(int(_digits(el.get("Q")) or "1000") / 1000.0, 3)
            else:
                qty = 1.0
                unit_price = gross
            name = (el.get("NM") or "").strip()
            lines[pos] = CheckLine(
                position=pos,
                name=name,
                kind=_classify(name, uktzed),
                quantity=qty or 1.0,
                unit_price=unit_price,
                gross=gross,
                uktzed=uktzed,
                barcode=_barcode(el),
                excisable=tag == "PP",
                article=(el.get("C") or "").strip() or None,
                tax_code=(el.get("TX") or "").strip() or None,
            )
        elif tag == "D":
            target = int(el.get("NI") or 0)
            amount = _money(el.get("SM"))
            if (el.get("TY") or "0").strip() == "1":     # націнка (surcharge)
                amount = -amount
            line = lines.get(target)
            if line is not None:
                line.discount = round(line.discount + amount, 2)
        elif tag == "M":
            t = (el.get("T") or "").strip()
            method = "card" if t == "1" else "cash" if t == "0" else "other"
            check.payments.append(CheckPayment(
                method=method,
                amount=_money(el.get("SM")),
                label=(el.get("NM") or "").strip() or None,
                card_mask=(el.get("PD") or "").strip() or None,
                payment_system=(el.get("PSNM") or "").strip() or None,
                auth_code=(el.get("PE") or "").strip() or None,
                rrn=(el.get("RRN") or "").strip() or None,
                acquirer=(el.get("PA") or "").strip() or None,
                terminal=(el.get("PB") or "").strip() or None,
            ))
        elif tag == "L":
            text = (el.text or "").strip()
            if text:
                check.text_lines.append(text)
                if not lines and not check.payments:
                    check.header_lines.append(text)
        elif tag == "E":
            check.document_id = (el.get("NO") or "").strip() or check.document_id
            check.total = _money(el.get("SM"))
            check.vat_total = _money(el.get("TXSM"))
            try:
                check.vat_rate = round(float(el.get("TXPR")), 2) if el.get("TXPR") else None
            except (TypeError, ValueError):
                check.vat_rate = None
            check.issued_at = check.issued_at or _parse_ts(el.get("TS"))

            # Some РРОs (notably METRO) encode every P/D amount as the taxable
            # base, while E.SM is the amount actually paid including VAT.  Tax
            # groups on E/TX tell us how to convert each product line to the
            # same (consumer-facing) basis as the receipt total.
            tax_rates: dict[str, float] = {}
            vat_sum = 0.0
            for tax in el.findall("TX"):
                code = (tax.get("TX") or "").strip()
                try:
                    rate = float(tax.get("TXPR") or 0)
                except (TypeError, ValueError):
                    rate = 0.0
                if code:
                    tax_rates[code] = rate
                vat_sum += _money(tax.get("TXSM"))
            if tax_rates:
                check.vat_total = round(vat_sum, 2)
                if len(set(tax_rates.values())) == 1:
                    check.vat_rate = next(iter(tax_rates.values()))
                for line in lines.values():
                    line.vat_rate = tax_rates.get(line.tax_code)

    check.lines = [lines[k] for k in sorted(lines)]
    for line in check.lines:
        line.vat_rate = line.vat_rate or check.vat_rate

    # Only gross-up when the XML proves that line bases + declared VAT equal
    # E.SM. Other fiscal formats already include VAT in P.SM and remain intact.
    base_total = check.net_total
    if (check.total and check.vat_total > 0
            and abs(base_total + check.vat_total - check.total) <= 0.05):
        for line in check.lines:
            factor = 1.0 + float(line.vat_rate or 0.0) / 100.0
            line.gross = round(line.gross * factor, 2)
            line.discount = round(line.discount * factor, 2)
            if line.unit_price is not None:
                line.unit_price = round(line.unit_price * factor, 2)
        # Absorb independent per-line kopeck rounding into the final line.
        residual = round(check.total - check.net_total, 2)
        if check.lines and residual:
            check.lines[-1].gross = round(check.lines[-1].gross + residual, 2)
    check.total_discount = round(sum(max(l.discount, 0.0) for l in check.lines), 2)
    check.issued_at = check.issued_at or _parse_ts(dat.findtext("TS"))
    if not check.total:
        check.total = check.net_total

    mac = root.find("MAC")
    if mac is not None:
        check.mac = (mac.text or "").strip() or None
        # ПРРО builds that carry MAC/@ID give the document an id of their own,
        # and that is the one ДПС addresses it by — it is what the QR's
        # `cashregs/check?id=…` holds and what the cabinet names the download
        # after. E/@NO is only the receipt's number within the shift: it repeats
        # across receipts of the same till, so keying on it made two different
        # purchases look like one.
        document_id = (mac.get("ID") or "").strip()
        if document_id:
            check.document_id = document_id

    _apply_loyalty(check)
    if plaintext:
        _apply_plaintext_header(check, plaintext)
    _apply_xml_header(check)
    _apply_network(check)
    return check


_HDR_META_RE = re.compile(
    r"^\s*(каса|чек|зм[іi]на|z\b|ts\s*=|фн|пн|[іi]д|касовий|ф[іi]скальн|"
    r"артикул|штрих)", re.IGNORECASE)


def _barcode(el) -> str | None:
    """EAN/UPC printed next to the item. ПРРО builds differ in the attribute
    name (`CD` on KBM/FP devices, `BC`/`BAR` elsewhere)."""
    for attr in ("CD", "BC", "BAR", "EAN"):
        value = _digits(el.get(attr))
        if len(value) in (8, 12, 13, 14):
            return value
    return None


def _apply_xml_header(check: FiscalCheck) -> None:
    """Recover seller/point-of-sale data from the printed header the ПРРО put
    into the leading <L> rows. Receipts fetched with the plain-text form get a
    better header later; this is the only source when the XML arrives alone."""
    rows: list[str] = []
    for row in check.header_lines:
        if _HDR_META_RE.match(row):
            if m := re.search(r"^\s*каса\D*([0-9]+)", row, re.IGNORECASE):
                check.register = check.register or m.group(1)
            if m := re.search(r"\bZ\s*([0-9]+)", row, re.IGNORECASE):
                check.shift = check.shift or m.group(1)
            continue
        rows.append(row)
    if not rows:
        return
    first = rows[0]
    cashier_row = False
    if m := re.match(r"^\s*([0-9]{2,})\s+(.+?)\s+([0-9]{1,3})\s*$", first):
        # "<точка> Прізвище І. Б. <код касира>" — the operator, not the seller.
        check.store_number = check.store_number or m.group(1)
        check.cashier = check.cashier or m.group(2).strip()
        cashier_row = True
    elif m := re.match(r"^\s*([0-9]{2,})\s+(.+?)\s*$", first):
        check.store_number = check.store_number or m.group(1)
        first = m.group(2)
    first = first.strip(' "')
    if first and not cashier_row and not check.seller_name:
        check.seller_name = first
    tail = [r.strip(' "') for r in rows[1:] if r.strip(' "')]
    if tail and not check.seller_address:
        check.seller_address = " ".join(tail)


def _apply_network(check: FiscalCheck) -> None:
    """A ПРРО document carries no shop sign — only the seller's ПН/ЄДРПОУ. Turn
    those into the brand the user actually shopped at when we know the pair."""
    identity = networks.by_tax_number(check.seller_tax_number or "")
    if identity:
        network, entity = identity
        if entity and not check.seller_edrpou:
            check.seller_edrpou = entity
    else:
        network = networks.by_edrpou(check.seller_edrpou or "")
    if network and not check.seller_name:
        check.seller_name = network.name


def _apply_loyalty(check: FiscalCheck) -> None:
    blob = "\n".join(check.text_lines)
    if m := re.search(r"LoyaltyID:\s*([0-9]+)", blob, re.IGNORECASE):
        check.loyalty["loyalty_id"] = m.group(1)
    if m := re.search(r"Кл[іi][єe]нт:\s*([0-9*]{6,})", blob):
        check.loyalty["client_card"] = m.group(1)
    if m := re.search(r"(?:знижка|програма|program)\s+([A-ZА-ЯЇІ]{3,})", blob, re.IGNORECASE):
        check.loyalty["program"] = m.group(1).upper()


def _apply_plaintext_header(check: FiscalCheck, plaintext: str) -> None:
    stop = re.compile(r"^\s*(ІД|IД|ПН|ПНН|Касовий|КАСОВИЙ|РРО|ЧЕК|ФН)\b", re.IGNORECASE)
    rows = [r.strip() for r in plaintext.splitlines()]
    header: list[str] = []
    for row in rows:
        if not row:
            continue
        if stop.match(row):
            if m := re.search(r"\b([0-9]{8,10})\b", row):
                if row.upper().lstrip().startswith(("ІД", "IД")):
                    check.seller_edrpou = m.group(1)
            break
        if set(row) <= {"-", "=", "*", " "}:
            break
        header.append(row)
    if header and not check.seller_name:
        check.seller_name = header[0].replace('"', "").strip()
    if len(header) > 1 and not check.seller_address:
        check.seller_address = " ".join(header[1:]).replace('"', "").strip()


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #
async def fetch_check(
    *,
    fiscal_number: str,
    document_id: str,
    issued_at: dt.datetime | None,
    total: float,
    api_token: str | None = None,
    captcha_token: str | None = None,
    timeout: float = 10.0,
    client: httpx.AsyncClient | None = None,
) -> FiscalCheck | None:
    """Pull the full receipt from ДПС.

    Prefer the documented open-register API (`api_token`, or the
    RS_TAX_CABINET_TOKEN environment variable). The browser/reCAPTCHA endpoint
    is used only when `captcha_token` is explicitly supplied. Best-effort:
    returns None on transport/auth/shape failure.
    """
    if not (fiscal_number and document_id):
        return None
    local = (as_utc(issued_at) or dt.datetime.now(dt.timezone.utc)).astimezone(KYIV)
    common = {
        "id": document_id,
        "date": local.strftime("%Y-%m-%d %H:%M:%S"),
        "fn": fiscal_number,
    }
    api_token = api_token or os.getenv("RS_TAX_CABINET_TOKEN")
    if api_token:
        if client is None and not _guard_before_request():
            logger.warning("tax cabinet API skipped by safety circuit breaker")
            return None
        endpoint = _TOKEN_ENDPOINT
        # type=1 asks for the original XML. Current responses may also include
        # checkXml; _payload_xml handles both documented shapes.
        params = {**common, "type": "1", "token": api_token}
    elif captcha_token:
        endpoint = _WEB_ENDPOINT
        params = {
            **common, "type": "3", "sm": f"{total:.2f}",
            "captcha": captcha_token,
        }
    else:
        logger.info("tax cabinet lookup skipped: no API or captcha token")
        return None
    owns = client is None
    client = client or httpx.AsyncClient(timeout=timeout, headers={"Accept": "application/json"})
    try:
        resp = await client.get(endpoint, params=params)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001 — best effort only
        response=getattr(exc,"response",None)
        if owns:
            _guard_failure(getattr(response,"status_code",None), getattr(response,"text","") or str(exc))
        logger.info("tax cabinet lookup failed for fn=%s id=%s: %s",
                    fiscal_number, document_id, exc)
        return None
    finally:
        if owns:
            await client.aclose()

    if str(payload.get("resultCode") or "0") not in ("0", "None"):
        logger.info("tax cabinet returned resultCode=%s (%s) for fn=%s id=%s",
                    payload.get("resultCode"), payload.get("resultText"),
                    fiscal_number, document_id)
        return None

    # chkAll (documented API) returns the requested original XML in `check`;
    # chkAllWeb returns it in `checkXml` and the printable text in `check`.
    xml_b64 = payload.get("checkXml") or payload.get("check")
    if not xml_b64:
        return None
    try:
        xml_bytes = base64.b64decode(xml_b64)
        plaintext = None
        if payload.get("checkXml") and payload.get("check"):
            plaintext = base64.b64decode(payload["check"]).decode("cp1251", "replace")
        raw_url = (
            f"https://cabinet.tax.gov.ua/cashregs/check?id={document_id}"
            f"&fn={fiscal_number}&sm={total:.2f}"
        )
        return parse_check_xml(xml_bytes, raw_url=raw_url, plaintext=plaintext)
    except Exception:  # noqa: BLE001 — unexpected shape
        logger.warning("tax cabinet response could not be parsed", exc_info=True)
        return None


async def fetch_structured(qr: FiscalQr, *, api_token: str | None = None,
                           captcha_token: str | None = None,
                           timeout: float = 10.0) -> NormalizedReceipt | None:
    """Adapter used by the scan pipeline: FiscalQr -> NormalizedReceipt | None."""
    if not (qr.fiscal_number and qr.document_id):
        return None
    check = await fetch_check(
        fiscal_number=qr.fiscal_number,
        document_id=qr.document_id,
        issued_at=qr.issued_at,
        total=qr.total_amount or 0.0,
        api_token=api_token,
        captcha_token=captcha_token,
        timeout=timeout,
    )
    return check.to_normalized_receipt() if check else None
