"""Foxtrot web-cabinet adapter.

Foxtrot exposes the authenticated cabinet as a cookie-backed web session rather
than a documented JSON API.  The adapter deliberately stores only the session
cookie and request id in encrypted credential ``extra``.
"""
from __future__ import annotations

import datetime as dt
import base64
import calendar
import html
import re
from pathlib import Path
from urllib.parse import quote, urljoin

from app.adapters.base import (AdapterError, AuthResult, BaseAdapter,
    CatalogProduct, LoyaltyBalance, NormalizedItem, NormalizedReceipt)
from app.core.http import AsyncClient
from app.core.time import store_local_to_utc
from app.core.config import BASE_DIR

BASE_URL = "https://www.foxtrot.com.ua"
SEARCH_URL = "https://foxtrot-api.evinent.site/api/search/autocomplete/11/1"
RECEIPT_DIR = BASE_DIR / "data" / "receipts"
WEB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{BASE_URL}/uk/",
    "Origin": BASE_URL,
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
}

def _money(value: str | None) -> float:
    if not value: return 0.0
    value = value.replace(" ", "").replace("\xa0", "").replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", value)
    return float(m.group()) if m else 0.0

def _cookies(extra: dict) -> dict[str, str]:
    return dict(extra.get("cookies") or {})

async def _save_receipt(client: AsyncClient, url: str, external_id: str) -> str | None:
    try:
        response = await client.get(url)
        if response.status_code < 200 or response.status_code >= 300 or not response.content:
            return None
        suffix = ".pdf" if response.content.startswith(b"%PDF") else ".html"
        filename = f"foxtrot-{external_id}{suffix}"
        RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
        (RECEIPT_DIR / filename).write_bytes(response.content)
        return filename
    except Exception:
        return None

def _csrf(text: str) -> str | None:
    m = re.search(r'name="__RequestVerificationToken"[^>]+value="([^"]+)', text)
    return html.unescape(m.group(1)) if m else None


_AMOUNT_RE = r"(-?\d[\d\s\u00a0]*(?:[.,]\d{1,2})?)\s*(?:₴|грн)"


def _plain_text(markup: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", markup))).strip()


def _label_amount(markup: str, label: str) -> float | None:
    text = _plain_text(markup)
    match = re.search(rf"\b{re.escape(label)}\b\s*:?\s*{_AMOUNT_RE}", text, re.I)
    return _money(match.group(1)) if match else None


def _product_href(markup: str) -> str | None:
    for href in re.findall(r'href\s*=\s*["\']([^"\']+)', markup, re.I):
        href = html.unescape(href)
        if re.search(r"/uk/shop/[^?#]+\.html(?:[?#]|$)", href, re.I):
            return href
    return None


def _product_warranty(markup: str) -> str | None:
    text = _plain_text(markup)
    match = re.search(
        r"\bГарантія(?:\s+від\s+виробника)?\s*:?[\s-]*(\d+\s*(?:міс\.?|місяц(?:і|ів)?|рок(?:и|ів)?))",
        text,
        re.I,
    )
    return match.group(0) if match else None


def _product_image(markup: str) -> str | None:
    for pattern in (
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    ):
        match = re.search(pattern, markup, re.I)
        if match:
            return urljoin(BASE_URL, html.unescape(match.group(1)))
    return None


def _warranty_expiry(purchased_at: dt.datetime, warranty_text: str | None) -> dt.datetime | None:
    if not warranty_text:
        return None
    match = re.search(r"(\d+)\s*(міс\.?|місяц(?:і|ів)?|рок(?:и|ів)?)", warranty_text, re.I)
    if not match:
        return None
    duration = int(match.group(1))
    months = duration * 12 if match.group(2).lower().startswith("рок") else duration
    month_index = purchased_at.month - 1 + months
    year = purchased_at.year + month_index // 12
    month = month_index % 12 + 1
    day = min(purchased_at.day, calendar.monthrange(year, month)[1])
    return purchased_at.replace(year=year, month=month, day=day)


def _order_type(block: str) -> str:
    for encoded_url in re.findall(r'data-url=["\']([^"\']+)', block, re.I):
        try:
            padding = "=" * (-len(encoded_url) % 4)
            decoded_url = base64.b64decode(encoded_url + padding).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
        if "/account/orderdetails/" not in decoded_url:
            continue
        match = re.search(r"[?&]type=([^&]+)", decoded_url, re.I)
        if match:
            return match.group(1).upper()
    return "WEB"

class FoxtrotAdapter(BaseAdapter):
    store_code = "foxtrot"

    async def start_login(self, phone_number: str) -> AuthResult:
        phone = re.sub(r"\D", "", phone_number)
        if phone.startswith("0"): phone = "38" + phone
        async with AsyncClient(base_url=BASE_URL, headers=WEB_HEADERS, timeout=25) as c:
            form = await c.get("/uk/authentication/loadauthform")
            token = _csrf(form.text)
            auth_headers = {"RequestVerificationToken": token} if token else {}
            r = await c.post("/uk/authentication/getlogindata", headers=auth_headers, json={"phone": phone})
            if r.status_code == 404:
                # The site occasionally routes the legacy endpoint only under
                # the Russian locale, while the Ukrainian UI remains active.
                r = await c.post("/ru/authentication/getlogindata", headers=auth_headers, json={"phone": phone})
            if r.status_code >= 400:
                raise AdapterError(f"Фокстрот: не вдалося отримати параметри входу ({r.status_code})")
            r = await c.post("/uk/authentication/login", headers=auth_headers, json={"channel": 4, "phone": f"+{phone}"})
            if r.status_code == 404:
                r = await c.post("/ru/authentication/login", headers=auth_headers, json={"channel": 4, "phone": f"+{phone}"})
            data = r.json()
            if not data.get("requestId"): raise AdapterError("Фокстрот: не вдалося запросити код")
            cookies = {k: v for k, v in c.cookies.items()}
        return AuthResult(pending=True, extra={"phone_number": phone, "request_id": data["requestId"], "cookies": cookies, "csrf": token})

    async def confirm_login(self, extra: dict, code: str) -> AuthResult:
        # Foxtrot's current flow confirms the OTP in the messenger/app and then
        # exposes completion through this endpoint; code is retained for the UI
        # contract and future flow variants.
        phone, request_id = extra.get("phone_number"), extra.get("request_id")
        if not phone or not request_id: raise AdapterError("Фокстрот: сесія входу застаріла")
        async with AsyncClient(base_url=BASE_URL, headers=WEB_HEADERS, cookies=_cookies(extra), timeout=25) as c:
            r = await c.get("/uk/authentication/requestauthenticationstatus", params={"requestId": request_id, "phone": phone})
            data = r.json()
            if not data.get("result") or data.get("refresh", True):
                raise AdapterError("Фокстрот: код ще не підтверджено")
            cookies = {k: v for k, v in c.cookies.items()}
        return AuthResult(access_token="foxtrot-session", extra={"cookies": cookies, "phone_number": phone})

    async def fetch_receipts(self, access_token: str, extra: dict, since: dt.datetime | None = None) -> list[NormalizedReceipt]:
        async with AsyncClient(base_url=BASE_URL, headers=WEB_HEADERS, cookies=_cookies(extra), timeout=30) as c:
            orders = []
            seen_order_ids = set()
            for page_number in range(100):
                page = await c.get(
                    "/uk/account/getorders",
                    params={"withHeader": "True", "page": page_number},
                    headers={"X-Requested-With": "XMLHttpRequest"},
                )
                if page.status_code >= 400 or "profile__orders-table" not in page.text:
                    if page_number == 0:
                        raise AdapterError("Фокстрот: сесія недійсна або замовлення недоступні")
                    break
                page_orders = []
                for order_id, block in re.findall(r'data-order-id="(\d+)"(.*?)(?=data-order-id="|\Z)', page.text, re.S):
                    if order_id in seen_order_ids:
                        continue
                    seen_order_ids.add(order_id)
                    date_match = re.search(r'td-mh-14">([^<]+)', block)
                    total_match = re.search(r'orders-table__td bold-td.*?<p[^>]*>([^<]+)', block, re.S)
                    page_orders.append((
                        order_id,
                        date_match.group(1) if date_match else "",
                        total_match.group(1) if total_match else None,
                        _order_type(block),
                    ))
                if not page_orders:
                    break
                orders.extend(page_orders)
            out=[]
            for order_id, date_text, list_total, order_type in orders:
                detail_response = await c.get(
                    f"/uk/account/orderdetails/{order_id}",
                    params={"type": order_type, "page": 0, "isActualOrders": "False"},
                    headers={"X-Requested-With": "XMLHttpRequest"},
                )
                if detail_response.status_code >= 400:
                    continue
                detail = detail_response.text
                date_m=re.search(r'(\d{1,2})\s+([^<\d]+)\s+(\d{4})', date_text)
                purchased=dt.datetime.now(dt.timezone.utc)
                if date_m:
                    months={"січня":1,"лютого":2,"березня":3,"квітня":4,"травня":5,"червня":6,"липня":7,"серпня":8,"вересня":9,"жовтня":10,"листопада":11,"грудня":12}
                    purchased=store_local_to_utc(dt.datetime(int(date_m[3]), months.get(date_m[2].strip(),1), int(date_m[1])))
                # Re-read Foxtrot order details even for existing receipts:
                # the order list/detail contains authoritative final totals and
                # the product card is the only warranty source.
                items=[]
                for m in re.finditer(r'class="profile__orders-tovar__title"[^>]*>.*?<p[^>]*>(.*?)</p>.*?class="profile__orders-tovar__count".*?<p>(.*?)</p>.*?class="profile__orders-tovar__price".*?<p>(.*?)</p>', detail, re.S):
                    name=re.sub(r'<.*?>','',m[1]).strip(); qty=_money(m[2]) or 1
                    items.append(NormalizedItem(name=name, quantity=qty, total_price=_money(m[3]), unit_price=_money(m[3])/qty))
                check=re.search(r'href="(https://check\.checkbox\.ua/[^\"]+)"', detail)
                receipt_file = await _save_receipt(c, check.group(1), order_id) if check else None
                product_href = _product_href(detail)
                product_url = urljoin(BASE_URL, product_href) if product_href else None
                warranty_text = None
                product_image_url = None
                if product_url:
                    product_response = await c.get(product_url)
                    if product_response.status_code < 400:
                        warranty_text = _product_warranty(product_response.text)
                        product_image_url = _product_image(product_response.text)

                total_amount = _label_amount(detail, "Разом")
                if total_amount is None:
                    total_amount = _money(list_total)
                discount = _label_amount(detail, "Знижка")
                earned_bonuses = _label_amount(detail, "Нараховано бонусів")
                used_bonuses = _label_amount(detail, "Використано бонусів")
                warranty_expires_at = _warranty_expiry(purchased, warranty_text)
                if items and product_url:
                    # The first product link in Foxtrot's order detail belongs
                    # to the first line item; warranty text is read from that card.
                    items[0].product_url = product_url
                    items[0].product_image_url = product_image_url
                    items[0].has_warranty = bool(warranty_text)
                    items[0].warranty_text = warranty_text
                    items[0].warranty_expires_at = warranty_expires_at
                    items[0].warranty_checked_at = dt.datetime.now(dt.timezone.utc)
                out.append(NormalizedReceipt(
                    external_id=order_id,
                    purchased_at=purchased,
                    total_amount=total_amount,
                    shop_name="Фокстрот",
                    channel="online" if order_type == "WEB" else "offline",
                    items=items,
                    total_discount=discount,
                    bonuses=earned_bonuses,
                    bonuses_used=abs(used_bonuses) if used_bonuses is not None else None,
                    raw={
                        "html": detail,
                        "receipt_file": receipt_file,
                        "product_url": product_url,
                        "warranty_text": warranty_text,
                        "order_type": order_type,
                    },
                    receipt_url=check.group(1) if check else None,
                    has_warranty=bool(warranty_text),
                    warranty_expires_at=warranty_expires_at,
                ))
            return out

    async def get_balance(self, access_token: str, extra: dict) -> LoyaltyBalance:
        async with AsyncClient(base_url=BASE_URL, headers=WEB_HEADERS, cookies=_cookies(extra), timeout=20) as c:
            r=await c.get("/uk/home/getheadercashback", params={"isAuthenticated":"True"}, headers={"X-Requested-With":"XMLHttpRequest"})
        m=re.search(r'data-bonuses\s*=\s*["\']([\d.,\s\u00a0]+)',r.text, re.I)
        if not m:
            # The personal FoxClub page contains the same value on some site
            # versions where the compact header partial is empty.
            async with AsyncClient(base_url=BASE_URL, headers=WEB_HEADERS, cookies=_cookies(extra), timeout=20) as c:
                r=await c.get("/uk/account/foxclubpersonal")
            m=re.search(r'data-bonuses\s*=\s*["\']([\d.,\s\u00a0]+)',r.text, re.I)
        if not m: raise AdapterError(f"Фокстрот: баланс бонусів не знайдено ({r.status_code})")
        return LoyaltyBalance(total=_money(m.group(1)))

    async def get_warranty_document(self, access_token: str | None, extra: dict, external_id: str) -> bytes:
        """Return the product card HTML, which is Foxtrot's warranty source."""
        async with AsyncClient(base_url=BASE_URL, headers=WEB_HEADERS, cookies=_cookies(extra), timeout=25) as c:
            for order_type in ("WEB", "RETAIL"):
                detail = await c.get(
                    f"/uk/account/orderdetails/{external_id}",
                    params={"type": order_type, "page": 0, "isActualOrders": "False"},
                    headers={"X-Requested-With": "XMLHttpRequest"},
                )
                href = _product_href(detail.text) if detail.status_code < 400 else None
                if href:
                    product = await c.get(urljoin(BASE_URL, href))
                    if product.status_code < 400 and _product_warranty(product.text):
                        return product.content
        raise AdapterError("Фокстрот: гарантійна інформація для покупки не знайдена")

    async def search_catalog(self, access_token: str | None, extra: dict, query: str) -> list[CatalogProduct]:
        async with AsyncClient(timeout=20) as c:
            r=await c.get(f"{SEARCH_URL}/{quote(query)}/true/")
        if r.status_code != 200: raise AdapterError(f"Фокстрот: пошук повернув {r.status_code}")
        data=r.json(); out=[]
        for p in data.get("products") or []:
            image_url = p.get("photoUrl")
            if image_url:
                image_url = urljoin(BASE_URL, image_url)
            out.append(CatalogProduct(
                name=p.get("title", ""),
                price=_money(str(p.get("price", 0))),
                brand=p.get("brand"),
                product_url=urljoin(BASE_URL, p["url"]) if p.get("url") else None,
                image_url=image_url,
                in_stock=p.get("isAvailable"),
            ))
        return out
