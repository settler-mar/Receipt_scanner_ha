"""AliExpress order import.

AliExpress has no stable public buyer-orders API.  This adapter intentionally
supports captured/exported JSON only; it does not implement catalog search.
The parser is liberal about field names because the web and mobile order APIs
use different wrappers and names.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from typing import Any

from app.adapters.base import (
    ROTATED_ACCESS_TOKEN_KEY,
    AdapterError,
    BaseAdapter,
    NormalizedItem,
    NormalizedReceipt,
    NormalizedReceiptComponent,
)
from app.integrations import (
    BrowserSession,
    BrowserSessionConfig,
    DeliveryUpdate,
    FullHistoryImport,
    JsonResponseCapture,
    OrderDeliveryUpdater,
    PageInfo,
    PaginatedRecordCollector,
)

ORDERS_URL = "https://www.aliexpress.com/p/order/index.html"
RECENT_PAGE_TIMEOUT_MS = 90_000
FULL_HISTORY_PAGE_TIMEOUT_MS = 180_000
RECENT_INITIAL_WAIT_MS = 4_000
FULL_HISTORY_INITIAL_WAIT_MS = 10_000
RECENT_MAX_PAGES = 100
FULL_HISTORY_MAX_PAGES = 300
RECENT_CLICK_TIMEOUT_MS = 10_000
FULL_HISTORY_CLICK_TIMEOUT_MS = 30_000
RECENT_RESPONSE_WAIT_ATTEMPTS = 40
FULL_HISTORY_RESPONSE_WAIT_ATTEMPTS = 120
DETAIL_PAGE_TIMEOUT_MS = 120_000
DETAIL_INITIAL_WAIT_MS = 6_000
DETAIL_BATCH_SIZE = 100
logger = logging.getLogger(__name__)
# Compatibility aliases for callers written before the generic history class.
FULL_HISTORY_MARKER = FullHistoryImport.marker_key("aliexpress")
LOAD_ALL_OPTION = FullHistoryImport.LOAD_ALL_OPTION
STOP_ORDER_IDS_OPTION = FullHistoryImport.STOP_KEYS_OPTION
HISTORY_COMPLETE_RESULT = FullHistoryImport.COMPLETE_RESULT


def _first(data: dict, *names: str, default: Any = None) -> Any:
    for name in names:
        value = data.get(name)
        if value not in (None, ""):
            return value
    return default


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d[\d\s.,]*", str(value or ""))
    if match is None:
        return default
    text = re.sub(r"\s+", "", match.group(0))
    if "," in text and "." in text:
        decimal = "," if text.rfind(",") > text.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        text = text.replace(thousands, "").replace(decimal, ".")
    else:
        text = text.replace(",", ".")
        if text.count(".") > 1:
            parts = text.split(".")
            text = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return float(text)
    except ValueError:
        return default


def _date(value: Any) -> dt.datetime:
    if isinstance(value, (int, float)):
        stamp = float(value) / 1000 if value > 10_000_000_000 else float(value)
        return dt.datetime.fromtimestamp(stamp, tz=dt.timezone.utc)
    text = str(value or "").strip().replace("Z", "+00:00")
    text = re.sub(r"^(?:date|дата)\s*:\s*", "", text, flags=re.I)
    if not text:
        raise ValueError("AliExpress: у замовленні відсутня дата")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        for fmt in (
            "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y",
            "%b %d, %Y", "%B %d, %Y",
        ):
            try:
                parsed = dt.datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"AliExpress: невідомий формат дати: {value}") from None
    return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    out: list[str] = []
    for entry in values:
        if isinstance(entry, dict):
            entry = _first(entry, "tracking_number", "trackingNumber", "mailNo", "logisticsNo", "number", "code")
        text = str(entry or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _tracking(order: dict) -> list[str]:
    result: list[str] = []
    for key in ("tracking_numbers", "trackingNumbers", "tracking_number", "trackingNumber", "mailNo", "logisticsNo"):
        result.extend(_strings(order.get(key)))
    for key in ("logistics", "shipments", "packages", "trackingInfo", "logisticsInfo"):
        result.extend(_strings(order.get(key)))
    return list(dict.fromkeys(result))


def _images(item: dict) -> list[str]:
    result: list[str] = []
    for key in ("images", "imageUrls", "productImages", "image", "imageUrl", "productImage", "skuImage"):
        value = item.get(key)
        values = value if isinstance(value, list) else [value]
        for image in values:
            if isinstance(image, dict):
                image = _first(image, "url", "src", "imageUrl")
            url = str(image or "").strip()
            if url.startswith("//"):
                url = "https:" + url
            if url.startswith("http") and url not in result:
                result.append(url)
    return result


def _absolute_url(value: Any) -> str | None:
    url = str(value or "").strip()
    if not url:
        return None
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://www.aliexpress.com" + url
    return url


_VARIANT_KEYS = (
    "selectedSku", "selectedSKU", "selected_sku", "skuInfo", "sku_info",
    "skuText", "sku_text", "skuDescription", "skuDesc", "sku_desc",
    "skuAttr", "sku_attr", "skuAttributes", "sku_attributes",
    "skuProperties", "sku_properties", "skuPropertyValues", "properties",
    "attributes", "variation", "variant", "productSku", "product_sku",
)


def _variant_value(value: Any) -> str:
    """Turn AliExpress' several selected-SKU shapes into display text."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        parts = [_variant_value(entry) for entry in value]
        return "; ".join(part for part in parts if part)
    if isinstance(value, dict):
        # Property objects are usually {propertyName, propertyValue}; the
        # aliases below cover the mobile/web order payloads as well.
        label = _first(value, "propertyName", "property_name", "name", "label", "key", "skuName")
        selected = _first(
            value, "propertyValue", "property_value", "value", "text",
            "displayValue", "display_value", "skuValue", "valueName",
        )
        if label is not None and selected is not None:
            return f"{label}: {selected}"
        for key in ("skuPropertyValues", "propertyValues", "values", "items", "list"):
            if key in value:
                nested = _variant_value(value[key])
                if nested:
                    return nested
        parts = []
        for key, nested_value in value.items():
            if key.lower() in {"id", "skuId".lower(), "propertyId".lower()}:
                continue
            text = _variant_value(nested_value)
            if text:
                parts.append(f"{key}: {text}")
        return "; ".join(parts)
    return ""


def _variant_text(data: dict) -> str | None:
    parts = []
    for key in _VARIANT_KEYS:
        if key not in data:
            continue
        text = _variant_value(data[key])
        if text and text.casefold() not in {part.casefold() for part in parts}:
            parts.append(text)
    return "; ".join(parts) or None


def _item(data: dict, order_store: str | None) -> NormalizedItem:
    quantity = _number(_first(data, "quantity", "count", "productCount", "skuCount"), 1) or 1
    unit_price = _number(_first(data, "unit_price", "unitPrice", "price", "productPrice", "skuPrice"))
    total = _number(_first(data, "total_price", "totalPrice", "total", "itemAmount"), unit_price * quantity)
    images = _images(data)
    product_id = _first(data, "productId", "product_id", "itemId", "item_id")
    url = _first(data, "product_url", "productUrl", "itemUrl", "url")
    if not url and product_id:
        url = f"https://www.aliexpress.com/item/{product_id}.html"
    elif isinstance(url, str) and url.startswith("//"):
        url = "https:" + url
    elif isinstance(url, str) and url.startswith("/"):
        url = "https://www.aliexpress.com" + url
    name = str(_first(data, "name", "title", "productName", "productTitle", default="Товар AliExpress")).strip()
    variant = _variant_text(data)
    if variant and variant.casefold() not in name.casefold():
        name = f"{name} ({variant})"
    return NormalizedItem(
        name=name,
        quantity=quantity,
        unit=_first(data, "unit", default="шт"),
        unit_price=unit_price,
        total_price=total,
        discount=_number(_first(data, "discount", "discountAmount")),
        barcode=str(_first(data, "skuId", "sku_id", "productId", "product_id", default="")) or None,
        product_url=url,
        product_image_url=images[0] if images else None,
        merchant_name=_first(data, "storeName", "store_name", "sellerName", "seller_name", default=order_store),
        product_image_urls=images,
    )


def _orders(payload: dict) -> list[dict]:
    candidates: Any = payload
    for _ in range(6):
        if isinstance(candidates, list):
            return [x for x in candidates if isinstance(x, dict)]
        if not isinstance(candidates, dict):
            break
        found = next((candidates[k] for k in ("orders", "orderList", "items", "list", "resultList") if isinstance(candidates.get(k), list)), None)
        if found is not None:
            return [x for x in found if isinstance(x, dict)]
        nested = next((candidates[k] for k in ("data", "result", "module") if isinstance(candidates.get(k), dict)), None)
        if nested is None:
            break
        candidates = nested
    dida = _dida_orders(payload)
    if dida:
        return dida
    raise ValueError("AliExpress: очікується JSON з масивом orders/orderList")


def _find_order_lists(value: Any, depth: int = 0) -> list[list[dict]]:
    """Find order arrays in captured web responses without mistaking product arrays for orders."""
    if depth > 8:
        return []
    found: list[list[dict]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"orders", "orderList", "resultList"} and isinstance(child, list):
                orders = [row for row in child if isinstance(row, dict)]
                if orders:
                    found.append(orders)
            found.extend(_find_order_lists(child, depth + 1))
    elif isinstance(value, list):
        for child in value:
            found.extend(_find_order_lists(child, depth + 1))
    return found


def _dida_orders(value: Any, depth: int = 0) -> list[dict]:
    """Extract the current PC order-list component format (ae-dida/mtop)."""
    if depth > 8:
        return []
    found: list[dict] = []
    if isinstance(value, dict):
        fields = value.get("fields")
        if (
            isinstance(fields, dict)
            and fields.get("orderId") not in (None, "")
            and isinstance(fields.get("orderLines"), list)
        ):
            store = _first(fields, "storeName", "shopName", default="AliExpress")
            tracking_url = None
            for button in fields.get("buttons") or []:
                if not isinstance(button, dict):
                    continue
                label = str(_first(button, "text", "title", default=""))
                if re.search(r"track(?:ing)?(?:\s+status|\s+order)?", label, re.I):
                    tracking_url = _first(button, "href", "url")
                    if tracking_url:
                        break
            items = []
            for line in fields["orderLines"]:
                if not isinstance(line, dict):
                    continue
                item = {
                    "name": _first(line, "itemTitle", "productTitle", default="Товар AliExpress"),
                    "quantity": _first(line, "quantity", default=1),
                    "unitPrice": _first(line, "itemPriceText", "formatPriceInfo"),
                    "skuId": line.get("skuId"),
                    "productId": line.get("productId"),
                    "itemUrl": line.get("itemDetailUrl"),
                    "imageUrl": line.get("itemImgUrl"),
                    "storeName": store,
                }
                for key in _VARIANT_KEYS:
                    if key in line:
                        item[key] = line[key]
                items.append(item)
            found.append({
                "orderId": fields["orderId"],
                "date": fields.get("orderDateText"),
                "statusText": _first(fields, "statusText", "statusTitle"),
                "storeName": store,
                "totalAmount": _first(fields, "totalPriceText", "formatPriceInfo"),
                "currencyCode": _first(fields, "currencyCode", "baseCurrency", default="USD"),
                "orderUrl": fields.get("orderDetailUrl"),
                "trackingUrl": tracking_url,
                "items": items,
                # Keep the original server-side fields. The normalized keys
                # above are intentionally compact for the importer, while the
                # raw-response archive needs prices, fees and adjustments that
                # are not yet understood by us.
                "_raw_fields": fields,
            })
            return found
        for child in value.values():
            found.extend(_dida_orders(child, depth + 1))
    elif isinstance(value, list):
        for child in value:
            found.extend(_dida_orders(child, depth + 1))
    return found


def _order_rows(value: Any) -> list[list[dict]]:
    rows = _find_order_lists(value)
    dida = _dida_orders(value)
    if dida:
        rows.append(dida)
    return rows


def _dida_page_info(value: Any, depth: int = 0) -> PageInfo | None:
    if depth > 8:
        return None
    if isinstance(value, dict):
        fields = value.get("fields")
        if isinstance(fields, dict) and {"hasMore", "pageIndex", "pageSize"} <= fields.keys():
            return PageInfo(
                has_more=bool(fields.get("hasMore")),
                button_text=str(fields.get("hasMoreText") or "View orders"),
                page_index=int(fields.get("pageIndex") or 1),
            )
        for child in value.values():
            result = _dida_page_info(child, depth + 1)
            if result is not None:
                return result
    elif isinstance(value, list):
        for child in value:
            result = _dida_page_info(child, depth + 1)
            if result is not None:
                return result
    return None


def _order_needs_detail(order: dict) -> bool:
    """Prioritize orders whose list card cannot explain its final total."""
    fields = order.get("_raw_fields") if isinstance(order.get("_raw_fields"), dict) else order
    total_text = _first(fields, "totalPriceText", "formatPriceInfo")
    if total_text in (None, ""):
        return True
    total = _number(total_text, default=-1)
    if total < 0:
        return True
    line_sum = 0.0
    for line in fields.get("orderLines") or order.get("items") or []:
        if not isinstance(line, dict):
            continue
        price = _number(_first(line, "itemPriceText", "formatPriceInfo", "totalPrice", "total_price"))
        quantity = _number(_first(line, "quantity", "count"), default=1) or 1
        line_sum += price * quantity
    return abs(total - line_sum) > 0.02


def _has_detail_price_breakdown(order: dict) -> bool:
    fields = order.get("_raw_fields") if isinstance(order.get("_raw_fields"), dict) else {}
    responses = fields.get("_detail_responses") or []

    def visit(value: Any, depth: int = 0) -> bool:
        if depth > 10:
            return False
        if isinstance(value, dict):
            price_details = value.get("priceDetails")
            total_price = value.get("totalPrice")
            if isinstance(price_details, list) and isinstance(total_price, dict):
                return True
            return any(visit(child, depth + 1) for child in value.values())
        if isinstance(value, list):
            return any(visit(child, depth + 1) for child in value)
        return False

    return visit(responses)


def _component_kind(label: str) -> str:
    normalized = label.casefold()
    if "shipping" in normalized or "delivery" in normalized or "достав" in normalized:
        return "delivery"
    if "tax" in normalized or "vat" in normalized or "подат" in normalized:
        return "tax"
    if any(token in normalized for token in ("coupon", "discount", "coins", "зниж", "купон")):
        return "discount"
    if "fee" in normalized or "service" in normalized or "коміс" in normalized:
        return "fee"
    if "adjustment" in normalized or "кориг" in normalized:
        return "adjustment"
    return "other"


def _detail_components(order: dict) -> list[NormalizedReceiptComponent]:
    """Extract provider order components without assigning them to products."""
    fields = order.get("_raw_fields") if isinstance(order.get("_raw_fields"), dict) else order
    responses = fields.get("_detail_responses") or []

    def visit(value: Any, depth: int = 0) -> list[dict]:
        if depth > 10:
            return []
        if isinstance(value, dict):
            details = value.get("priceDetails")
            if isinstance(details, list):
                return [row for row in details if isinstance(row, dict)]
            for child in value.values():
                found = visit(child, depth + 1)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = visit(child, depth + 1)
                if found:
                    return found
        return []

    components: list[NormalizedReceiptComponent] = []
    for row in visit(responses):
        label = str(_first(row, "title", "label", "name", default="Other charge")).strip()
        if not label or label.casefold() == "subtotal":
            continue
        raw_value = _first(row, "value", "amount", "price")
        amount = _number(raw_value)
        if str(raw_value or "").strip().startswith(("-", "−")):
            amount = -abs(amount)
        if abs(amount) < 0.005:
            continue
        components.append(NormalizedReceiptComponent(
            kind=_component_kind(label),
            code=re.sub(r"[^a-z0-9]+", "_", label.casefold()).strip("_") or None,
            label=label,
            amount=round(amount, 2),
            currency="UAH",
            source="aliexpress.order_detail",
            raw=row,
        ))
    return components


def _receipt_adjustment(
    order: dict,
    total: float,
    items: list[NormalizedItem],
    components: list[NormalizedReceiptComponent] | None = None,
) -> tuple[float, str | None]:
    line_sum = round(sum(float(item.total_price or 0) for item in items), 2)
    component_sum = round(sum(float(component.amount or 0) for component in (components or [])), 2)
    residual = round(total - line_sum - component_sum, 2)
    # Backward-compatible helper behavior for callers that only ask whether a
    # detail payload exists; the full parser passes an explicit component list
    # and can therefore preserve any still-unparsed residual as an adjustment.
    if components is None and _has_detail_price_breakdown(order):
        return 0.0, None
    if abs(residual) < 0.01:
        return 0.0, None
    reason = (
        "AliExpress: в деталях заказа остался нераспознанный компонент"
        if _has_detail_price_breakdown(order)
        else "AliExpress: причина расхождения не определена по доступным данным заказа"
    )
    return residual, reason


async def _capture_order_details(page, capture: JsonResponseCapture, order: dict) -> list[Any]:
    order_url = _absolute_url(_first(order, "orderUrl", "order_url", "url"))
    if not order_url:
        return []
    before = len(capture.payloads)
    try:
        await page.goto(order_url, wait_until="domcontentloaded", timeout=DETAIL_PAGE_TIMEOUT_MS)
        await page.wait_for_timeout(DETAIL_INITIAL_WAIT_MS)
        await capture.drain()
        return capture.payloads[before:]
    except Exception:
        logger.warning("AliExpress order detail could not be captured", exc_info=True)
        await capture.drain()
        return capture.payloads[before:]


def is_terminal_order_status(status: str | None) -> bool:
    """Compatibility wrapper; new integrations should use OrderDeliveryUpdater."""
    return OrderDeliveryUpdater().is_terminal(status)


async def _tracking_numbers_from_page(context, url: str) -> list[str]:
    page = await context.new_page()
    try:
        await page.goto(_absolute_url(url), wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(2_000)
        text = await page.locator("body").inner_text()
        numbers = re.findall(
            r"tracking\s+(?:number|no\.?)\s*:\s*([A-Z0-9][A-Z0-9-]{5,})",
            text,
            flags=re.I,
        )
        return list(dict.fromkeys(number.strip() for number in numbers if number.strip()))
    except Exception:
        logger.warning("AliExpress tracking page could not be read", exc_info=True)
        return []
    finally:
        await page.close()


class AliExpressAdapter(BaseAdapter):
    store_code = "aliexpress"
    login_field = "phone_or_email"

    @staticmethod
    def _record_key(order: dict) -> str | None:
        value = _first(order, "id", "orderId", "order_id", "orderNumber", "order_number")
        return str(value) if value not in (None, "") else None

    @classmethod
    def _page_collector(
        cls,
        max_pages: int,
        *,
        click_timeout_ms: int = RECENT_CLICK_TIMEOUT_MS,
        response_wait_attempts: int = RECENT_RESPONSE_WAIT_ATTEMPTS,
    ) -> PaginatedRecordCollector:
        return PaginatedRecordCollector(
            record_groups=_order_rows,
            record_key=cls._record_key,
            page_info=_dida_page_info,
            max_pages=max_pages,
            click_timeout_ms=click_timeout_ms,
            response_wait_attempts=response_wait_attempts,
        )

    async def fetch_receipts(self, access_token: str, extra: dict, since=None):
        try:
            BrowserSession.parse_storage_state(access_token)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AdapterError("AliExpress: браузерна сесія пошкоджена — увійдіть повторно") from exc

        history = FullHistoryImport.consume(extra, self.store_code)
        # Existing installations may already contain the first page. The first
        # explicit full-history run must pass those rows and reach the end once;
        # later runs can stop at the first known order.
        backfill = history.initial_backfill

        try:
            capture = JsonResponseCapture(
                lambda url: any(marker in url for marker in ("order", "trade", "mtop")),
                lambda body: bool(_order_rows(body)),
            )
            async with BrowserSession(access_token, BrowserSessionConfig(headless=True)) as session:
                page = session.page
                assert page is not None
                capture.attach(page)
                page_timeout = FULL_HISTORY_PAGE_TIMEOUT_MS if history.load_all else RECENT_PAGE_TIMEOUT_MS
                initial_wait = FULL_HISTORY_INITIAL_WAIT_MS if history.load_all else RECENT_INITIAL_WAIT_MS
                await page.goto(ORDERS_URL, wait_until="domcontentloaded", timeout=page_timeout)
                await page.wait_for_timeout(initial_wait)
                if any(part in page.url.lower() for part in ("login", "signin")):
                    raise AdapterError("AliExpress: сесія завершилась — увійдіть повторно")
                await capture.drain()

                result = await self._page_collector(
                    max_pages=FULL_HISTORY_MAX_PAGES if history.load_all else RECENT_MAX_PAGES,
                    click_timeout_ms=(
                        FULL_HISTORY_CLICK_TIMEOUT_MS if history.load_all else RECENT_CLICK_TIMEOUT_MS
                    ),
                    response_wait_attempts=(
                        FULL_HISTORY_RESPONSE_WAIT_ATTEMPTS
                        if history.load_all else RECENT_RESPONSE_WAIT_ATTEMPTS
                    ),
                ).collect(
                    page,
                    capture,
                    paginate=history.load_all,
                    known_keys=history.known_keys,
                    stop_on_known=history.load_all and not backfill,
                )

                if history.refresh_details and result.records:
                    detail_capture = JsonResponseCapture(
                        lambda url: "mtop.aliexpress.trade.buyer.order.detail" in url,
                    )
                    detail_capture.attach(page)
                    candidates = [
                        order for order in result.records.values()
                        if (
                            _order_needs_detail(order)
                            and str(self._record_key(order)) not in history.detail_known_keys
                        )
                    ][:DETAIL_BATCH_SIZE]
                    for order in candidates:
                        details = await _capture_order_details(page, detail_capture, order)
                        if not details:
                            continue
                        fields = order.get("_raw_fields")
                        if not isinstance(fields, dict):
                            fields = dict(order)
                        enriched = dict(fields)
                        enriched["_detail_responses"] = details
                        order["_raw_fields"] = enriched
                extra[ROTATED_ACCESS_TOKEN_KEY] = await session.serialized_storage_state()
                if not result.records:
                    if result.stopped_on_known:
                        return []
                    raise AdapterError("AliExpress: сторінка відкрилась, але JSON замовлень не знайдено")
                if history.load_all and result.reached_end:
                    FullHistoryImport.mark_complete(extra)
                # Deliberately ignore `since`: old orders can gain another
                # parcel or tracking number long after purchase.
                return self.parse_import({"orders": list(result.records.values())})
        except AdapterError:
            raise
        except Exception as exc:
            logger.exception("AliExpress collection failed")
            raise AdapterError(f"AliExpress: помилка завантаження ({type(exc).__name__})") from exc
    async def refresh_order_tracking(
        self, access_token: str, extra: dict, active_orders: dict[str, str | None],
    ) -> dict[str, DeliveryUpdate]:
        """Daily lightweight refresh for unfinished orders only.

        Order-list pagination stops as soon as every active id has been seen;
        images/fonts/media are blocked. Stored tracking links are used as a
        fallback for active orders that are no longer on the recent list pages.
        Product items are never returned from this path.
        """
        try:
            BrowserSession.parse_storage_state(access_token)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AdapterError("AliExpress: браузерна сесія пошкоджена — увійдіть повторно") from exc

        capture = JsonResponseCapture(
            lambda url: "mtop.aliexpress.trade.buyer.order.list" in url,
            lambda body: bool(_order_rows(body)),
        )
        config = BrowserSessionConfig(headless=True, block_heavy_assets=True)
        async with BrowserSession(access_token, config) as session:
            page = session.page
            context = session.context
            assert page is not None and context is not None
            capture.attach(page)
            await page.goto(ORDERS_URL, wait_until="domcontentloaded", timeout=90_000)
            await page.wait_for_timeout(4_000)
            await capture.drain()

            result = await self._page_collector(max_pages=20).collect(
                page,
                capture,
                paginate=True,
                wanted_keys=set(active_orders),
            )
            updates: dict[str, DeliveryUpdate] = {}
            tracking_urls = dict(active_orders)
            for order_id, order in result.records.items():
                status = _first(order, "statusText", "status_text", "orderStatus", "status")
                updates[order_id] = DeliveryUpdate(status=str(status) if status is not None else None)
                tracking_urls[order_id] = order.get("trackingUrl") or tracking_urls[order_id]

            for order_id, tracking_url in tracking_urls.items():
                update = updates.setdefault(order_id, DeliveryUpdate())
                if tracking_url and not is_terminal_order_status(update.status):
                    update.tracking_numbers = await _tracking_numbers_from_page(context, tracking_url)

            extra[ROTATED_ACCESS_TOKEN_KEY] = await session.serialized_storage_state()
            return updates

    def parse_import(self, payload: dict) -> list[NormalizedReceipt]:
        result: list[NormalizedReceipt] = []
        for order in _orders(payload):
            order_id = _first(order, "id", "orderId", "order_id", "orderNumber", "order_number")
            if order_id is None:
                raise ValueError("AliExpress: у замовленні відсутній номер")
            status_value = _first(order, "statusText", "status_text", "orderStatus", "order_status", "status")
            status = str(status_value).strip() if status_value not in (None, "") else None
            store = _first(order, "storeName", "store_name", "sellerName", "seller_name", "shopName", default="AliExpress")
            raw_items = _first(order, "items", "products", "orderItems", "productList", "childOrderList", default=[])
            items = [_item(item, store) for item in raw_items if isinstance(item, dict)]
            total = _number(_first(order, "total_amount", "totalAmount", "payAmount", "orderAmount", "total"), sum(i.total_price for i in items))
            currency = _first(order, "currency", "currencyCode", "currency_code", default="USD")
            components = _detail_components(order)
            total_adjustment, adjustment_reason = _receipt_adjustment(order, total, items, components)
            total_discount = round(sum(abs(component.amount) for component in components if component.amount < 0), 2)
            result.append(NormalizedReceipt(
                external_id=str(order_id),
                purchased_at=_date(_first(order, "created_at", "createdAt", "createTime", "gmtCreate", "orderTime", "date")),
                total_amount=total,
                currency=str(currency),
                shop_name=str(store),
                payment_type=_first(order, "paymentMethod", "payment_method", "payType"),
                channel="online",
                items=items,
                components=components,
                receipt_url=_absolute_url(_first(
                    order, "orderUrl", "order_url", "url",
                    default=f"https://www.aliexpress.com/p/order/detail.html?orderId={order_id}",
                )),
                order_status=status,
                tracking_numbers=_tracking(order),
                total_discount=total_discount if components else None,
                total_adjustment=total_adjustment,
                adjustment_reason=adjustment_reason,
                raw=order.get("_raw_fields") or order,
            ))
        return result
