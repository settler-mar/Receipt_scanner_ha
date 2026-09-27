"""Liki.ua integration: password login, booked orders, Fishka accruals, catalog search.

Liki.ua is a booking service, not a pharmacy chain of its own: the user reserves
products on the site and collects them in one of the partner pharmacies, so an
order carries the pharmacy's own name/address while the account lives on
liki.ua. Everything is driven by the site's own AJAX cabinet — there is no
public mobile API (api.liki.ua answers "Bad credentials" to everything and the
LIKI app is the same cabinet behind a webview):

- POST /account/login   form-urlencoded `fast=1&login=…&password=…` ->
  {"result": "success", "data": {...}} plus a PHPSESSID cookie; the whole cookie
  jar is what we persist as the token. The body MUST be form-encoded and carry
  `X-Requested-With: XMLHttpRequest` — the site's own axios is configured that
  way (`axios.defaults.headers.post`), and a JSON body is silently read as empty
  credentials, i.e. answered with "Введені логін або пароль не знайдені".
- GET  /shop/orders     server-rendered order cards (.lk-order-block).
- POST /loyal/fishka    {"result": "success", "data": {"txs": [...], "txsPages": {...}}}
  the Fishka cashback ledger: one row per accrual, linked to an order by
  orderRID, plus the key for that order's PDF receipt.

Bonuses are *not* a liki.ua wallet: cashback is credited straight to the user's
Fishka card (myfishka.com) bound to the same phone number, so this adapter
reports per-receipt accruals but deliberately implements no get_balance /
get_loyalty_code — that balance belongs to Fishka, not to this account.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from urllib.parse import urljoin

from app.adapters.base import (
    AdapterError,
    AuthResult,
    BaseAdapter,
    CatalogProduct,
    NormalizedItem,
    NormalizedReceipt,
)
from app.adapters.html_tree import Node, number_in, text_of, tree
from app.core.http import AsyncClient, RequestException
from app.core.time import store_local_to_utc


BASE_URL = "https://liki.ua"
LOGIN_URL = f"{BASE_URL}/account/login"
ORDERS_URL = f"{BASE_URL}/shop/orders"
# The ledger lives under the cabinet's loyalty path, not at the site root.
FISHKA_URL = f"{BASE_URL}/loyal/fishka"
SEARCH_URL = f"{BASE_URL}/shop/search"

# The site's own AJAX contract: {"result": "success"|"error", "errmsg", "errcode"}.
# 206/207 are what its login component treats as "go to the full login page" —
# in practice the captcha that appears after repeated failures, which we must
# not try to solve.
CAPTCHA_ERRCODES = {206, 207}
# Fishka ledger pages are ~20 rows; a collection run never needs the whole history.
MAX_FISHKA_PAGES = 10
# Statuses that mean the booking never turned into a purchase.
CANCELLED_WORDS = ("скасован", "відхилен", "анульован", "не викуплен", "прострочен")

# Without X-Requested-With the same routes answer with a full HTML page instead
# of the JSON envelope; the form content type is what the site's axios sends.
_AJAX_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "uk-UA,uk;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/",
}
_HTML_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9",
}


def _money(fragment: str | None) -> float:
    return number_in(fragment) or 0.0


def _normalize_login(value: str) -> str:
    """The cabinet accepts either an e-mail or a 12-digit phone (380XXXXXXXXX);
    users type the phone in every possible shape, so only that branch is fixed up."""
    login = value.strip()
    if "@" in login:
        return login
    digits = re.sub(r"\D", "", login)
    if len(digits) == 10 and digits.startswith("0"):
        digits = "38" + digits
    if len(digits) == 9:
        digits = "380" + digits
    if len(digits) != 12 or not digits.startswith("380"):
        raise AdapterError(
            f"Liki.ua: номер має містити 12 цифр (+380XXXXXXXXX), а в акаунті їх {len(digits)} — "
            "виправте номер кнопкою «Редагувати» або вкажіть там e-mail від кабінету"
        )
    return digits


def _cookies_from_token(access_token: str) -> dict[str, str]:
    try:
        cookies = json.loads(access_token)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AdapterError("Liki.ua: пошкоджена сесія, увійдіть повторно") from exc
    if not isinstance(cookies, dict) or not cookies:
        raise AdapterError("Liki.ua: порожня сесія, увійдіть повторно")
    return {str(key): str(value) for key, value in cookies.items()}


def _ajax_payload(response) -> dict:
    """Unwrap the site's {"result": ...} envelope or raise a readable error."""
    try:
        body = response.json()
    except Exception as exc:  # curl_cffi raises its own json errors
        raise AdapterError("Liki.ua: сайт повернув не JSON — можливо, сесія завершилася") from exc
    if not isinstance(body, dict):
        raise AdapterError("Liki.ua: неочікувана відповідь сайту")
    if body.get("result") == "success":
        data = body.get("data")
        return data if isinstance(data, dict) else {}
    errcode = body.get("errcode")
    message = str(body.get("errmsg") or "").strip()
    if errcode in CAPTCHA_ERRCODES:
        raise AdapterError("Liki.ua: сайт вимагає захисний код — увійдіть вручну на liki.ua і повторіть")
    raise AdapterError(f"Liki.ua: {message or 'сайт відхилив запит'}")


def _is_authenticated(html_text: str) -> bool:
    """The cabinet renders a "Вхід / Реєстрація" link in its sidebar for guests
    and the user's own menu entries once logged in."""
    return "/account/login" not in html_text or "/account/logout" in html_text


# Order cards print the date in words ("21 вересня 2026") — no numeric form and
# no time of day anywhere in the cabinet.
MONTHS = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6,
    "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}


def _order_date(card_text: str) -> dt.datetime | None:
    spelled = re.search(r"(\d{1,2})\s+([а-яіїєґ]+)\s+(\d{4})", card_text, flags=re.I)
    if spelled and spelled.group(2).lower() in MONTHS:
        day, month, year = int(spelled.group(1)), MONTHS[spelled.group(2).lower()], int(spelled.group(3))
        return store_local_to_utc(dt.datetime(year, month, day, 12, 0))
    numeric = re.search(r"(\d{2})\.(\d{2})\.(\d{4})(?:[^\d]{0,4}(\d{2}):(\d{2}))?", card_text)
    if not numeric:
        return None
    day, month, year = int(numeric.group(1)), int(numeric.group(2)), int(numeric.group(3))
    return store_local_to_utc(dt.datetime(year, month, day, int(numeric.group(4) or 12), int(numeric.group(5) or 0)))


def _image_url(node: Node) -> str | None:
    image = next((child for child in node.descendants() if child.tag == "img"), None)
    if not image:
        return None
    path = image.attrs.get("src") or image.attrs.get("data-src") or ""
    if not path and image.attrs.get("data-srcset"):
        path = image.attrs["data-srcset"].split()[0]
    return urljoin(BASE_URL, path) if path else None


def _parse_items(card: Node) -> list[NormalizedItem]:
    items: list[NormalizedItem] = []
    for block in card.find_all_class("lk-one-order__block"):
        title = block.find_class("lk-one-order__block-title")
        if not title:
            continue
        price = block.find_class("lk-one-order__block-price")
        line_total = _money(price.text() if price else None)
        # "упаковка: 1" sits in the same row as the price, so the price node is
        # cut out before reading the quantity out of what's left.
        count = block.find_class("lk-one-order__block-count")
        count_text = count.text().replace(price.text(), "") if count and price else (count.text() if count else "")
        quantity = number_in(count_text)
        quantity = quantity if quantity and quantity > 0 else 1
        link = next((node for node in title.descendants() if node.tag == "a"), title)
        href = link.attrs.get("href")
        maker = block.find_class("lk-one-order__block-desc")
        image = _image_url(block)
        items.append(NormalizedItem(
            name=title.text(),
            quantity=quantity,
            unit="шт",
            unit_price=round(line_total / quantity, 2) if quantity else line_total,
            total_price=line_total,
            merchant_name=maker.text() if maker else None,
            product_url=urljoin(BASE_URL, href) if href else None,
            product_image_url=image,
            product_image_source_url=image,
        ))
    return items


def _parse_orders(html_text: str) -> list[NormalizedReceipt]:
    receipts: list[NormalizedReceipt] = []
    for card in tree(html_text).find_all_class("lk-order-block"):
        card_text = card.text()
        number_node = card.find_class("lk-order-block__head-number")
        number = re.search(r"\d{4,}", number_node.text() if number_node else card_text)
        purchased_at = _order_date(card_text)
        if not number or not purchased_at:
            continue

        status_node = card.find_class("lk-order-block__label")
        status = status_node.text() if status_node else ""
        cancelled = any(word in status.lower() for word in CANCELLED_WORDS)

        # The pharmacy block inside the expanded order carries the brand and the
        # street address. `.lk-order-block__address-content` is deliberately NOT
        # used: in the live cabinet that slot holds the customer's own phone.
        pharmacy = card.find_class("drugstore-card__pharmacy-title")
        address = card.find_class("drugstore-card__pharmacy-location")
        items = _parse_items(card)

        total_node = card.find_class("lk-one-order__total")
        total_amount = _money(total_node.text()) if total_node else 0.0
        if not total_amount:
            # Collapsed cards only show "N товарів | 424.50 ₴" in the header.
            header = card.find_class("lk-order-block__head-count")
            total_amount = _money(header.text().split("|")[-1]) if header else 0.0
        if not total_amount:
            total_amount = round(sum(item.total_price for item in items), 2)

        # Every completed order links its own printable PDF.
        pdf = next(
            (node for node in card.descendants()
             if node.tag == "a" and "orderspdf" in node.attrs.get("href", "")),
            None,
        )

        receipts.append(NormalizedReceipt(
            external_id=number.group(0),
            purchased_at=purchased_at,
            total_amount=total_amount,
            shop_name=pharmacy.text() if pharmacy else "Liki.ua",
            shop_address=address.text() if address else None,
            receipt_url=urljoin(BASE_URL, pdf.attrs["href"]) if pdf else None,
            # Booked on the site, handed over in the partner pharmacy — the
            # order itself is an online one, same call as fora.py's delivery branch.
            channel="online",
            items=items,
            order_status=status or None,
            excluded=cancelled,
            exclusion_reason=f"Статус замовлення: {status}" if cancelled else None,
            # This is a pharmacy reservation/order, not the fiscal checkout
            # receipt issued when the order is collected in-store.
            raw={"status": status, "source": "pharmacy_preorder", "reservation_type": "pharmacy_pickup",
                 "source_card_text": card.text(),
                 "source_items": [{"name": item.name, "quantity": item.quantity,
                                   "unit_price": item.unit_price, "total_price": item.total_price}
                                  for item in items]},
        ))
    return receipts


def _parse_fishka(data: dict) -> dict[str, dict]:
    """Cashback ledger rows keyed by the order they were accrued for.

    Rows without an orderRID are manual/partner operations on the Fishka card
    (txComment says what they were) and have no purchase on liki.ua to attach to.
    """
    accruals: dict[str, dict] = {}
    for tx in data.get("txs") or []:
        if not isinstance(tx, dict) or not tx.get("orderRID"):
            continue
        order_id = str(tx["orderRID"])
        amount = number_in(str(tx.get("txAmount", ""))) or 0.0
        receipt_url = None
        if tx.get("orderPdfKey"):
            receipt_url = f"{BASE_URL}/orderspdf/{order_id}-{tx['orderPdfKey']}.pdf"
        accruals[order_id] = {
            "bonuses": amount,
            "receipt_url": receipt_url,
            "status": tx.get("statusName") or tx.get("txStatus"),
        }
    return accruals


def _fishka_pages(data: dict) -> int:
    pages = data.get("txsPages")
    total = number_in(str(pages.get("npages", ""))) if isinstance(pages, dict) else None
    return int(total) if total else 1


def _parse_search(html_text: str) -> list[CatalogProduct]:
    products: list[CatalogProduct] = []
    for card in tree(html_text).find_all_class("catalog-block"):
        title = card.find_class("catalog-block__title")
        link = next((node for node in title.descendants() if node.tag == "a"), None) if title else None
        price = card.find_class("catalog-block__price")
        if not link or not price:
            continue
        subtitle = card.find_class("catalog-block__subtitle")
        # Cards show "від 32.00 ₴" — the cheapest offer across the pharmacies
        # that have the product; availability is a "в N аптеках" line.
        availability = " ".join(node.text() for node in card.find_all_class("catalog-block__reality"))
        in_pharmacies = re.search(r"в\s+(\d+)\s+аптек", availability, flags=re.I)
        image = _image_url(card)
        products.append(CatalogProduct(
            name=link.text(),
            price=_money(price.text()),
            brand=subtitle.text().split("(")[0].strip() if subtitle else None,
            image_url=image,
            product_url=urljoin(BASE_URL, link.attrs.get("href", "")),
            in_stock=bool(in_pharmacies and int(in_pharmacies.group(1)) > 0) if in_pharmacies else None,
        ))
    return products[:30]


class LikiAdapter(BaseAdapter):
    store_code = "liki"
    catalog_search_requires_auth = False
    login_field = "phone_or_email"

    async def login_with_password(self, phone_number: str, password: str) -> AuthResult:
        login = _normalize_login(phone_number)
        try:
            async with AsyncClient(timeout=25, headers=_HTML_HEADERS) as client:
                # The session cookie is issued on the first page view; the AJAX
                # login then binds the account to it.
                await client.get(LOGIN_URL, follow_redirects=True)
                response = await client.post(
                    LOGIN_URL,
                    # "fast" is the hidden field the login form always submits.
                    data={"fast": "1", "login": login, "password": password},
                    headers={**_AJAX_HEADERS, "Referer": LOGIN_URL},
                )
                if response.status_code >= 500:
                    raise AdapterError(f"Liki.ua: сайт відповів {response.status_code}")
                _ajax_payload(response)
                orders = await client.get(ORDERS_URL, headers=_HTML_HEADERS, follow_redirects=True)
                if orders.status_code != 200 or not _is_authenticated(orders.text):
                    raise AdapterError("Liki.ua: вхід не підтверджено, перевірте логін і пароль")
                session = json.dumps(client.cookies, ensure_ascii=False, separators=(",", ":"))
        except RequestException as exc:
            raise AdapterError("Liki.ua: не вдалося з'єднатися із сайтом") from exc
        return AuthResult(access_token=session, extra={"session_kind": "cookies"})

    async def fetch_receipts(
        self, access_token: str, extra: dict, since: dt.datetime | None = None
    ) -> list[NormalizedReceipt]:
        cookies = _cookies_from_token(access_token)
        try:
            async with AsyncClient(timeout=30, cookies=cookies, headers=_HTML_HEADERS) as client:
                response = await client.get(ORDERS_URL, follow_redirects=True)
                if response.status_code != 200:
                    raise AdapterError(f"Liki.ua: список замовлень повернув {response.status_code}")
                if not _is_authenticated(response.text):
                    raise AdapterError("Liki.ua: сесія завершилася, увійдіть повторно")
                receipts = _parse_orders(response.text)
                accruals = await self._fetch_accruals(client)
        except RequestException as exc:
            raise AdapterError("Liki.ua: не вдалося завантажити замовлення") from exc

        for receipt in receipts:
            accrual = accruals.get(receipt.external_id)
            if not accrual:
                continue
            receipt.bonuses = accrual["bonuses"]
            receipt.receipt_url = receipt.receipt_url or accrual["receipt_url"]

        if since is None:
            return receipts
        since_utc = since if since.tzinfo else since.replace(tzinfo=dt.timezone.utc)
        return [r for r in receipts if r.excluded or r.purchased_at >= since_utc]

    async def _fetch_accruals(self, client: AsyncClient) -> dict[str, dict]:
        """Fishka cashback ledger. Best-effort: a purchase is still a purchase
        when the bonus side of the site is down, so this never fails a run."""
        accruals: dict[str, dict] = {}
        page = 1
        while page <= MAX_FISHKA_PAGES:
            url = FISHKA_URL if page == 1 else f"{FISHKA_URL}/page={page}"
            try:
                response = await client.post(url, headers=_AJAX_HEADERS)
                data = _ajax_payload(response)
            except (AdapterError, RequestException):
                break
            accruals.update(_parse_fishka(data))
            if page >= _fishka_pages(data):
                break
            page += 1
        return accruals

    async def search_catalog(self, access_token: str | None, extra: dict, query: str) -> list[CatalogProduct]:
        query = query.strip()
        if not query:
            return []
        try:
            async with AsyncClient(timeout=20, headers=_HTML_HEADERS) as client:
                response = await client.get(SEARCH_URL, params={"query": query, "filters": 1})
        except RequestException as exc:
            raise AdapterError("Liki.ua: пошук тимчасово недоступний") from exc
        if response.status_code != 200:
            raise AdapterError(f"Liki.ua: пошук повернув {response.status_code}")
        return _parse_search(response.text)
