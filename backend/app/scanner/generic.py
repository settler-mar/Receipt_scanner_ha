"""Stage 3 — layout-agnostic structural parse of local OCR text (no AI).

For receipts from a chain we have no template for. Pure heuristics:
  * the total is the amount next to a "СУМА / ДО СПЛАТИ / УСЬОГО / TOTAL" line;
  * a line item is "<text> ... <number>" where the number looks like money and
    the text has at least one letter — with an optional "<qty> x <price>" tail.

Deliberately conservative: it would rather return a low-confidence draft (or
nothing) than a plausible-looking wrong one. reconcile.py is the gate.
"""
from __future__ import annotations

import datetime as dt
import re

from app.adapters.base import NormalizedItem, NormalizedReceipt
from app.core.time import store_local_to_utc

_MONEY = r"-?\d{1,3}(?:[  ]?\d{3})*[.,]\d{2}"
_TOTAL_RE = re.compile(
    r"(?:сума\s+до\s+сплати|до\s+сплати|усього|разом|всього|total|сума)\D{0,6}(" + _MONEY + r")",
    re.IGNORECASE,
)
_DISCOUNT_RE = re.compile(r"знижк\w*\D{0,6}(" + _MONEY + r")", re.IGNORECASE)
_DATETIME_RE = re.compile(r"(\d{2}[./]\d{2}[./]\d{2,4})[^\d]{1,4}(\d{2}:\d{2}(?::\d{2})?)")
_QTY_PRICE_RE = re.compile(
    r"(?P<qty>\d+(?:[.,]\d{1,3})?)\s*[xхХ*]\s*(?P<price>" + _MONEY + r")"
)
_TRAILING_MONEY_RE = re.compile(r"(?P<sum>" + _MONEY + r")\s*[А-ГA-Z]?\s*$")
_HAS_LETTER = re.compile(r"[A-Za-zА-Яа-яЇїІіЄєҐґ]")
_SKIP_LINE = re.compile(
    r"^\s*(?:сума|усього|разом|всього|до\s+сплати|пдв|податк|касир|чек|"
    r"дякуємо|решта|готівка|картка|безготів|бонус|знижк|акциз|фіскальн|"
    r"тов\b|фоп\b|єдрпоу|ідент|адреса)",
    re.IGNORECASE,
)


def _money(raw: str) -> float | None:
    raw = raw.replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return round(float(raw), 2)
    except ValueError:
        return None


def _parse_dt(text: str) -> dt.datetime | None:
    m = _DATETIME_RE.search(text)
    if not m:
        return None
    date_s, time_s = m.group(1).replace("/", "."), m.group(2)
    for fmt in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            d = dt.datetime.strptime(date_s, fmt).date()
            break
        except ValueError:
            continue
    else:
        return None
    parts = [int(p) for p in time_s.split(":")]
    return store_local_to_utc(dt.datetime.combine(d, dt.time(*(parts + [0, 0])[:3])))


def parse_generic(text: str) -> NormalizedReceipt | None:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None

    total_m = _TOTAL_RE.search(text)
    total = _money(total_m.group(1)) if total_m else None
    discount_m = _DISCOUNT_RE.search(text)
    discount = _money(discount_m.group(1)) if discount_m else None

    items: list[NormalizedItem] = []
    for line in lines:
        if _SKIP_LINE.search(line) or not _HAS_LETTER.search(line):
            continue
        tail = _TRAILING_MONEY_RE.search(line)
        if not tail:
            continue
        line_sum = _money(tail.group("sum"))
        if line_sum is None or line_sum <= 0:
            continue
        head = line[: tail.start()].strip(" .-·|")
        qty = 1.0
        price = None
        qp = _QTY_PRICE_RE.search(head)
        if qp:
            qty = _money(qp.group("qty")) or 1.0
            price = _money(qp.group("price"))
            head = head[: qp.start()].strip(" .-·|")
        if not _HAS_LETTER.search(head) or len(head) < 2:
            continue
        items.append(NormalizedItem(
            name=head, total_price=line_sum, quantity=qty, unit_price=price,
        ))

    if total is None and not items:
        return None
    # A total summed from the items reconciles against those same items by
    # construction, which would let a garbled OCR pass as high confidence. Flag
    # it so reconcile.py can refuse to treat the check as evidence.
    total_inferred = total is None
    if total_inferred:
        total = round(sum(it.total_price for it in items), 2)

    return NormalizedReceipt(
        external_id="",
        purchased_at=_parse_dt(text) or store_local_to_utc(dt.datetime.now()),
        total_amount=total,
        total_discount=discount,
        items=items,
        raw={"method": "generic", "ocr_excerpt": text[:2000],
             "total_inferred": total_inferred},
    )
