"""Vencon cabinet integration: password login, orders, receipts and catalog."""
from __future__ import annotations

import calendar
import datetime as dt
import html
import io
import json
import re
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from pypdf import PdfReader

from app.adapters.base import (
    AdapterError,
    AuthResult,
    BaseAdapter,
    CatalogProduct,
    LoyaltyBalance,
    NormalizedItem,
    NormalizedReceipt,
)
from app.adapters.html_tree import number_in, text_of, tree as _tree
from app.core.config import BASE_DIR
from app.core.http import AsyncClient, RequestException


BASE_URL = "https://vencon.ua"
ORDERS_URL = f"{BASE_URL}/ua/user/profile/orders"
RECEIPT_DIR = BASE_DIR / "data" / "receipts"
KYIV = ZoneInfo("Europe/Kyiv")


def _text(fragment: str) -> str:
    return text_of(fragment)


def _money(fragment: str) -> float:
    value = re.sub(r"[^0-9,.]", "", _text(fragment)).replace(",", ".")
    return float(value or 0)


def _number(fragment: str | None) -> float | None:
    return number_in(fragment)


def _parse_search(html_text: str) -> list[CatalogProduct]:
    products: list[CatalogProduct] = []
    for card in re.findall(r"<article\b.*?</article>", html_text, flags=re.S | re.I):
        title = re.search(
            r'<a[^>]*class="[^"]*product-title[^"]*"[^>]*href="([^"]+)"[^>]*title="([^"]+)"',
            card, flags=re.S | re.I,
        )
        price = re.search(r'<span[^>]*class=(?:"actual"|actual)[^>]*>(.*?)</span>', card, flags=re.S | re.I)
        if not title or not price:
            continue
        image = re.search(r'<img[^>]*(?:src|data-src)="([^"]+)"[^>]*>', card, flags=re.S | re.I)
        availability = re.search(
            r'<div[^>]*class=(?:"[^"]*availability-label[^"]*"|availability-label)[^>]*>(.*?)</div>',
            card, flags=re.S | re.I,
        )
        availability_text = _text(availability.group(1)).lower() if availability else ""
        products.append(CatalogProduct(
            name=html.unescape(title.group(2)),
            price=_money(price.group(1)),
            product_url=urljoin(BASE_URL, title.group(1)),
            image_url=urljoin(BASE_URL, image.group(1)) if image else None,
            in_stock=not any(word in availability_text for word in ("немає", "закінчився", "очікується")),
        ))
    return products[:30]


def _parse_balance(html_text: str) -> float:
    plain = _text(html_text)
    patterns = (
        r"(\d[\d\s]*(?:[,.]\d+)?)\s*бонус(?:и|ів)?\s+на\s+рахунку",
        r"на\s+рахунку\s+(\d[\d\s]*(?:[,.]\d+)?)\s*бонус",
        r"\bбонуси\s+(\d[\d\s]*(?:[,.]\d+)?)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, plain, flags=re.I)
        if match:
            return float(match.group(1).replace(" ", "").replace(",", "."))
    raise AdapterError("ВЕНКОН: не вдалося прочитати баланс бонусів")


def _add_months(value: dt.datetime, months: int) -> dt.datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _warranty_months(html_text: str) -> int | None:
    node = _tree(html_text).find_class("warranty-from-manufacturer")
    if not node:
        return None
    match = re.search(r"(\d+)\s*міс", node.text(), flags=re.I)
    return int(match.group(1)) if match else None


def _parse_orders(html_text: str) -> list[NormalizedReceipt]:
    root = _tree(html_text)
    receipts: list[NormalizedReceipt] = []
    for order in root.find_all_class("item-order"):
        order_id = order.attrs.get("id", "")
        if not order_id.startswith("order-"):
            continue
        external_id = order_id.removeprefix("order-")
        date_node = order.find_class("date")
        date_match = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", date_node.text() if date_node else "")
        if not date_match:
            continue
        day, month, year = map(int, date_match.groups())
        purchased_at = dt.datetime(year, month, day, 12, tzinfo=KYIV)

        status_node = order.find_class("status")
        status = status_node.text() if status_node else ""
        completed = status.strip().lower() == "виконано"
        header = order.find_class("block-title")
        total_node = header.find_class("total-sum") if header else None
        total_amount = _number(total_node.text() if total_node else None) or 0

        payment_node = order.find_class("pay-info")
        payment = payment_node.text() if payment_node else None
        if payment:
            payment = re.sub(r"^Оплата:\s*", "", payment, flags=re.I)
            payment = re.sub(r"\s+Оплачено$", "", payment, flags=re.I).strip()

        earned_node = order.find_class("accrued-bonuses-count")
        spent_node = order.find_class("spent-bonuses-count")
        bonuses = _number(earned_node.text()) if earned_node else None
        bonuses_used = _number(spent_node.text()) if spent_node else None
        receipt_node = order.find_class("download-receipt-link")
        receipt_url = urljoin(BASE_URL, receipt_node.attrs.get("href", "")) if receipt_node else None

        items: list[NormalizedItem] = []
        for product in order.find_all_class("product"):
            title_node = product.find_class("title")
            if not title_node:
                continue
            quantity_node = product.find_class("count-value")
            quantity = _number(quantity_node.text()) if quantity_node else 1
            quantity = quantity if quantity is not None else 1
            actual_node = product.find_class("actual")
            old_node = product.find_class("old-price")
            actual = _number(actual_node.text()) if actual_node else None
            old = _number(old_node.text()) if old_node else None
            unit_price = actual if actual is not None else (old or 0)

            image_node = next((node for node in product.descendants() if node.tag == "img"), None)
            image_path = None
            if image_node:
                image_path = image_node.attrs.get("src") or image_node.attrs.get("data-src")
                if not image_path and image_node.attrs.get("data-srcset"):
                    image_path = image_node.attrs["data-srcset"].split()[0]
            image_url = urljoin(BASE_URL, image_path) if image_path else None
            href = title_node.attrs.get("href")
            items.append(NormalizedItem(
                name=title_node.text(), quantity=quantity, unit="шт", unit_price=unit_price,
                total_price=unit_price * quantity,
                discount=max((old - unit_price) * quantity, 0) if old is not None else 0,
                product_url=urljoin(BASE_URL, href) if href else None,
                product_image_url=image_url, product_image_source_url=image_url,
            ))

        receipts.append(NormalizedReceipt(
            external_id=external_id, purchased_at=purchased_at, total_amount=total_amount,
            shop_name="ВЕНКОН", payment_type=payment, channel="online", items=items,
            total_discount=sum(item.discount for item in items), bonuses=bonuses,
            bonuses_used=bonuses_used, receipt_url=receipt_url,
            excluded=not completed,
            exclusion_reason=None if completed else f"Статус замовлення: {status or 'невідомий'}",
            raw={"status": status},
        ))
    return receipts


def _format_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if digits.startswith("0") and len(digits) == 10:
        digits = "38" + digits
    if len(digits) != 12 or not digits.startswith("380"):
        raise AdapterError("ВЕНКОН: вкажіть телефон у форматі +380XXXXXXXXX")
    return f"+380({digits[3:5]})-{digits[5:8]}-{digits[8:10]}-{digits[10:12]}"


def _cookies_from_token(access_token: str) -> dict[str, str]:
    try:
        cookies = json.loads(access_token)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AdapterError("ВЕНКОН: пошкоджена сесія, увійдіть повторно") from exc
    if not isinstance(cookies, dict) or not cookies:
        raise AdapterError("ВЕНКОН: порожня сесія, увійдіть повторно")
    return {str(key): str(value) for key, value in cookies.items()}


def _is_authenticated(response) -> bool:
    final_url = str(getattr(response, "url", ""))
    return response.status_code == 200 and "/user/profile/orders" in final_url and "SignInForm[password]" not in response.text


async def _save_receipt(client: AsyncClient, url: str, external_id: str) -> str | None:
    try:
        response = await client.get(url, follow_redirects=True)
    except Exception:
        return None
    if response.status_code != 200 or not response.content.startswith(b"%PDF"):
        return None
    RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"vencon-{external_id}.pdf"
    (RECEIPT_DIR / filename).write_bytes(response.content)
    return filename


def _fiscal_pdf_items(filename: str) -> list[NormalizedItem]:
    """Read paid lines from Vencon's fiscal PDF when order HTML is stale."""
    try:
        path = RECEIPT_DIR / filename
        text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    except Exception:
        return []
    items: list[NormalizedItem] = []
    lines = [line.strip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        match = re.match(r"(?P<qty>\d+(?:[.,]\d+)?)\s+x\s+(?P<price>\d+(?:[.,]\d+)?)", line)
        if not match:
            continue
        qty = float(match.group("qty").replace(",", "."))
        price = float(match.group("price").replace(",", "."))
        name_lines: list[str] = []
        for candidate in lines[index + 1:]:
            if re.match(r"^\d+(?:[.,]\d+)?\s+[АA]$", candidate) or candidate.startswith("ТЕРМІНАЛ"):
                break
            if candidate:
                name_lines.append(candidate)
        name = " ".join(name_lines).strip()
        if name and price:
            items.append(NormalizedItem(name=name, quantity=qty, unit="шт", unit_price=price / qty, total_price=price))
    return items


class VenconAdapter(BaseAdapter):
    store_code = "vencon"
    catalog_search_requires_auth = False

    async def login_with_password(self, phone_number: str, password: str) -> AuthResult:
        phone = _format_phone(phone_number)
        headers = {"Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8", "Accept-Language": "uk-UA,uk;q=0.9"}
        try:
            async with AsyncClient(timeout=25, headers=headers) as client:
                await client.warmup(f"{BASE_URL}/ua", follow_redirects=True)
                await client.get(f"{BASE_URL}/ua/auth/render-auth-modal", follow_redirects=True)
                response = await client.post(
                    f"{BASE_URL}/ua/auth/sign-in",
                    data={"SignInForm[phone]": phone, "SignInForm[password]": password},
                    headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{BASE_URL}/ua"},
                    follow_redirects=True,
                )
                if response.status_code >= 400:
                    raise AdapterError("ВЕНКОН: сервер відхилив вхід")
                orders = await client.get(ORDERS_URL, follow_redirects=True)
                if not _is_authenticated(orders):
                    raise AdapterError("ВЕНКОН: невірний телефон або пароль; якщо запитано SMS, завершіть перевірку на сайті")
                session = json.dumps(client.cookies, ensure_ascii=False, separators=(",", ":"))
        except RequestException as exc:
            raise AdapterError("ВЕНКОН: не вдалося з'єднатися із сайтом") from exc
        return AuthResult(access_token=session, extra={"session_kind": "cookies"})

    async def fetch_receipts(self, access_token: str, extra: dict, since: dt.datetime | None = None) -> list[NormalizedReceipt]:
        cookies = _cookies_from_token(access_token)
        try:
            async with AsyncClient(timeout=30, cookies=cookies, headers={"Accept-Language": "uk-UA,uk;q=0.9"}) as client:
                response = await client.get(ORDERS_URL, follow_redirects=True)
                if not _is_authenticated(response):
                    raise AdapterError("ВЕНКОН: сесія завершилася, увійдіть повторно")
                receipts = _parse_orders(response.text)
                since_aware = since.replace(tzinfo=dt.timezone.utc) if since and since.tzinfo is None else since
                selected = [r for r in receipts if r.excluded or since_aware is None or r.purchased_at.astimezone(dt.timezone.utc) >= since_aware]
                checked_at = dt.datetime.now(dt.timezone.utc)
                for receipt in selected:
                    if receipt.excluded:
                        continue
                    for item in receipt.items:
                        if not item.product_url:
                            continue
                        try:
                            page = await client.get(item.product_url, follow_redirects=True)
                            months = _warranty_months(page.text) if page.status_code == 200 else None
                        except RequestException:
                            months = None
                        item.warranty_checked_at = checked_at
                        item.has_warranty = months is not None
                        if months is not None:
                            item.warranty_text = f"{months} міс. гарантії від виробника"
                            item.warranty_expires_at = _add_months(receipt.purchased_at, months)
                    warranty_items = [item for item in receipt.items if item.has_warranty]
                    receipt.has_warranty = bool(warranty_items)
                    expiries = [item.warranty_expires_at for item in warranty_items if item.warranty_expires_at]
                    receipt.warranty_expires_at = max(expiries) if expiries else None
                    if receipt.receipt_url:
                        filename = await _save_receipt(client, receipt.receipt_url, receipt.external_id)
                        if filename:
                            receipt.raw["receipt_file"] = filename
                            fiscal_items = _fiscal_pdf_items(filename)
                            if fiscal_items:
                                receipt.items = fiscal_items
                                receipt.total_amount = round(sum(item.total_price for item in fiscal_items), 2)
                                receipt.raw["fiscal_pdf_items"] = [
                                    {"name": item.name, "quantity": item.quantity, "total_price": item.total_price}
                                    for item in fiscal_items
                                ]
        except RequestException as exc:
            raise AdapterError("ВЕНКОН: не вдалося завантажити замовлення") from exc
        return selected

    async def get_balance(self, access_token: str, extra: dict) -> LoyaltyBalance:
        cookies = _cookies_from_token(access_token)
        try:
            async with AsyncClient(timeout=20, cookies=cookies, headers={"Accept-Language": "uk-UA,uk;q=0.9"}) as client:
                response = await client.get(ORDERS_URL, follow_redirects=True)
        except RequestException as exc:
            raise AdapterError("ВЕНКОН: не вдалося оновити баланс") from exc
        if not _is_authenticated(response):
            raise AdapterError("ВЕНКОН: сесія завершилася, увійдіть повторно")
        return LoyaltyBalance(total=_parse_balance(response.text), currency="UAH")

    def parse_import(self, payload: dict) -> list[NormalizedReceipt]:
        orders = payload.get("orders")
        if not isinstance(orders, list):
            raise ValueError("Vencon import expects an 'orders' array")
        receipts: list[NormalizedReceipt] = []
        for order in orders:
            if not isinstance(order, dict) or order.get("status") not in {"completed", "Виконано"}:
                continue
            items = [NormalizedItem(
                name=str(item["name"]).strip(), quantity=float(item.get("quantity", 1)), unit=item.get("unit", "шт"),
                unit_price=float(item.get("unit_price", item["total_price"])), total_price=float(item["total_price"]),
                discount=float(item.get("discount", 0)), product_url=item.get("product_url"),
                product_image_url=item.get("product_image_url"), product_image_source_url=item.get("product_image_url"),
                has_warranty=item.get("has_warranty"), warranty_text=item.get("warranty_text"),
                warranty_expires_at=(dt.datetime.fromisoformat(str(item["warranty_expires_at"]).replace("Z", "+00:00")) if item.get("warranty_expires_at") else None),
            ) for item in order.get("items", [])]
            receipts.append(NormalizedReceipt(
                external_id=str(order["number"]), purchased_at=dt.datetime.fromisoformat(str(order["purchased_at"]).replace("Z", "+00:00")),
                total_amount=float(order["total_amount"]), shop_name="ВЕНКОН", payment_type=order.get("payment_type"), channel="online",
                items=items, total_discount=float(order.get("total_discount", sum(i.discount for i in items))),
                bonuses=order.get("bonuses"), bonuses_used=order.get("bonuses_used"), receipt_url=order.get("receipt_url"),
                has_warranty=order.get("has_warranty"),
                warranty_expires_at=(dt.datetime.fromisoformat(str(order["warranty_expires_at"]).replace("Z", "+00:00")) if order.get("warranty_expires_at") else None),
                raw=order,
            ))
        return receipts

    async def search_catalog(self, access_token: str | None, extra: dict, query: str) -> list[CatalogProduct]:
        query = query.strip()
        if not query:
            return []
        try:
            async with AsyncClient(timeout=20, headers={"Accept": "text/html"}) as client:
                response = await client.get(f"{BASE_URL}/ua/search", params={"q": query})
        except RequestException as exc:
            raise AdapterError("ВЕНКОН: пошук тимчасово недоступний") from exc
        if response.status_code != 200:
            raise AdapterError(f"ВЕНКОН: пошук повернув {response.status_code}")
        return _parse_search(response.text)
