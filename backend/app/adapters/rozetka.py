"""Rozetka.ua buyer order history.

Rozetka and Prom.ua use the same RID/Clerk buyer account.  The credential is
therefore the encrypted Patchright storage state captured by the shared
browser login flow; this adapter only changes the cabinet URL and the order
normalisation rules.
"""
from __future__ import annotations

import datetime as dt
import base64
import json
import os
import re
from typing import Any

from patchright.async_api import Browser, Playwright, async_playwright

from app.adapters.base import AdapterError, BaseAdapter, NormalizedItem, NormalizedReceipt, NormalizedReceiptComponent

ORDERS_URL = "https://rozetka.com.ua/ua/cabinet/orders/"
ORDERS_API_URL = "https://cabinet-api.rozetka.com.ua/v1/orders/get"
WARRANTY_API_URL = "https://cabinet-api.rozetka.com.ua/v1/orders/get-warranty-ticket"
_CANCELLED = {"cancelled", "canceled", "скасовано", "отменен", "отменено", "deleted"}


def _first(data: dict, *names: str, default: Any = None) -> Any:
    for name in names:
        value = data.get(name)
        if value is not None and value != "":
            return value
    return default


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[^0-9,.-]", "", str(value or "")).replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return default


def _warranty_present(value: Any) -> bool:
    if isinstance(value, dict):
        value = value.get("name") or value.get("title") or value.get("text") or value.get("period")
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return bool(text) and not any(marker in text for marker in ("відсут", "отсутств", "немає", "нет гарант"))


def _date(value: Any) -> dt.datetime:
    if isinstance(value, (int, float)):
        value = float(value) / 1000 if value > 10_000_000_000 else float(value)
        return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc)
    text = str(value or "").strip().replace("Z", "+00:00")
    for parser in (dt.datetime.fromisoformat,):
        try:
            parsed = parser(text)
            return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed
        except ValueError:
            pass
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(text, fmt).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            pass
    raise ValueError(f"Rozetka.ua: невідомий формат дати: {value}")


def _item(raw: dict, purchased_at: Any = None) -> NormalizedItem:
    quantity = _number(_first(raw, "quantity", "count", "amount"), 1) or 1
    unit_price = _number(_first(raw, "unit_price", "price", "price_with_discount"))
    total = _number(_first(raw, "total_price", "total", "sum"), unit_price * quantity)
    warranty = _first(raw, "warranty", "guarantee", "warranty_period", "guarantee_period")
    has_warranty = raw.get("has_warranty")
    if has_warranty is None:
        has_warranty = _warranty_present(warranty)
    checked_at = raw.get("warranty_checked_at")
    if isinstance(checked_at, str):
        try:
            checked_at = _date(checked_at)
        except ValueError:
            checked_at = None
    return NormalizedItem(
        name=str(_first(raw, "name", "title", "product_name", "subject", default="Товар Rozetka.ua")),
        quantity=quantity, unit=_first(raw, "unit", "measure_unit", default="шт"),
        unit_price=unit_price, total_price=total,
        discount=_number(_first(raw, "discount", "discount_amount")),
        barcode=_first(raw, "barcode", "sku", "article", "product_id"),
        product_url=_first(raw, "product_url", "url"),
        product_image_url=_first(raw, "product_image_url", "image_url", "image"),
        has_warranty=has_warranty,
        warranty_text=raw.get("warranty_text") or (str(warranty) if warranty else None),
        warranty_expires_at=_warranty_date(raw, purchased_at),
        warranty_checked_at=checked_at,
    )


def _warranty_date(raw: dict, fallback_purchased_at: Any = None) -> dt.datetime | None:
    value = _first(raw, "warranty_expires_at", "guarantee_expires_at")
    if value:
        try:
            return _date(value)
        except ValueError:
            pass
    months = _number(_first(raw, "warranty_months", "guarantee_months"))
    purchased = _first(raw, "purchased_at", "date_created", "created_at", "date", default=fallback_purchased_at)
    if months and purchased:
        try:
            start = _date(purchased)
            return start + dt.timedelta(days=round(months * 30.4375))
        except ValueError:
            pass
    return None


def _receipt(order: dict) -> NormalizedReceipt:
    # Native buyer-cabinet response (see ORDERS_API_URL). Convert it to the
    # adapter's small neutral shape before normalising common/manual imports.
    detail = order.get("order_detailed") if isinstance(order.get("order_detailed"), dict) else {}
    if detail:
        seller = detail.get("goods_seller") or {}
        warranty = detail.get("warranty") or {}
        delivery = detail.get("order_delivery_info") or {}
        info = detail.get("order_info") or {}
        native_items = []
        for product in detail.get("order_products") or []:
            if not isinstance(product, dict):
                continue
            images = product.get("images") or {}
            product_warranty = product.get("_warranty_available")
            native_items.append({
                "id": product.get("id"),
                "title": product.get("title"),
                "quantity": product.get("quantity", 1),
                "price": product.get("price", product.get("cost")),
                "total_price": product.get("cost_with_discount", product.get("cost")),
                "discount": max(0.0, _number(product.get("cost")) - _number(product.get("cost_with_discount"))),
                "product_id": product.get("id"),
                "url": product.get("href"),
                "image": images.get("main") or images.get("original"),
                "has_warranty": product_warranty if product_warranty is not None else bool(warranty.get("guarantee")),
                "warranty_text": product.get("_warranty_text"),
                "warranty_checked_at": (
                    dt.datetime.now(dt.timezone.utc).isoformat() if product_warranty is not None else None
                ),
            })
        order_warranty = order.get("_warranty_available")
        order = {
            **order,
            "created_at": detail.get("created_raw") or order.get("created"),
            "total_amount": detail.get("cost_with_discount", order.get("discount_cost", order.get("cost"))),
            "shop_name": seller.get("title") or "Rozetka.ua",
            "delivery_address": delivery.get("delivery_full_address"),
            "payment_type": info.get("payment_title") or info.get("payment_type"),
            "items": native_items,
            "has_warranty": order_warranty if order_warranty is not None else (
                order.get("order_has_warranty_ticket") or warranty.get("guarantee")
            ),
        }
    purchased_value = _first(order, "purchased_at", "date_created", "created_date", "dateCreated", "created_at", "createdAt", "created", "orderDate", "date")
    purchased_at = _date(purchased_value)
    raw_items = _first(order, "items", "products", "order_items", "goods", default=[])
    if isinstance(raw_items, dict):
        raw_items = _first(raw_items, "items", "products", "goods", default=[])
    items = [_item(item, purchased_at) for item in raw_items if isinstance(item, dict)]
    status = str(_first(order, "status", "status_name", "state", default="")).lower()
    seller = _first(order, "shop_name", "seller_name", "company_name", "seller", default="Rozetka.ua")
    if isinstance(seller, dict):
        seller = _first(seller, "name", "title", default="Rozetka.ua")
    total = _number(_first(order, "total_amount", "total_price", "price", "total", "amount"),
                    sum(item.total_price for item in items))
    external_id = _first(order, "number", "order_number", "orderNumber", "id", "order_id", "orderId")
    if external_id is None:
        raise ValueError("Rozetka.ua: у замовленні відсутній номер/id")
    excluded = status in _CANCELLED or "cancel" in status or "скас" in status
    components: list[NormalizedReceiptComponent] = []
    delivery = detail.get("order_delivery_info") if isinstance(detail, dict) else {}
    delivery_amount = _number(_first(delivery, "cost_with_discount", "cost")) if isinstance(delivery, dict) else 0
    if abs(delivery_amount) >= 0.005:
        components.append(NormalizedReceiptComponent(
            kind="delivery", code="delivery", label="Доставка", amount=round(delivery_amount, 2),
            currency=str(_first(order, "currency", "currency_code", default="UAH")),
            source="rozetka.order_delivery_info",
            raw={"delivery": delivery},
        ))
    return NormalizedReceipt(
        external_id=str(external_id),
        purchased_at=purchased_at,
        total_amount=total, currency=str(_first(order, "currency", "currency_code", default="UAH")),
        shop_name=str(seller), shop_address=_first(order, "delivery_address", "address"),
        payment_type=_first(order, "payment_type", "payment_method", "payment_option"),
        channel="online", items=items,
        components=components,
        total_discount=_number(_first(order, "total_discount", "discount")),
        receipt_url=_first(order, "receipt_url", "order_url", "url")
        or f"https://rozetka.com.ua/ua/cabinet/orders/{external_id}/",
        excluded=excluded, exclusion_reason=f"Rozetka.ua status: {status}" if excluded else None,
        has_warranty=_warranty_present(_first(order, "has_warranty", "warranty", "guarantee")) or any(i.has_warranty for i in items),
        warranty_expires_at=max((i.warranty_expires_at for i in items if i.warranty_expires_at), default=None),
        raw=order,
    )


def _order_candidates(value: Any) -> list[dict]:
    """Find order lists in the slightly varying cabinet API envelopes."""
    found: list[dict] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and any(key in item for key in ("id", "order_id", "order_number", "orderNumber", "orderId")) and any(
                key in item for key in ("date", "created", "createdAt", "created_date", "date_created", "dateCreated", "orderDate", "purchased_at")
            ):
                found.append(item)
            else:
                found.extend(_order_candidates(item))
    elif isinstance(value, dict):
        if any(key in value for key in ("id", "order_id", "order_number", "orderNumber", "orderId")) and any(
            key in value for key in ("date", "created", "createdAt", "created_date", "date_created", "dateCreated", "orderDate", "purchased_at")
        ):
            found.append(value)
        for key, child in value.items():
            found.extend(_order_candidates(child))
    return found


async def _warranty_status(context, headers: dict[str, str], order: dict) -> dict[str, dict]:
    """Read per-product warranty status from Rozetka's cabinet endpoint."""
    detail = order.get("order_detailed") if isinstance(order.get("order_detailed"), dict) else {}
    products = detail.get("order_products") or []
    order_id = order.get("id")
    goods_ids = [str(product.get("id")) for product in products if isinstance(product, dict) and product.get("id")]
    if not order_id or not goods_ids:
        return {}
    try:
        response = await context.request.get(
            WARRANTY_API_URL,
            params={
                "front-type": "xl", "country": "UA", "lang": "ua",
                "goods_ids": ",".join(goods_ids), "order_id": str(order_id),
            },
            headers=headers,
        )
        if response.status != 200:
            return {}
        body = json.loads((await response.text()).lstrip("\ufeff"))
        result = ((body.get("data") or {}).get("result") or {}) if isinstance(body, dict) else {}
        return {
            str(goods_id): entries[0]
            for goods_id, entries in result.items()
            if isinstance(entries, list) and entries and isinstance(entries[0], dict)
        }
    except Exception:
        # Warranty metadata must not make an otherwise valid order import fail.
        return {}


class RozetkaAdapter(BaseAdapter):
    store_code = "rozetka"

    async def fetch_receipts(self, access_token: str, extra: dict, since: dt.datetime | None = None) -> list[NormalizedReceipt]:
        try:
            state = json.loads(access_token)
            if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
                raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AdapterError("Rozetka.ua: збережена сесія пошкоджена — увійдіть повторно") from exc
        playwright = await async_playwright().start()
        try:
            # Rozetka's Cloudflare clearance is tied to the browser fingerprint.
            # Login runs headed in the container's Xvfb display; using headless
            # Chromium here makes the same valid cookies receive a 403 HTML
            # challenge instead of the JSON orders response.
            browser: Browser = await playwright.chromium.launch(
                headless=not bool(os.environ.get("DISPLAY")), channel="chrome",
            )
            context = await browser.new_context(storage_state=state, locale="uk-UA")
            page = await context.new_page()
            await page.goto(ORDERS_URL, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(7_000)
            if "/cabinet/orders" not in page.url or "/signin" in page.url:
                raise AdapterError("Rozetka.ua: браузерна сесія завершилась — потрібна повторна авторизація")
            orders: list[dict] = []
            page_number = 1
            total_pages = 1
            # Restrict this to the API URL.  Passing both the cabinet and API
            # URLs can produce duplicate cookie names from different domains;
            # the API may then select the stale cabinet value and answer 401.
            browser_cookies = await context.cookies([ORDERS_API_URL])
            cookie_header = "; ".join(
                f"{cookie['name']}={cookie['value']}" for cookie in browser_cookies
            )
            while page_number <= total_pages:
                # Use the API request context attached to the browser context.
                # It shares storage_state cookies but is not subject to the
                # page's cross-origin CORS policy (cabinet-api.rozetka.com.ua).
                user_agent = await page.evaluate("navigator.userAgent")
                api_headers = {
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                    "Origin": "https://rozetka.com.ua",
                    "Referer": ORDERS_URL,
                    "User-Agent": user_agent,
                    "X-Requested-With": "XMLHttpRequest",
                    "Cache-Control": "no-cache",
                }
                # An empty explicit Cookie header would suppress the request
                # context's own cookie jar, so only override it when the
                # browser actually has API-matching cookies.
                if cookie_header:
                    api_headers["Cookie"] = cookie_header
                response = await context.request.get(
                    ORDERS_API_URL,
                    params={"front-type": "xl", "country": "UA", "lang": "ua", "page": page_number},
                    headers=api_headers,
                )
                body_text = await response.text()
                if response.status in {401, 403}:
                    raise AdapterError("Rozetka.ua: браузерна сесія завершилась — потрібна повторна авторизація")
                try:
                    body = json.loads(body_text.lstrip("\ufeff"))
                except Exception as exc:
                    content_type = response.headers.get("content-type", "")
                    lowered = body_text[:500].lower()
                    if response.status in {401, 403} or "sign-in" in lowered or "вхід" in lowered:
                        raise AdapterError("Rozetka.ua: браузерна сесія завершилась — потрібна повторна авторизація") from exc
                    raise AdapterError(
                        f"Rozetka.ua: API замовлень повернув не-JSON (HTTP {response.status}, {content_type or 'без content-type'})"
                    ) from exc
                if not isinstance(body, dict):
                    raise AdapterError("Rozetka.ua: API замовлень повернув неочікувану відповідь")
                data = body.get("data") or {}
                page_orders = data.get("orders") or []
                if not isinstance(page_orders, list):
                    raise AdapterError("Rozetka.ua: у відповіді API відсутній список замовлень")
                orders.extend(order for order in page_orders if isinstance(order, dict))
                paginator = data.get("paginator") or {}
                total_pages = max(total_pages, int(paginator.get("pages") or page_number))
                page_number += 1
                if not page_orders:
                    break
            for order in orders:
                status_by_product = await _warranty_status(context, api_headers, order)
                if not status_by_product:
                    continue
                order["_warranty_available"] = any(
                    bool(status.get("availability_warranty")) for status in status_by_product.values()
                )
                detail = order.get("order_detailed") if isinstance(order.get("order_detailed"), dict) else {}
                for product in detail.get("order_products") or []:
                    if not isinstance(product, dict):
                        continue
                    status = status_by_product.get(str(product.get("id")))
                    if status is not None:
                        product["_warranty_available"] = bool(status.get("availability_warranty"))
                        product["_warranty_text"] = status.get("message")
            unique_orders: dict[str, dict] = {}
            for order in orders:
                key = str(_first(order, "number", "order_number", "orderNumber", "id", "order_id", "orderId", default=""))
                if key:
                    unique_orders[key] = order
            receipts = [_receipt(order) for order in unique_orders.values()]
            # Product/order payloads usually expose the warranty flag.  The
            # actual electronic card is fetched lazily by get_warranty_document
            # when the user opens it, avoiding one browser request per order.
            return [r for r in receipts if not since or r.purchased_at >= since]
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"Rozetka.ua: помилка збору замовлень: {type(exc).__name__}: {exc}") from exc
        finally:
            with_context = locals().get("context")
            with_browser = locals().get("browser")
            if with_context:
                await with_context.close()
            if with_browser:
                await with_browser.close()
            await playwright.stop()

    async def get_warranty_document(self, access_token: str | None, extra: dict, external_id: str) -> bytes:
        """Download the electronic warranty card shown in the order cabinet."""
        if not access_token:
            raise AdapterError("Rozetka.ua: сесія відсутня — потрібна повторна авторизація")
        try:
            state = json.loads(access_token)
            if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
                raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AdapterError("Rozetka.ua: збережена сесія пошкоджена — увійдіть повторно") from exc
        playwright = await async_playwright().start()
        browser = context = None
        try:
            browser = await playwright.chromium.launch(
                headless=not bool(os.environ.get("DISPLAY")), channel="chrome",
            )
            context = await browser.new_context(storage_state=state, locale="uk-UA", accept_downloads=True)
            page = await context.new_page()
            await page.goto(f"https://rozetka.com.ua/ua/cabinet/orders/{external_id}/", wait_until="domcontentloaded", timeout=60_000)
            if "/signin" in page.url:
                raise AdapterError("Rozetka.ua: браузерна сесія завершилась — потрібна повторна авторизація")
            result = await page.evaluate("""async () => {
              const links = [...document.querySelectorAll('a[href]')]
                .map(a => a.href).filter(h => /(warranty|guarantee|garant|талон)/i.test(h));
              for (const url of links) {
                try {
                  const response = await fetch(url, {credentials: 'include'});
                  if (response.ok) {
                    const bytes = new Uint8Array(await response.arrayBuffer());
                    let binary = ''; for (const byte of bytes) binary += String.fromCharCode(byte);
                    return btoa(binary);
                  }
                } catch (_) {}
              }
              return null;
            }""")
            if isinstance(result, str) and result:
                content = base64.b64decode(result)
                if content.startswith(b"%PDF"):
                    return content
            raise AdapterError("Rozetka.ua: гарантійний талон для цього замовлення не знайдено")
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"Rozetka.ua: не вдалося завантажити гарантійний талон: {type(exc).__name__}") from exc
        finally:
            if context:
                await context.close()
            if browser:
                await browser.close()
            await playwright.stop()

    def parse_import(self, payload: dict) -> list[NormalizedReceipt]:
        orders = _first(payload, "orders", "purchases", "data", "items")
        if isinstance(orders, dict):
            orders = _first(orders, "orders", "purchases", "items", "results")
        if not isinstance(orders, list):
            raise ValueError("Rozetka.ua: очікується JSON з масивом orders/items")
        return [_receipt(order) for order in orders if isinstance(order, dict)]
