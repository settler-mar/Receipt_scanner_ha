"""Parser for the `<CHECK>` XML the ДПС cabinet hands out for a single receipt.

This is the file you get from the receipt viewer's own download, and it is a
different document from the two shapes `fiscal_check.py` handles: those are the
ПРРО fiscal document (`<RQ>`) and the public API envelope (`<Chk>`). This one is
already decoded and flat — head, total, payments, lines — so it maps straight
onto a NormalizedReceipt without the kopeck arithmetic the ПРРО format needs.

It carries no fiscal document number, only the registrar number and a document
UID, so it cannot produce the `fiscal:<fn>:<id>` key a QR scan does. The UID is
stable per receipt, which keeps re-imports idempotent; matching it against a
copy of the same receipt filed from its QR is left to manual_dedup.
"""
from __future__ import annotations

import datetime as dt
import re
import xml.etree.ElementTree as ET

from app.adapters.base import NormalizedItem, NormalizedReceipt
from app.core.time import store_local_to_utc

_DECLARATION_RE = re.compile(r"^\s*<\?xml[^>]*\?>", re.IGNORECASE)

#: PAYFORMCD is not consistent between ПРРО vendors, so the printed name decides.
_CARD_WORDS = ("картк", "безготів", "картой", "card")
_CASH_WORDS = ("готівк", "наличн", "cash")


def _text(node: ET.Element | None, tag: str) -> str:
    if node is None:
        return ""
    return (node.findtext(tag) or "").strip()


def _number(raw: str, places: int) -> float | None:
    raw = raw.replace(" ", "").replace("\xa0", "").replace(",", ".")
    if not raw:
        return None
    try:
        return round(float(raw), places)
    except ValueError:
        return None


def _money(raw: str) -> float | None:
    return _number(raw, 2)


def _quantity(raw: str) -> float | None:
    """Weighed goods carry three decimals (0.554 kg) — money rounding loses them."""
    return _number(raw, 3)


def _issued_at(head: ET.Element | None) -> dt.datetime | None:
    """ORDERDATE is ddmmyyyy and ORDERTIME hhmmss, both in store-local time."""
    date_s, time_s = _text(head, "ORDERDATE"), _text(head, "ORDERTIME")
    if len(date_s) != 8:
        return None
    try:
        day = dt.datetime.strptime(date_s, "%d%m%Y").date()
    except ValueError:
        return None
    try:
        moment = dt.datetime.strptime((time_s + "000000")[:6], "%H%M%S").time()
    except ValueError:
        moment = dt.time()
    return store_local_to_utc(dt.datetime.combine(day, moment))


def _payment_type(root: ET.Element) -> str | None:
    for row in root.findall("./CHECKPAY/ROW"):
        name = _text(row, "PAYFORMNM").casefold()
        if any(word in name for word in _CARD_WORDS):
            return "card"
        if any(word in name for word in _CASH_WORDS):
            return "cash"
    return None


def _items(root: ET.Element) -> list[NormalizedItem]:
    items: list[NormalizedItem] = []
    for row in root.findall("./CHECKBODY/ROW"):
        name = " ".join(_text(row, "NAME").split())
        cost = _money(_text(row, "COST"))
        if not name or cost is None:
            continue
        discount = _money(_text(row, "DISCOUNTSUM")) or 0.0
        items.append(NormalizedItem(
            name=name,
            # COST is the gross line; NormalizedItem.total_price is what was
            # actually charged, which is the same thing net of any line discount.
            total_price=round(cost - discount, 2),
            quantity=_quantity(_text(row, "AMOUNT")) or 1.0,
            unit_price=_money(_text(row, "PRICE")),
            discount=discount,
        ))
    return items


def parse_cabinet_check_xml(data: bytes | str) -> NormalizedReceipt | None:
    """Return the receipt described by a `<CHECK>` document, or None.

    None means "this is not that format" — the caller is expected to try the
    other ДПС shapes before giving up.
    """
    try:
        if isinstance(data, bytes):
            # Raw file: the declaration says windows-1251, so let ElementTree
            # apply it rather than guessing.
            root = ET.fromstring(data)
        else:
            # Already decoded upstream. ElementTree refuses a str that still
            # carries an encoding declaration, and re-encoding to cp1251 would
            # mangle anything outside it — drop the declaration instead.
            root = ET.fromstring(_DECLARATION_RE.sub("", data, count=1).lstrip())
    except ET.ParseError:
        return None

    if root.tag != "CHECK":
        return None
    head = root.find("CHECKHEAD")
    issued_at = _issued_at(head)
    total = _money(_text(root.find("CHECKTOTAL"), "SUM"))
    items = _items(root)
    if issued_at is None or total is None:
        return None

    registrar, uid = _text(head, "CASHREGISTERNUM"), _text(head, "UID")
    order_num = _text(head, "ORDERNUM")
    # Prefer the document UID: it is stable per receipt, so re-importing the same
    # file updates one row instead of adding another.
    identity = uid or f"{registrar}:{order_num}"
    shop_name = _text(head, "POINTNM") or _text(head, "ORGNM") or None

    return NormalizedReceipt(
        external_id=f"cabinet-check:{identity}",
        purchased_at=issued_at,
        total_amount=total,
        shop_name=shop_name,
        shop_address=_text(head, "POINTADDR") or None,
        payment_type=_payment_type(root),
        items=items,
        total_discount=_money(_text(root.find("CHECKTOTAL"), "DISCOUNTSUM")),
        raw={
            "method": "cabinet_check_xml",
            "seller_name": _text(head, "ORGNM") or None,
            "seller_tax_number": _text(head, "TIN") or None,
            "fiscal_number": registrar or None,
            "order_number": order_num or None,
            "document_uid": uid or None,
        },
    )
