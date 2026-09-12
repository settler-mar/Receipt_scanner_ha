"""Prom.ua buyer order history and public catalog search.

Prom's documented public API belongs to sellers and returns orders received by
their company, not purchases made by a buyer. For a private buyer, there is no
documented OAuth/API access to the order history, so orders are imported through
the buyer cabinet in a real Chromium context.  Like Dnipro-M, the encrypted
"access token" is a Patchright storage-state JSON document.  Receipt collection
replays that state and calls the cabinet's own ``OrderListQuery`` GraphQL query.

The parser accepts both a small stable export shape and the common field names
used by Prom order payloads. Keeping that tolerance here is useful because API
responses and manual exports often carry slightly different names.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import logging
import re
import urllib.parse
from typing import Any

from app.adapters.base import (
    AdapterError, BaseAdapter, CatalogProduct, NormalizedItem, NormalizedReceipt,
)
import httpx
from patchright.async_api import Browser, Playwright, async_playwright


_CANCELLED = {"cancelled", "canceled", "deleted", "declined", "скасовано", "отменен", "отменено"}
_PRODUCT_LINK_RE = re.compile(
    r'<a\b[^>]*href=["\'](?P<href>/(?:ua/)?(?:p|m)[^"\']+)["\'][^>]*>(?P<body>.*?)</a>',
    re.I | re.S,
)
_TAG_RE = re.compile(r"<[^>]+>")
_PRICE_RE = re.compile(r"(?<![\d])(?:від\s+)?(?P<price>\d[\d\s\u00a0]*(?:[,.]\d{1,2})?)\s*₴", re.I)
_IMAGE_RE = re.compile(
    r'<img\b[^>]*(?:src|data-src)=["\'](?P<src>https?://[^"\']+)["\']', re.I,
)
BASE_URL = "https://my.prom.ua"
ORDERS_URL = f"{BASE_URL}/ua/cabinet/user/orders"
logger = logging.getLogger(__name__)

ORDER_LIST_QUERY = """query OrderListQuery($perPage: Int!, $page: Int!, $searchTerm: String!, $tab: String, $sort: String!, $userId: Int) {
  orderList(perPage: $perPage, page: $page, search: $searchTerm, tab: $tab, sort: $sort, userId: $userId) {
    pagesCount
    ordersCount
    orders {
      id
      dateCreated
      type
      status
      isArchived
      source
      statusChain { title key isExist }
      orderPaymentOption { id type }
      company { id name logo siteUrl catalogUrl email }
      cart {
        id
        totals { amount currency currencyText }
        cartItems {
          id
          productId
          productPrice
          productBasePrice
          productName
          presence
          quantity
          isGift
          measureUnit { value }
          measureUnitByQuantity
          maybeProduct {
            product {
              id
              name
              imageUrl(width: 1000, height: 1000)
              measureUnit { value }
            }
          }
        }
      }
      total(source: "orderList") { type title subtitle value deliveryType discountType order }
    }
  }
}"""


def _first(data: dict, *names: str, default: Any = None) -> Any:
    for name in names:
        value = data.get(name)
        if value is not None and value != "":
            return value
    return default


def _number(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^0-9,.-]", "", str(value)).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return default


def _date(value: Any) -> dt.datetime:
    if isinstance(value, (int, float)):
        # Be liberal with browser payloads: JavaScript timestamps are ms.
        timestamp = float(value) / 1000 if value > 10_000_000_000 else float(value)
        return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc)
    text = str(value or "").strip()
    if not text:
        raise ValueError("Prom.ua: у замовленні відсутня дата")
    text = text.replace("Z", "+00:00")
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
            raise ValueError(f"Prom.ua: невідомий формат дати: {value}") from None
    return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed


def _label(value: Any) -> str | None:
    if isinstance(value, dict):
        value = _first(value, "name", "title", "label")
    return str(value).strip() if value not in (None, "") else None


def _item(data: dict) -> NormalizedItem:
    quantity = _number(_first(data, "quantity", "count", "amount"), 1.0) or 1.0
    unit_price = _number(_first(data, "unit_price", "price", "price_with_discount"))
    total = _number(_first(data, "total_price", "total", "sum"), unit_price * quantity)
    discount = _number(_first(data, "discount", "discount_amount"))
    return NormalizedItem(
        name=str(_first(data, "name", "title", "product_name", default="Товар Prom.ua")).strip(),
        quantity=quantity,
        unit=_first(data, "unit", "measure_unit", default="шт"),
        unit_price=unit_price,
        total_price=total,
        discount=discount,
        barcode=_first(data, "barcode", "sku", "external_id"),
        product_url=_first(data, "product_url", "url"),
        product_image_url=_first(data, "product_image_url", "image_url", "image"),
        has_warranty=data.get("has_warranty"),
        warranty_text=data.get("warranty_text"),
    )


async def _launch_browser(*, headless: bool = True) -> tuple[Playwright, Browser]:
    playwright = await async_playwright().start()
    try:
        return playwright, await playwright.chromium.launch(headless=headless, channel="chrome")
    except Exception as exc:
        await playwright.stop()
        raise AdapterError("Prom.ua: не вдалося запустити Chromium") from exc


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _graphql_item(data: dict) -> NormalizedItem:
    data = _as_dict(data)
    product = _as_dict(_as_dict(data.get("maybeProduct")).get("product"))
    quantity = _number(data.get("quantity"), 1.0) or 1.0
    unit_price = _number(data.get("productPrice"))
    base_price = _number(data.get("productBasePrice"), unit_price)
    unit = ((product.get("measureUnit") or {}).get("value")
            or (data.get("measureUnit") or {}).get("value")
            or data.get("measureUnitByQuantity") or "шт")
    return NormalizedItem(
        name=str(product.get("name") or data.get("productName") or "Товар Prom.ua").strip(),
        quantity=quantity,
        unit=unit,
        unit_price=unit_price,
        total_price=unit_price * quantity,
        discount=max(0.0, (base_price - unit_price) * quantity),
        barcode=str(data.get("productId")) if data.get("productId") is not None else None,
        product_image_url=product.get("imageUrl"),
    )


def _graphql_receipt(order: dict) -> NormalizedReceipt:
    order = _as_dict(order)
    cart = _as_dict(order.get("cart"))
    items = [_graphql_item(item) for item in (cart.get("cartItems") or [])]
    # `cart.totals` is an array of Price rows (amount/currency/currencyText).
    totals_raw = cart.get("totals")
    totals = next((_as_dict(t) for t in totals_raw if isinstance(t, dict)), {}) if isinstance(totals_raw, list) else _as_dict(totals_raw)
    status_chain = order.get("statusChain") or []
    current_status = next(
        (entry for entry in reversed(status_chain) if isinstance(entry, dict) and entry.get("isExist")),
        {},
    )
    status_key = str(current_status.get("key") or order.get("status") or "").lower()
    company = _as_dict(order.get("company"))
    total_amount = _number(totals.get("amount"), sum(item.total_price for item in items))
    return NormalizedReceipt(
        external_id=str(order["id"]),
        purchased_at=_date(order.get("dateCreated")),
        total_amount=total_amount,
        currency=str(totals.get("currency") or "UAH"),
        shop_name=company.get("name") or "Prom.ua",
        payment_type=_as_dict(order.get("orderPaymentOption")).get("type"),
        channel="online",
        items=items,
        total_discount=sum(item.discount for item in items),
        receipt_url=f"{ORDERS_URL}/{order['id']}",
        excluded=status_key in _CANCELLED or "cancel" in status_key,
        exclusion_reason=f"Prom.ua status: {status_key}" if status_key else None,
        raw=order,
    )


class PromAdapter(BaseAdapter):
    store_code = "prom"

    # Interactive login (phone + SMS one-time code, unified Rozetka/Clerk
    # account) lives in ``app/services/prom_login.py`` and is driven through the
    # dedicated ``/api/accounts/{id}/prom/login/*`` endpoints — modelled on the
    # AliExpress browser-login flow (step progress, manual takeover, screenshot
    # streaming). The credential it stores is a Patchright ``storage_state`` JSON
    # blob, replayed by ``fetch_receipts`` below.

    async def fetch_receipts(
        self, access_token: str, extra: dict, since: dt.datetime | None = None
    ) -> list[NormalizedReceipt]:
        try:
            state = json.loads(access_token)
            if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
                raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AdapterError("Prom.ua: збережена сесія пошкоджена — увійдіть повторно") from exc

        playwright, browser = await _launch_browser(headless=True)
        context = await browser.new_context(storage_state=state, locale="uk-UA")
        page = await context.new_page()
        receipts: list[NormalizedReceipt] = []
        try:
            await page.goto(ORDERS_URL, wait_until="domcontentloaded", timeout=60_000)
            if "/cabinet/user/orders" not in page.url:
                raise AdapterError("Prom.ua: браузерна сесія завершилась — потрібна повторна авторизація")
            page_number = 1
            while True:
                result = await page.evaluate("""async ({query, variables}) => {
                    const infoResponse = await fetch('/remote/auth/info', {credentials: 'include'});
                    const info = await infoResponse.json();
                    const cookie = document.cookie.split('; ').find(v => v.startsWith('csrf_token='));
                    const csrf = infoResponse.headers.get('x-csrftoken') || (cookie ? decodeURIComponent(cookie.split('=')[1]) : '');
                    const response = await fetch('/cabinet/user/graphql', {
                        method: 'POST', credentials: 'include',
                        headers: {
                            'Accept': 'application/json', 'Content-Type': 'application/json',
                            'X-Requested-With': 'XMLHttpRequest', 'X-CSRFToken': csrf,
                            'X-PromUserID': String(info.id || ''),
                            'x-client-name': 'cms-ui-web', 'x-language': 'uk',
                            'x-apollo-operation-name': 'OrderListQuery'
                        },
                        body: JSON.stringify({operationName: 'OrderListQuery', query, variables})
                    });
                    return {status: response.status, url: response.url, body: await response.json()};
                }""", {
                    "query": ORDER_LIST_QUERY,
                    "variables": {"perPage": 20, "page": page_number, "searchTerm": "", "tab": "all", "sort": "dateCreated.desc"},
                })
                if not isinstance(result, dict):
                    raise AdapterError(f"Prom.ua: несподівана відповідь GraphQL ({type(result).__name__})")
                body = result.get("body")
                if isinstance(body, list):
                    body = body[0] if body else {}
                if not isinstance(body, dict):
                    raise AdapterError(
                        f"Prom.ua: GraphQL повернув не-JSON (HTTP {result.get('status')}) — "
                        "імовірно сесія завершилась, увійдіть повторно"
                    )
                if result.get("status") in {401, 403} or body.get("errors"):
                    errors = body.get("errors") or []
                    if any("AUTH_REQUIRED" in str(error) for error in errors):
                        raise AdapterError("Prom.ua: браузерна сесія завершилась — потрібна повторна авторизація")
                    raise AdapterError(f"Prom.ua: GraphQL повернув помилку: {errors or result.get('status')}")
                order_list = (body.get("data") or {}).get("orderList") or {}
                orders = order_list.get("orders") or []
                if not orders:
                    break
                stop = False
                for order in orders:
                    receipt = _graphql_receipt(order)
                    if since and receipt.purchased_at < since:
                        stop = True
                        break
                    receipts.append(receipt)
                if stop or page_number >= int(order_list.get("pagesCount") or page_number):
                    break
                page_number += 1
            return receipts
        except AdapterError:
            raise
        except Exception as exc:
            logger.exception("Prom.ua GraphQL receipt collection failed")
            raise AdapterError(
                f"Prom.ua: помилка збору замовлень: {type(exc).__name__}: {exc}"
            ) from exc
        finally:
            await context.close()
            await browser.close()
            await playwright.stop()

    def parse_import(self, payload: dict) -> list[NormalizedReceipt]:
        orders = _first(payload, "orders", "purchases", "data")
        if isinstance(orders, dict):
            orders = _first(orders, "orders", "purchases", "items", "results")
        if not isinstance(orders, list):
            raise ValueError(
                "Prom.ua: очікується JSON з масивом orders (номер/id, дата/date_created, "
                "сума/price та масив products/items)"
            )

        receipts: list[NormalizedReceipt] = []
        for order in orders:
            if not isinstance(order, dict):
                continue
            external_id = _first(order, "number", "order_number", "id")
            purchased_at = _first(order, "purchased_at", "date_created", "created_at", "date")
            if external_id is None:
                raise ValueError("Prom.ua: у замовленні відсутній номер/id")

            raw_items = _first(order, "items", "products", "order_items", default=[])
            if isinstance(raw_items, dict):
                raw_items = _first(raw_items, "items", "products", default=[])
            items = [_item(item) for item in raw_items if isinstance(item, dict)]
            total = _number(
                _first(order, "total_amount", "price", "total_price", "total", "amount"),
                sum(item.total_price for item in items),
            )
            status = _label(_first(order, "status", "status_name")) or ""
            seller = _first(order, "shop_name", "company_name", "seller_name", "company")
            if isinstance(seller, dict):
                seller = _first(seller, "name", "title")

            receipts.append(NormalizedReceipt(
                external_id=str(external_id),
                purchased_at=_date(purchased_at),
                total_amount=total,
                currency=str(_first(order, "currency", "currency_code", default="UAH")),
                shop_name=str(seller).strip() if seller else "Prom.ua",
                shop_address=_label(_first(order, "delivery_address", "address")),
                payment_type=_label(_first(order, "payment_type", "payment_option", "payment_method")),
                channel="online",
                items=items,
                total_discount=_number(_first(order, "total_discount", "discount")),
                receipt_url=_first(order, "receipt_url", "order_url", "url"),
                excluded=status.lower() in _CANCELLED,
                exclusion_reason=f"Prom.ua status: {status}" if status.lower() in _CANCELLED else None,
                raw=order,
            ))
        return receipts

    async def search_catalog(self, access_token: str | None, extra: dict, query: str) -> list[CatalogProduct]:
        params = {"search_term": query}
        headers = {"User-Agent": "Mozilla/5.0", "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8"}
        async with httpx.AsyncClient(timeout=20, headers=headers, follow_redirects=True) as client:
            resp = await client.get("https://prom.ua/search", params=params)
        if resp.status_code != 200:
            raise AdapterError(f"Prom.ua: пошук товарів повернув {resp.status_code}")
        return _parse_catalog_html(resp.text)


def _clean_html(text: str) -> str:
    return html.unescape(_TAG_RE.sub(" ", text)).strip()


def _parse_catalog_html(body: str) -> list[CatalogProduct]:
    products: list[CatalogProduct] = []
    seen: set[str] = set()
    for match in _PRODUCT_LINK_RE.finditer(body):
        href = html.unescape(match.group("href"))
        text = re.sub(r"\s+", " ", _clean_html(match.group("body")))
        if not text or href in seen:
            continue
        window = _clean_html(body[match.end():match.end() + 2500])
        price_match = _PRICE_RE.search(window)
        if not price_match:
            continue
        image_match = _IMAGE_RE.search(match.group("body"))
        seen.add(href)
        products.append(CatalogProduct(
            name=text,
            price=_number(price_match.group("price")),
            image_url=html.unescape(image_match.group("src")) if image_match else None,
            product_url=urllib.parse.urljoin("https://prom.ua", href),
            in_stock=("В наличии" in window or "В наявності" in window or "Готово к отправке" in window),
        ))
        if len(products) >= 30:
            break
    return products
