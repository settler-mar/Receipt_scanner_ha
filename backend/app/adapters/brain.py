"""Brain (brain.com.ua) buyer-cabinet adapter.

Brain has no documented buyer API. The authenticated cabinet is a cookie-backed,
server-rendered page (jQuery + Yii-style backend). Reverse-engineered live
against a real account:

  * Login:  POST /api/v1/login   (jQuery $.ajax, form-encoded)
            data: {phone_number, password}
            header: X-CSRF-Token: <meta name="csrftoken"> from any HTML page —
               MANDATORY, the endpoint 403s without it.
            -> JSON {"Status": 1, "RedirectTo": ...} on success;
               {"Status": 0, "Error": "<message>"} on bad credentials.
               The session is a set of HttpOnly cookies —
               ``curl_cffi`` captures them, so the whole cookie jar is what we
               persist (serialized to JSON as the encrypted "access token",
               same as adapters/vencon.py). The submitted password is never
               stored.
  * Orders: GET /cabinet/dashboard/  — the order list *and* every order's line
            items, warranty flag and delivery/payment block are all rendered
            into this one page (no per-order detail request). Markup:
              div.order-item[data-order-id]                 (internal order id)
                div.br-prof-table-order                     display number
                div.br-prof-table-date       "от 07.09.2026"
                div.br-prof-table-amount     "999₴"
                div.br-prof-table-status ... span            status text
                div.br-prof-table-hidden
                  div.br-prof-th-item[data-product-code][data-order-line-id]
                    a.link-hov-text[href]                    product name + URL
                    div.br-prof-th-price / .br-prof-th-quantity
                    div.br-prof-th-warranty .warranty-wrapper  (=> "Гарталон",
                       a downloadable warranty card exists for this line)
                    div.br-prof-th-img img[data-observe-src]  product image
                  div.br-prof-th-info                        delivery + payment
  * Warranty card:  GET /api/v1/myorder/warranty/{order-id}/{product-code}/
  * Invoice/чек:    GET /api/v1/myorder/invoice/{order-id}/

Warranty *duration* is not on the cabinet page — it lives on the product card
("Гарантия: 24 месяца" / JSON "Warranty":"24"), same pattern as Foxtrot/Vencon,
so items are enriched by fetching their product page.

Note on bot protection: the public site carries a JS anti-bot check on some
paths. ``app.core.http.AsyncClient`` (curl_cffi, chrome impersonation) plus a
homepage warmup has been enough in testing; if Brain ever hard-blocks
server-side collection, ``parse_import`` is the manual-capture fallback.
"""
from __future__ import annotations

import asyncio
import calendar
import datetime as dt
import html
import json
import logging
import re
from urllib.parse import urljoin

from app.adapters.base import (
    AdapterError,
    AuthResult,
    BaseAdapter,
    NormalizedItem,
    NormalizedReceipt,
)
from app.core.http import AsyncClient, RequestException
from app.core.time import store_local_to_utc

logger = logging.getLogger(__name__)

BASE_URL = "https://brain.com.ua"
DASHBOARD_URL = f"{BASE_URL}/cabinet/dashboard/"
LOGIN_PAGE_URL = f"{BASE_URL}/login/"
LOGIN_URL = f"{BASE_URL}/api/v1/login"

_CSRF_RE = re.compile(r'<meta\s+name="csrftoken"\s+content="([^"]+)"', re.I)

WEB_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8",
    "Referer": f"{BASE_URL}/",
}
XHR_HEADERS = {"X-Requested-With": "XMLHttpRequest", "Referer": f"{BASE_URL}/"}

# Statuses that mean "not a completed purchase" — kept in the result (so a row
# saved earlier can be removed) but never inserted or reported. See base.py.
_CANCELLED_MARKERS = (
    "скасов", "отмен", "анульов", "не відбул", "не состоял",
    "повернн", "возврат", "återlämn",
)

_TAG_RE = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    return " ".join(html.unescape(_TAG_RE.sub(" ", fragment)).split())


def _money(fragment: str | None) -> float:
    if not fragment:
        return 0.0
    cleaned = re.sub(r"[^0-9,.-]", "", fragment.replace("\xa0", "").replace(" ", "")).replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    return float(match.group()) if match else 0.0


def _format_phone(value: str) -> str:
    """Brain's login form wants the bare national number, ``0XXXXXXXXX``."""
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("380") and len(digits) == 12:
        digits = "0" + digits[3:]
    elif digits.startswith("80") and len(digits) == 11:
        digits = "0" + digits[2:]
    if len(digits) != 10 or not digits.startswith("0"):
        raise AdapterError("Brain: вкажіть телефон у форматі 0XXXXXXXXX або +380XXXXXXXXX")
    return digits


def _add_months(moment: dt.datetime, months: int) -> dt.datetime:
    month_index = moment.month - 1 + months
    year = moment.year + month_index // 12
    month = month_index % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def _warranty_months(product_html: str) -> int | None:
    """Read the manufacturer warranty length off a Brain product card."""
    match = re.search(r'"Warranty"\s*:\s*"?(\d{1,3})"?', product_html)
    if match:
        return int(match.group(1))
    plain = _text(product_html)
    match = re.search(
        r"[Гг]арант(?:ія|ия)\s*:?\s*(\d{1,3})\s*(міс|мес|рок|рік|год|year|лет)",
        plain,
        re.I,
    )
    if not match:
        return None
    value = int(match.group(1))
    return value * 12 if match.group(2).lower().startswith(("рок", "рік", "год", "year", "лет")) else value


def _session_alive(page_html: str) -> bool:
    return "br-prof-order-block" in page_html or "Вийти з кабінету" in page_html or "Выйти из кабинета" in page_html


_ORDER_RE = re.compile(
    r'<div class="order-item" data-order-id="(?P<oid>\d+)">(?P<body>.*?)'
    r'(?=<div class="order-item" data-order-id="|<div class="br-prof-order-pagination|\Z)',
    re.S,
)
_ITEM_RE = re.compile(
    r'<div class="br-prof-th-item"\s+data-product-code="(?P<code>[^"]+)"'
    r'\s+data-order-line-id="(?P<line>\d+)">(?P<body>.*?)'
    r'(?=<div class="br-prof-th-item"\s+data-product-code=|<div class="br-prof-th-info"|\Z)',
    re.S,
)


def _parse_orders(page_html: str) -> list[NormalizedReceipt]:
    receipts: list[NormalizedReceipt] = []
    for order in _ORDER_RE.finditer(page_html):
        order_id = order.group("oid")
        body = order.group("body")

        number_match = re.search(r'br-prof-table-order">\s*([^<]+?)\s*<', body)
        order_number = _text(number_match.group(1)) if number_match else order_id

        date_match = re.search(r'br-prof-table-date">[^<]*?(\d{2}\.\d{2}\.\d{4})', body)
        if not date_match:
            continue
        day, month, year = map(int, date_match.group(1).split("."))
        purchased_at = store_local_to_utc(dt.datetime(year, month, day, 12))

        amount_match = re.search(r'br-prof-table-amount">\s*([\d\s .,]+)', body)
        total_amount = _money(amount_match.group(1)) if amount_match else 0.0

        status_match = re.search(
            r'br-prof-table-status-wrapper[^>]*>\s*<span[^>]*>\s*([^<]+?)\s*<', body
        )
        status = _text(status_match.group(1)) if status_match else ""
        excluded = any(marker in status.lower() for marker in _CANCELLED_MARKERS)

        payment_match = re.search(
            r'Спос[іб][бо]?\s*оплат[иы]:.*?col-content">\s*<div>\s*([^<]+?)\s*<', body, re.S
        )
        payment_type = _text(payment_match.group(1)) if payment_match else None

        address_match = re.search(
            r'[АА]дрес[а]?\s*доставки:.*?col-content">(.*?)</div>\s*</div>', body, re.S
        )
        shop_address = _text(address_match.group(1))[:250] if address_match else None

        items: list[NormalizedItem] = []
        for item in _ITEM_RE.finditer(body):
            item_body = item.group("body")
            name_match = re.search(
                r'class="link-hov-text"[^>]*href="([^"]+)"[^>]*>\s*([^<]+?)\s*<', item_body
            )
            product_url = urljoin(BASE_URL, html.unescape(name_match.group(1))) if name_match else None
            name = _text(name_match.group(2)) if name_match else "Товар Brain"

            price_match = re.search(r'br-prof-th-price">\s*([\d\s .,]+)', item_body)
            qty_match = re.search(r'br-prof-th-quantity">\s*([\d.,]+)', item_body)
            unit_price = _money(price_match.group(1)) if price_match else 0.0
            quantity = _money(qty_match.group(1)) or 1.0

            image_match = re.search(r'br-prof-th-img.*?data-observe-src="([^"]+)"', item_body, re.S)
            image_url = html.unescape(image_match.group(1)) if image_match else None

            items.append(NormalizedItem(
                name=name,
                quantity=quantity,
                unit="шт",
                unit_price=unit_price,
                total_price=round(unit_price * quantity, 2),
                barcode=item.group("code"),
                product_url=product_url,
                product_image_url=image_url,
                product_image_source_url=image_url,
                has_warranty=True if "warranty-wrapper" in item_body else None,
            ))

        receipts.append(NormalizedReceipt(
            external_id=order_id,
            purchased_at=purchased_at,
            total_amount=total_amount or sum(i.total_price for i in items),
            shop_name="Brain",
            shop_address=shop_address,
            payment_type=payment_type,
            channel="online",
            items=items,
            receipt_url=f"{BASE_URL}/api/v1/myorder/invoice/{order_id}/",
            excluded=excluded,
            exclusion_reason=f"Статус замовлення: {status}" if excluded else None,
            raw={"order_number": order_number, "status": status},
        ))
    return receipts


class BrainAdapter(BaseAdapter):
    store_code = "brain"

    async def login_with_password(self, phone_number: str, password: str) -> AuthResult:
        phone = _format_phone(phone_number)
        try:
            async with AsyncClient(timeout=25, headers=WEB_HEADERS) as client:
                login_page = await client.warmup(LOGIN_PAGE_URL)
                csrf_match = _CSRF_RE.search(login_page.text)
                if not csrf_match:
                    raise AdapterError("Brain: не вдалося відкрити форму входу")
                response = await client.post(
                    LOGIN_URL,
                    data={"phone_number": phone, "password": password},
                    headers={**XHR_HEADERS, "X-CSRF-Token": csrf_match.group(1)},
                )
                if response.status_code >= 400:
                    raise AdapterError(f"Brain: сервер відхилив вхід ({response.status_code})")
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                if not payload.get("Status") and not payload.get("RedirectTo"):
                    message = payload.get("Error") or payload.get("message") or "невірний телефон або пароль"
                    raise AdapterError(f"Brain: {_text(str(message))[:200]}")

                dashboard = await client.get(DASHBOARD_URL)
                if not _session_alive(dashboard.text):
                    raise AdapterError("Brain: не вдалося увійти — перевірте телефон і пароль")
                session = json.dumps(client.cookies, ensure_ascii=False, separators=(",", ":"))
        except RequestException as exc:
            raise AdapterError("Brain: не вдалося з'єднатися із сайтом") from exc
        return AuthResult(access_token=session, extra={"session_kind": "cookies"})

    async def fetch_receipts(
        self, access_token: str, extra: dict, since: dt.datetime | None = None
    ) -> list[NormalizedReceipt]:
        cookies = _cookies_from_token(access_token)
        try:
            async with AsyncClient(timeout=30, cookies=cookies, headers=WEB_HEADERS) as client:
                page = await client.get(DASHBOARD_URL)
                if page.status_code >= 400 or not _session_alive(page.text):
                    raise AdapterError("Brain: сесія завершилася, увійдіть повторно")
                receipts = _parse_orders(page.text)

                since_utc = since.astimezone(dt.timezone.utc) if since and since.tzinfo else since
                selected = [
                    receipt for receipt in receipts
                    if receipt.excluded
                    or since_utc is None
                    or receipt.purchased_at.astimezone(dt.timezone.utc) >= since_utc
                ]
                await self._enrich_warranties(client, [r for r in selected if not r.excluded])
        except RequestException as exc:
            raise AdapterError("Brain: не вдалося завантажити замовлення") from exc
        return selected

    async def _enrich_warranties(self, client: AsyncClient, receipts: list[NormalizedReceipt]) -> None:
        checked_at = dt.datetime.now(dt.timezone.utc)
        semaphore = asyncio.Semaphore(5)
        cache: dict[str, int | None] = {}

        async def enrich(receipt: NormalizedReceipt, item: NormalizedItem) -> None:
            if not item.product_url:
                return
            async with semaphore:
                try:
                    if item.product_url not in cache:
                        page = await client.get(item.product_url)
                        cache[item.product_url] = (
                            _warranty_months(page.text) if page.status_code == 200 else None
                        )
                    months = cache[item.product_url]
                except RequestException:
                    logger.warning("Brain: не вдалося перевірити гарантію для %s", item.name, exc_info=True)
                    return
            item.warranty_checked_at = checked_at
            if months:
                item.has_warranty = True
                item.warranty_text = f"Гарантія: {months} міс."
                item.warranty_expires_at = _add_months(receipt.purchased_at, months)
            elif item.has_warranty is None:
                item.has_warranty = False

        try:
            await asyncio.gather(*(
                enrich(receipt, item) for receipt in receipts for item in receipt.items
            ))
        except RequestException:
            logger.warning("Brain: каталог гарантій тимчасово недоступний", exc_info=True)

        for receipt in receipts:
            warranty_items = [item for item in receipt.items if item.has_warranty]
            receipt.has_warranty = bool(warranty_items) or None
            expiries = [item.warranty_expires_at for item in warranty_items if item.warranty_expires_at]
            receipt.warranty_expires_at = max(expiries) if expiries else None

    async def get_warranty_document(self, access_token: str | None, extra: dict, external_id: str) -> bytes:
        if not access_token:
            raise AdapterError("Brain: немає сесії — увійдіть повторно")
        cookies = _cookies_from_token(access_token)
        async with AsyncClient(timeout=25, cookies=cookies, headers=WEB_HEADERS) as client:
            page = await client.get(DASHBOARD_URL)
            if page.status_code >= 400 or not _session_alive(page.text):
                raise AdapterError("Brain: сесія завершилася, увійдіть повторно")
            order = next(
                (m for m in _ORDER_RE.finditer(page.text) if m.group("oid") == str(external_id)),
                None,
            )
            if order is None:
                raise AdapterError("Brain: замовлення не знайдено")
            items = list(_ITEM_RE.finditer(order.group("body")))
            product_code = next(
                (i.group("code") for i in items if "warranty-wrapper" in i.group("body")),
                items[0].group("code") if items else None,
            )
            if not product_code:
                raise AdapterError("Brain: гарантійний талон для покупки не знайдено")
            response = await client.get(
                f"{BASE_URL}/api/v1/myorder/warranty/{external_id}/{product_code}/",
                headers=XHR_HEADERS,
            )
        if response.status_code >= 400 or not response.content:
            raise AdapterError("Brain: гарантійний талон недоступний")
        return response.content

    def parse_import(self, payload: dict) -> list[NormalizedReceipt]:
        """Parse a saved copy of the ``/cabinet/dashboard/`` HTML (``{"html": ...}``)
        — the manual fallback if server-side collection is ever blocked."""
        page_html = payload.get("html") if isinstance(payload, dict) else None
        if not isinstance(page_html, str) or "order-item" not in page_html:
            raise ValueError("Brain: import expects {\"html\": \"<dashboard page source>\"}")
        return _parse_orders(page_html)


def _cookies_from_token(access_token: str) -> dict[str, str]:
    try:
        cookies = json.loads(access_token)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AdapterError("Brain: пошкоджена сесія, увійдіть повторно") from exc
    if not isinstance(cookies, dict) or not cookies:
        raise AdapterError("Brain: порожня сесія, увійдіть повторно")
    return {str(key): str(value) for key, value in cookies.items()}
