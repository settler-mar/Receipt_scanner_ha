"""Structured parser for public Vchasno.Kasa receipt viewer pages."""
from __future__ import annotations

import datetime as dt
import re
from html.parser import HTMLParser

from app.adapters.base import NormalizedItem, NormalizedReceipt
from app.core.time import store_local_to_utc

_MONEY_RE = re.compile(r"(-?\d[\d ]*[.,]\d{2})")
_QTY_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*[xх×]\s*(\d+(?:[.,]\d+)?)", re.I)
_DATE_RE = re.compile(r"(\d{2})[.]?(\d{2})[.]?(\d{4})\s+(\d{2})[-:](\d{2})[-:](\d{2})")
_FN_RE = re.compile(r"ФН\s+(?:ПРРО\s+)?([A-Za-z0-9_-]+)", re.I)
_CHECK_RE = re.compile(r"ЧЕК\s*№\s*([A-Za-z0-9_-]+)", re.I)


def _number(value: str) -> float:
    return float(value.replace(" ", "").replace(",", "."))


class _ReceiptHtml(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.current_cell: list[str] | None = None
        self.current_row: list[str] | None = None
        self.current_block: list[str] | None = None
        self.rows: list[list[str]] = []
        self.blocks: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"style", "script", "svg"}:
            self.skip += 1
        if self.skip:
            return
        if tag == "tr":
            self.current_row = []
        elif tag == "td" and self.current_row is not None:
            self.current_cell = []
        elif tag in {"p", "h4"}:
            self.current_block = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"style", "script", "svg"} and self.skip:
            self.skip -= 1
            return
        if self.skip:
            return
        if tag == "td" and self.current_cell is not None and self.current_row is not None:
            self.current_row.append(" ".join(self.current_cell).strip())
            self.current_cell = None
        elif tag == "tr" and self.current_row is not None:
            if any(self.current_row):
                self.rows.append(self.current_row)
            self.current_row = None
        elif tag in {"p", "h4"} and self.current_block is not None:
            value = " ".join(self.current_block).strip()
            if value:
                self.blocks.append(value)
            self.current_block = None

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        value = " ".join(data.split())
        if not value:
            return
        if self.current_cell is not None:
            self.current_cell.append(value)
        if self.current_block is not None:
            self.current_block.append(value)


def parse_vchasno_html(page: str, *, source_url: str) -> NormalizedReceipt | None:
    if "kasa.vchasno" not in source_url.lower():
        return None
    parser = _ReceiptHtml()
    parser.feed(page)
    joined = "\n".join(parser.blocks)

    fn_match = _FN_RE.search(joined)
    check_match = _CHECK_RE.search(joined)
    date_match = _DATE_RE.search(joined)
    if not check_match or not date_match:
        return None

    day, month, year, hour, minute, second = map(int, date_match.groups())
    purchased_at = store_local_to_utc(dt.datetime(year, month, day, hour, minute, second))

    items: list[NormalizedItem] = []
    pending: tuple[float, float] | None = None
    for cells in parser.rows:
        values = [cell for cell in cells if cell]
        if not values:
            continue
        qty_match = _QTY_RE.fullmatch(values[0])
        if qty_match:
            pending = (_number(qty_match.group(1)), _number(qty_match.group(2)))
            continue
        if pending is None or len(values) < 2:
            continue
        amount_match = _MONEY_RE.search(values[-1])
        if not amount_match:
            continue
        name = values[0].strip()
        if name.casefold() in {"сума", "до сплати", "безготівкова", "готівка"}:
            pending = None
            continue
        quantity, unit_price = pending
        items.append(NormalizedItem(
            name=name, quantity=quantity, unit_price=unit_price,
            total_price=_number(amount_match.group(1)),
        ))
        pending = None

    total = None
    for cells in parser.rows:
        values = [cell for cell in cells if cell]
        if len(values) >= 2 and values[0].strip().casefold() == "сума":
            match = _MONEY_RE.search(values[-1])
            if match:
                total = _number(match.group(1))
                break
    if total is None:
        total = round(sum(item.total_price for item in items), 2)
    if not items or not total:
        return None

    seller = parser.blocks[0] if parser.blocks else None
    shop = next((line for line in parser.blocks if line.upper().startswith(("МАГАЗИН ", "АЗС ", "КАФЕ "))), None)
    fn = fn_match.group(1) if fn_match else None
    check_id = check_match.group(1)
    payment = "card" if any("безготів" in " ".join(row).casefold() for row in parser.rows) else None
    external_id = f"fiscal:{fn}:{check_id}" if fn else f"vchasno:{check_id}"
    return NormalizedReceipt(
        external_id=external_id,
        purchased_at=purchased_at,
        total_amount=total,
        shop_name=shop or seller,
        payment_type=payment,
        receipt_url=source_url,
        items=items,
        raw={"source": "vchasno_html", "seller_name": seller, "fiscal_number": fn,
             "document_id": check_id},
    )
