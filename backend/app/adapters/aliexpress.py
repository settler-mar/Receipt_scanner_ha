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

from app.adapters.base import ROTATED_ACCESS_TOKEN_KEY, AdapterError, BaseAdapter, NormalizedItem, NormalizedReceipt
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
    return NormalizedItem(
        name=str(_first(data, "name", "title", "productName", "productTitle", default="Товар AliExpress")).strip(),
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
                items.append({
                    "name": _first(line, "itemTitle", "productTitle", default="Товар AliExpress"),
                    "quantity": _first(line, "quantity", default=1),
                    "unitPrice": _first(line, "itemPriceText", "formatPriceInfo"),
                    "skuId": line.get("skuId"),
                    "productId": line.get("productId"),
                    "itemUrl": line.get("itemDetailUrl"),
                    "imageUrl": line.get("itemImgUrl"),
                    "storeName": store,
                })
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

    @staticmethod
    def _record_key(order: dict) -> str | None:
        value = _first(order, "id", "orderId", "order_id", "orderNumber", "order_number")
        return str(value) if value not in (None, "") else None

    @classmethod
    def _page_collector(cls, max_pages: int) -> PaginatedRecordCollector:
        return PaginatedRecordCollector(
            record_groups=_order_rows,
            record_key=cls._record_key,
            page_info=_dida_page_info,
            max_pages=max_pages,
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
            async with BrowserSession(access_token) as session:
                page = session.page
                assert page is not None
                capture.attach(page)
                await page.goto(ORDERS_URL, wait_until="domcontentloaded", timeout=90_000)
                await page.wait_for_timeout(4_000)
                if any(part in page.url.lower() for part in ("login", "signin")):
                    raise AdapterError("AliExpress: сесія завершилась — увійдіть повторно")
                await capture.drain()

                result = await self._page_collector(max_pages=100).collect(
                    page,
                    capture,
                    paginate=history.load_all,
                    known_keys=history.known_keys,
                    stop_on_known=history.load_all and not backfill,
                )
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
        config = BrowserSessionConfig(block_heavy_assets=True)
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
            result.append(NormalizedReceipt(
                external_id=str(order_id),
                purchased_at=_date(_first(order, "created_at", "createdAt", "createTime", "gmtCreate", "orderTime", "date")),
                total_amount=total,
                currency=str(currency),
                shop_name=str(store),
                payment_type=_first(order, "paymentMethod", "payment_method", "payType"),
                channel="online",
                items=items,
                receipt_url=_absolute_url(_first(
                    order, "orderUrl", "order_url", "url",
                    default=f"https://www.aliexpress.com/p/order/detail.html?orderId={order_id}",
                )),
                order_status=status,
                tracking_numbers=_tracking(order),
                raw=order,
            ))
        return result
