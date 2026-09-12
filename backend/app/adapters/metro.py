"""METRO UA Scan & Go receipt integration.

The cart/card scan is a one-time server-side Shopper registration, not an
authentication step, so it deliberately does not appear in our UI.
"""
from __future__ import annotations

import base64
import datetime as dt
import io
import re
import uuid
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from html.parser import HTMLParser

from pypdf import PdfReader

from app.adapters.base import AdapterError, AuthResult, BaseAdapter, NormalizedItem, NormalizedReceipt
from app.core.http import AsyncClient

BASE_URL = "https://metroua.supersmart.me/services"
APP_VERSION = "1.0.150a"
HEADERS = {"Accept": "application/json", "Content-Type": "application/json; charset=utf-8", "User-Agent": "okhttp/4.9.2"}


def _payload(data: dict) -> dict:
    return data.get("result") or data.get("data") or data


def _parse_time(value: object) -> dt.datetime:
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
        return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc)
    parsed = dt.datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed.astimezone(dt.timezone.utc)


def _money(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[^0-9,.-]", "", str(value or "0")).replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return 0.0


def _cart_barcode(value: str) -> str:
    """Expand a human cart number while preserving arbitrary raw barcodes.

    Observed METRO UA labels use ``74300011 + NNNNN + 743``.  A value longer
    than five digits (or containing non-digits) is treated as an already full,
    store-issued code and sent unchanged.
    """
    value = value.strip()
    if value.isdigit() and 1 <= len(value) <= 5:
        number = int(value)
        if number <= 0:
            raise AdapterError("METRO: номер візка має бути більшим за нуль")
        return f"74300011{number:05d}743"
    if value:
        return value
    raise AdapterError("METRO: введіть номер або повний код візка")


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() == "tr":
            self._row = []
        elif tag.lower() in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag.lower() == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def _invoice_items(invoice: dict) -> list[NormalizedItem]:
    direct = invoice.get("items") or invoice.get("products") or invoice.get("lines")
    if isinstance(direct, list):
        result = []
        for row in direct:
            if not isinstance(row, dict):
                continue
            name = row.get("name") or row.get("description") or row.get("itemName")
            if not name:
                continue
            qty = _money(row.get("quantity", 1)) or 1
            total = _money(row.get("totalPrice", row.get("total", row.get("amount", 0))))
            unit_price = _money(row.get("unitPrice", row.get("price", 0))) or (total / qty if qty else total)
            result.append(NormalizedItem(name=str(name), quantity=qty, unit_price=unit_price,
                                         total_price=total, barcode=str(row.get("barcode")) if row.get("barcode") else None,
                                         discount=_money(row.get("discount", 0))))
        return result

    encoded = invoice.get("invoice") or invoice.get("html")
    if not encoded:
        return []
    if invoice.get("invoice"):
        try:
            decoded = base64.b64decode(encoded)
        except (ValueError, TypeError):
            decoded = b""
        if str(invoice.get("invoiceFormat", "")).upper() == "PDF" or decoded.startswith(b"%PDF"):
            try:
                text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(decoded)).pages)
            except Exception:
                return []
            return _pdf_invoice_items(text)
        html = decoded.decode("utf-8", errors="replace")
    else:
        html = str(encoded)
    if "<" not in html:
        return []
    parser = _TableParser()
    parser.feed(html)
    items: list[NormalizedItem] = []
    for cells in parser.rows:
        if len(cells) < 3 or not cells[0] or not re.search(r"\d", cells[-1]):
            continue
        numeric = [_money(cell) for cell in cells[1:]]
        total = numeric[-1]
        if total <= 0 or any(word in cells[0].lower() for word in ("разом", "всього", "total", "пдв")):
            continue
        qty = numeric[-3] if len(numeric) >= 3 and numeric[-3] > 0 else 1
        unit_price = numeric[-2] if len(numeric) >= 2 and numeric[-2] > 0 else total / qty
        items.append(NormalizedItem(name=cells[0], quantity=qty, unit_price=unit_price, total_price=total))
    return items


def _pdf_invoice_items(text: str) -> list[NormalizedItem]:
    """Parse the product rows from METRO UA's invoice PDF text layer."""
    row_pattern = re.compile(
        r"\d{2}\.\d{2}\.\d{4}\s+(\d{7}):\s+(.+?)\s+\.\s+"
        r"\d{10}\s+\S+\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s*$"
    )
    items: list[NormalizedItem] = []
    for raw_line in text.splitlines():
        line = " ".join(raw_line.split())
        match = row_pattern.search(line)
        if not match:
            continue
        product_code, name, quantity, unit_price, total_price = match.groups()
        items.append(NormalizedItem(
            name=name.strip(),
            quantity=_money(quantity),
            unit_price=_money(unit_price),
            total_price=_money(total_price),
            barcode=product_code,
        ))
    return items


def _distribute_receipt_total(items: list[NormalizedItem], receipt_total: object) -> list[NormalizedItem]:
    """Gross up METRO's net PDF rows so their cents sum to the receipt total.

    METRO exposes only the aggregate tax in the invoice.  Allocate it
    proportionally using largest remainders; this avoids a rounding drift while
    keeping the final line sum exactly equal to the charged amount.
    """
    if not items:
        return items
    net = [Decimal(str(item.total_price)) for item in items]
    net_total = sum(net, Decimal("0"))
    gross_cents = int((Decimal(str(_money(receipt_total))) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if net_total <= 0 or gross_cents <= 0:
        return items

    exact = [Decimal(gross_cents) * value / net_total for value in net]
    cents = [int(value.to_integral_value(rounding=ROUND_FLOOR)) for value in exact]
    remainder = gross_cents - sum(cents)
    order = sorted(range(len(items)), key=lambda index: (exact[index] - cents[index], -index), reverse=True)
    for index in order[:remainder]:
        cents[index] += 1

    for item, item_cents in zip(items, cents):
        item.total_price = item_cents / 100
        if item.quantity:
            item.unit_price = float(
                (Decimal(item_cents) / 100 / Decimal(str(item.quantity))).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            )
    return items


class MetroAdapter(BaseAdapter):
    store_code = "metro"

    async def login_with_device_uuid(self, device_uuid: str) -> AuthResult:
        device_uuid = device_uuid.strip()
        if not re.fullmatch(r"[0-9a-fA-F-]{32,36}", device_uuid):
            raise AdapterError("METRO: некоректний ID пристрою")
        body = {"uuid": device_uuid, "appVersion": APP_VERSION, "deviceType": "Android",
                "location": None, "macAddress": None}
        async with AsyncClient(timeout=20, headers=HEADERS) as client:
            response = await client.post(f"{BASE_URL}/v4/customer/signin", json=body)
        if response.status_code != 200:
            raise AdapterError(f"METRO: вхід повернув {response.status_code}")
        data = _payload(response.json())
        device = data.get("device") or data
        token = device.get("token")
        customer_id = device.get("customerId") or device_uuid
        device_id = device.get("id") or device.get("deviceId")
        if not token:
            raise AdapterError("METRO: сервер не повернув токен")
        return AuthResult(access_token=token, extra={"customerId": customer_id, "device_uuid": device_uuid,
                                                     "device_id": device_id})

    async def login_with_device_registration(self, cart_barcode: str, card_barcode: str) -> AuthResult:
        """Mirror METRO's first-run flow: sign in, attach cart, validate member card.

        The generated UUID is persisted only after the whole flow succeeds.  On
        subsequent collections neither physical code is needed again.
        """
        cart_barcode = _cart_barcode(cart_barcode)
        card_barcode = card_barcode.strip()
        if not cart_barcode or not card_barcode:
            raise AdapterError("METRO: відскануйте код візка та картки клієнта")
        device_uuid = str(uuid.uuid4())
        session = await self.login_with_device_uuid(device_uuid)
        token = session.access_token or ""
        device_id = session.extra.get("device_id")
        # login_with_device_uuid accepts old server responses too; registration
        # specifically needs the numeric device id returned by current v4.
        if device_id is None:
            raise AdapterError("METRO: сервер не повернув ID нової сесії")
        headers = {**HEADERS, "Authorization": f"Bearer {token}"}
        async with AsyncClient(timeout=25, headers=headers) as client:
            await client.post(f"{BASE_URL}/v4/order/lookup", json={
                "deviceId": device_id, "orderId": None, "storeId": None, "cartBarcode": None,
            })
            cart_response = await client.post(f"{BASE_URL}/v4/order/scan/cart", json={
                "orderId": None, "deviceId": device_id, "cartBarcode": cart_barcode, "action": 1,
            })
            if cart_response.status_code != 200:
                raise AdapterError(f"METRO: код візка відхилено ({cart_response.status_code})")
            cart_data = _payload(cart_response.json())
            order = cart_data.get("order") or cart_data
            order_id = order.get("id") or order.get("orderId")
            if order_id is None:
                raise AdapterError("METRO: після сканування візка не створено замовлення")
            card_response = await client.post(f"{BASE_URL}/v4/validate/customer", json={
                "customerId": card_barcode,
                "memberId": None,
                "phoneNumber": None,
                "orderId": int(order_id),
                "deviceId": device_id,
                "memberIdentifierType": "MEMBER_CARD",
                "entryMethod": {"action": "SCANNED", "reason": "ORDER"},
                "memberCardResponse": None,
            })
            if card_response.status_code != 200:
                raise AdapterError(f"METRO: картку клієнта відхилено ({card_response.status_code})")
            # Despite its name, /historical/paid's customerId is the scanned
            # loyalty-card barcode.  Sending the installation UUID makes METRO
            # answer with HTTP 500 / internal code 412 ("Shopper not found").
            # Keep the exact user input: leading zeroes are significant.
            registered_customer_id = card_barcode
        session.extra.update({
            "customerId": registered_customer_id,
            "device_uuid": device_uuid,
            "device_id": device_id,
        })
        return session

    async def fetch_receipts(self, access_token: str, extra: dict,
                             since: dt.datetime | None = None) -> list[NormalizedReceipt]:
        customer_id = extra.get("customerId") or extra.get("customer_id")
        if not customer_id:
            raise AdapterError("METRO: у сесії відсутній customerId")
        headers = {**HEADERS, "Authorization": f"Bearer {access_token}"}
        async with AsyncClient(timeout=25, headers=headers) as client:
            response = await client.post(f"{BASE_URL}/v4/order/historical/paid",
                                         json={"id": 0, "customerId": customer_id})
            if response.status_code in {401, 403}:
                raise AdapterError("METRO: сесія прострочена — підключіть пристрій повторно")
            if response.status_code != 200:
                raise AdapterError(f"METRO: історія покупок повернула {response.status_code}")
            history = _payload(response.json())
            orders = history.get("orders") if isinstance(history, dict) else history
            receipts: list[NormalizedReceipt] = []
            for order in orders or []:
                order_id = order.get("id") or order.get("orderId")
                if order_id is None or order.get("checkoutTime") is None:
                    continue
                purchased_at = _parse_time(order["checkoutTime"])
                if since and purchased_at < since:
                    continue
                invoice_response = await client.post(f"{BASE_URL}/v6/invoice",
                                                     json={"orderId": int(order_id), "languageCode": "uk"})
                invoice: dict = {}
                if invoice_response.status_code == 200:
                    raw_invoice = _payload(invoice_response.json())
                    invoice = raw_invoice if isinstance(raw_invoice, dict) else {}
                total_amount = _money(order.get("totalPrice", order.get("total", 0)))
                items = _distribute_receipt_total(_invoice_items(invoice), total_amount)
                receipts.append(NormalizedReceipt(external_id=str(order_id), purchased_at=purchased_at,
                                                  total_amount=total_amount,
                                                  shop_name="METRO", items=items,
                                                  raw={"order": order, "invoice": invoice}))
        return receipts
