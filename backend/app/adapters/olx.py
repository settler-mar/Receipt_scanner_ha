"""Public OLX.ua listing search.

This adapter intentionally uses only the public search and listing pages. OLX
often hides a seller's phone behind an interaction or does not publish it at
all; in those cases ``seller_phone`` remains ``None``. We do not call private
contact endpoints or attempt to unmask a number.
"""
from __future__ import annotations

import html
import datetime as dt
import json
import os
import re
from urllib.parse import quote, urljoin

import httpx
from patchright.async_api import Browser, async_playwright

from app.adapters.base import (
    AdapterError, BaseAdapter, CatalogProduct, NormalizedItem, NormalizedReceipt, NotImplementedYet,
)

BASE_URL = "https://www.olx.ua"
DELIVERY_URL = f"{BASE_URL}/uk/delivery/"
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?380|0)\s*\(?\d{2}\)?[\s-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)")
_LINK_RE = re.compile(
    r'<a\b[^>]*href=["\'](?P<href>[^"\']+)["\'][^>]*>(?P<body>.*?)</a>',
    re.I | re.S,
)
_PRICE_RE = re.compile(r"(?P<price>\d[\d\s\u00a0]*(?:[,.]\d{1,2})?)\s*(?:грн|₴|UAH)", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", value))).strip()


def _phone(value: str) -> str | None:
    match = _PHONE_RE.search(html.unescape(value))
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(0))
    if digits.startswith("0"):
        digits = "38" + digits
    if digits.startswith("380") and len(digits) == 12:
        return "+" + digits
    return None


def _price(value: str) -> float | None:
    match = _PRICE_RE.search(_text(value))
    if not match:
        return None
    try:
        return float(match.group("price").replace(" ", "").replace("\u00a0", "").replace(",", "."))
    except ValueError:
        return None


def _amount(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return _price(str(value))


def _date(value) -> dt.datetime:
    if isinstance(value, (int, float)):
        timestamp = float(value) / 1000 if value > 10_000_000_000 else float(value)
        return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc)
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = dt.datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"OLX: невідомий формат дати: {value}") from None
    return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed


def _first(data: dict, *keys, default=None):
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return default


def _as_text(value) -> str | None:
    if isinstance(value, dict):
        value = _first(value, "name", "title", "label", "value", "text")
    return str(value).strip() if value not in (None, "") else None


def _order_item(raw: dict, seller_name: str | None = None) -> NormalizedItem:
    name = _as_text(_first(raw, "name", "title", "productName", "itemTitle", "subject")) or "Товар OLX"
    quantity = float(_first(raw, "quantity", "count", "qty", default=1) or 1)
    price = _amount(_first(raw, "price", "unitPrice", "amount", "cost", default=0)) or 0.0
    total = _amount(_first(raw, "total", "totalPrice", "totalAmount", "sum", default=price * quantity))
    total = total if total is not None else price * quantity
    url = _first(raw, "url", "productUrl", "listingUrl", "advertUrl", "href")
    image = _first(raw, "image", "imageUrl", "photo", "thumbnail")
    return NormalizedItem(
        name=name, quantity=quantity, unit="шт", unit_price=price or (total / quantity if quantity else total),
        total_price=total, product_url=url, product_image_url=image,
        product_image_source_url=image, merchant_name=seller_name,
    )


def _receipt_from_order(order: dict) -> NormalizedReceipt:
    order_id = _first(order, "order_id", "orderId", "transactionId", "transaction_id", "id", "number")
    if order_id is None:
        raise ValueError("OLX: у замовленні відсутній номер/id")
    purchased = _first(order, "purchased_at", "purchasedAt", "created_at", "createdAt", "date", "dateCreated", "orderedAt")
    if purchased is None:
        raise ValueError(f"OLX: замовлення {order_id} не містить дати")
    seller = _as_text(_first(order, "seller", "seller_name", "sellerName", "contact", "user"))
    contact = _first(order, "contact", "seller", "user", default={})
    phone = _phone(str(_first(contact, "phone", "phoneNumber", "mobile", default=""))) if isinstance(contact, dict) else _phone(str(contact))
    raw_items = _first(order, "items", "products", "goods", "orderItems", default=[])
    if isinstance(raw_items, dict):
        raw_items = [raw_items]
    items = [_order_item(item, seller) for item in raw_items if isinstance(item, dict)]
    if not items:
        items = [_order_item(order, seller)]
    total = _amount(_first(order, "total_amount", "totalAmount", "total_price", "totalPrice", "amount", "price", default=sum(i.total_price for i in items)))
    status = str(_first(order, "status", "statusName", "state", default=""))
    cancelled = any(word in status.lower() for word in ("cancel", "скас", "відхил", "отмен"))
    listing_url = _first(order, "url", "listingUrl", "advertUrl", "productUrl")
    return NormalizedReceipt(
        external_id=str(order_id), purchased_at=_date(purchased), total_amount=total or 0,
        currency=str(_first(order, "currency", "currencyCode", default="UAH")),
        shop_name=seller or "OLX", shop_address=_as_text(_first(order, "deliveryAddress", "address", "pickupPoint")),
        payment_type=_as_text(_first(order, "paymentMethod", "payment_type", "payment")),
        channel="online", items=items, receipt_url=listing_url,
        order_status=status or None, excluded=cancelled,
        exclusion_reason=f"Статус замовлення: {status}" if cancelled else None,
        raw={**order, "seller_phone": phone} if phone else order,
    )


def _walk_orders(value, out: list[dict]) -> None:
    if isinstance(value, list):
        for item in value:
            _walk_orders(item, out)
    elif isinstance(value, dict):
        has_id = any(key in value for key in ("order_id", "orderId", "transactionId", "transaction_id", "orderNumber"))
        has_date = any(key in value for key in ("created_at", "createdAt", "purchasedAt", "orderedAt", "dateCreated"))
        if has_id and has_date:
            out.append(value)
            return
        for child in value.values():
            _walk_orders(child, out)


def _parse_orders_html(body: str) -> list[NormalizedReceipt]:
    """Parse server-rendered/embedded OLX delivery data without private APIs."""
    candidates: list[dict] = []
    for match in re.finditer(r'<script[^>]*type=["\']application/(?:json|ld\+json)["\'][^>]*>(.*?)</script>', body, re.I | re.S):
        try:
            _walk_orders(json.loads(html.unescape(match.group(1))), candidates)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    for match in re.finditer(r'data-order(?:-data)?=["\'](?P<data>\{.*?\})["\']', body, re.I | re.S):
        try:
            _walk_orders(json.loads(html.unescape(match.group("data"))), candidates)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    unique: dict[str, NormalizedReceipt] = {}
    for order in candidates:
        try:
            receipt = _receipt_from_order(order)
        except ValueError:
            continue
        unique[receipt.external_id] = receipt
    return list(unique.values())
    try:
        return float(match.group("price").replace(" ", "").replace("\u00a0", "").replace(",", "."))
    except ValueError:
        return None


def _image(card: str) -> str | None:
    match = re.search(r'<img\b[^>]*(?:src|data-src)=["\'](https?://[^"\']+)', card, re.I)
    return html.unescape(match.group(1)) if match else None


def _parse_search_html(body: str) -> list[CatalogProduct]:
    products: list[CatalogProduct] = []
    seen: set[str] = set()
    # Search cards have changed class names several times; the stable part is
    # the /d/ listing URL and a nearby price/title block.
    for match in _LINK_RE.finditer(body):
        href = html.unescape(match.group("href"))
        if "/d/" not in href or href in seen:
            continue
        card = body[max(0, match.start() - 1800): min(len(body), match.end() + 1800)]
        title = _text(match.group("body"))
        title = _PRICE_RE.sub("", title).strip()
        price = _price(card)
        if not title or price is None or len(title) < 2:
            continue
        seen.add(href)
        products.append(CatalogProduct(
            name=title[:300], price=price, currency="UAH",
            product_url=urljoin(BASE_URL, href), image_url=_image(card),
        ))
        if len(products) >= 30:
            break
    return products


def _enrich_listing(product: CatalogProduct, body: str) -> CatalogProduct:
    phone = _phone(body)
    if phone:
        product.seller_phone = phone
    seller = re.search(r'(?i)(?:seller|user|advertiser|продавець)[^<]{0,80}[>:]\s*([^<]{2,80})', body)
    if seller:
        product.seller_name = _text(seller.group(1))[:120]
    return product


class OlxAdapter(BaseAdapter):
    store_code = "olx"
    catalog_search_requires_auth = False
    receipts_supported = True

    async def fetch_receipts(self, access_token: str, extra: dict, since=None):
        try:
            state = json.loads(access_token)
            if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
                raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AdapterError("OLX: збережена браузерна сесія пошкоджена — увійдіть повторно") from exc
        playwright = await async_playwright().start()
        browser: Browser | None = None
        context = None
        try:
            browser = await playwright.chromium.launch(headless=not bool(os.environ.get("DISPLAY")), channel="chrome")
            context = await browser.new_context(storage_state=state, locale="uk-UA")
            page = await context.new_page()
            await page.goto(DELIVERY_URL, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(2_000)
            if "login" in page.url.lower() or "sign-in" in page.url.lower():
                raise AdapterError("OLX: браузерна сесія завершилась — потрібна повторна авторизація")
            receipts = _parse_orders_html(await page.content())
            if not receipts:
                raise AdapterError("OLX: у розділі «Мої покупки» не знайдено замовлень або формат сторінки змінився")
            return [r for r in receipts if not since or r.purchased_at >= since]
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"OLX: не вдалося завантажити замовлення: {type(exc).__name__}") from exc
        finally:
            if context:
                await context.close()
            if browser:
                await browser.close()
            await playwright.stop()

    def parse_import(self, payload: dict) -> list[NormalizedReceipt]:
        orders = _first(payload, "orders", "purchases", "transactions", "data", "items")
        if isinstance(orders, dict):
            nested: list[dict] = []
            _walk_orders(orders, nested)
            orders = nested
        if isinstance(payload.get("html"), str):
            return _parse_orders_html(payload["html"])
        if not isinstance(orders, list):
            raise ValueError("OLX: очікується JSON з масивом orders/purchases або полем html")
        return [_receipt_from_order(order) for order in orders if isinstance(order, dict)]

    async def search_catalog(self, access_token: str | None, extra: dict, query: str) -> list[CatalogProduct]:
        url = f"{BASE_URL}/d/uk/list/q-{quote(query.strip())}/"
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
            "Accept-Language": "uk-UA,uk;q=0.9",
        }
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=headers) as client:
                response = await client.get(url)
                if response.status_code != 200:
                    raise AdapterError(f"OLX: пошук оголошень повернув {response.status_code}")
                products = _parse_search_html(response.text)
                # A phone is optional metadata, so a blocked/changed detail
                # page must not make the whole product search fail.
                for product in products[:10]:
                    try:
                        detail = await client.get(product.product_url or "")
                        if detail.status_code == 200:
                            _enrich_listing(product, detail.text)
                    except httpx.HTTPError:
                        continue
                return products
        except AdapterError:
            raise
        except httpx.HTTPError as exc:
            raise AdapterError("OLX: не вдалося отримати публічні оголошення") from exc


__all__ = ["OlxAdapter", "_parse_search_html", "_parse_orders_html", "_phone"]
