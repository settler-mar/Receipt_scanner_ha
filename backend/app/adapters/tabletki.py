"""Tabletki.ua — агрегатор аптек: бронь товару в аптеці, викуп на місці.

Особистого JSON API в сайта немає: кабінет — це звичайний ASP.NET MVC, який
віддає HTML і кілька `/ajax/*` партіалів (розвідано по HAR живої сесії).
Тому адаптер працює як браузер:

  POST /cabinet/loginByEmail        {email, password, pageType}
      -> {"type":"success","view":"..."} + cookie сесії (зберігаємо весь jar,
         як у vencon.py — пароль не зберігається ніколи)
  GET  /uk/reserve/history/         історія броней, перші 5 карток
  POST /cabinet/completedHistory/   {quantity} — той самий блок, але довший
         (кнопка «показати ще»); пагінуємо, доки список росте.

Чек = викуплена бронь (`Викуплено`). Скасовані та ще не викуплені броні
повертаються з `excluded=True`, щоб колектор прибрав рядок, якщо статус
змінився після збереження.

Пошук по каталогу публічний (працює без логіну) — сторінка /uk/search/<запит>/
з картками товарів і мінімальною ціною «від N грн» по аптеках міста.

Бонусів/кешбеку тут немає взагалі: Tabletki.ua — агрегатор, програма лояльності
лишається за конкретною аптекою. У кабінеті, у cabinet.min.js і на головній
немає жодної згадки «бонус»/«кешбек», тож `get_balance` свідомо не реалізовано.

Cloudflare відсікає звичайний httpx, тому весь трафік іде через
`app.core.http.AsyncClient` (curl_cffi з TLS-імперсонацією Chrome).
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
from urllib.parse import quote, urljoin

from app.adapters.base import (
    AdapterError,
    AuthResult,
    BaseAdapter,
    CatalogProduct,
    NormalizedItem,
    NormalizedReceipt,
)
from app.core.http import AsyncClient, RequestException
from app.core.time import store_local_to_utc

BASE_URL = "https://tabletki.ua"
LOGIN_PAGE_URL = f"{BASE_URL}/uk/cabinet/login/"
LOGIN_URL = f"{BASE_URL}/cabinet/loginByEmail"
HISTORY_URL = f"{BASE_URL}/uk/reserve/history/"
HISTORY_MORE_URL = f"{BASE_URL}/cabinet/completedHistory/"

# Скільки карток догружаємо за один крок «показати ще» і скільки таких кроків
# максимум — 77 броней у тестовому акаунті розбираються за ~4 запити.
_PAGE_SIZE = 20
_MAX_PAGES = 30

# Маркер сторінки для неавторизованого відвідувача (кабінет віддається і без
# логіну, просто порожній) — окремого 401/302 сайт не дає.
_ANONYMOUS_MARKER = "Вхід / Реєстрація"

_STATUS_BOUGHT = "Викуплено"

_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9",
}
_AJAX_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": BASE_URL,
}


def _text(fragment: "str | re.Match | None") -> str:
    """Плоский текст фрагмента. Приймає і готовий рядок, і результат re.search —
    парсер майже завжди працює з match-ами, і розгортати їх на місці шумно."""
    if fragment is None:
        return ""
    if isinstance(fragment, re.Match):
        fragment = fragment.group(1) if fragment.re.groups else fragment.group(0)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).replace("\xa0", " ").split())


def _money(fragment: "str | re.Match | None") -> float:
    plain = _text(fragment).replace(" ", "").replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", plain)
    return float(match.group(0)) if match else 0.0


def _quantity(fragment: "str | re.Match | None") -> float:
    match = re.search(r"\d+(?:[,.]\d+)?", _text(fragment))
    return float(match.group(0).replace(",", ".")) if match else 1.0


def _parse_items(block: str) -> list[NormalizedItem]:
    items: list[NormalizedItem] = []
    # Розмітка вкладена нерівномірно, тому ріжемо по відкривальному тегу картки
    # товару: у «хвості» останнього шматка лишається підсумок «Разом», але він
    # має власний клас і під regex позиції не підпадає.
    for chunk in re.split(r'(?=<div class="reservation__quantity-info product-info-opener")', block)[1:]:
        name = _text(re.search(r"<h2[^>]*>(.*?)</h2>", chunk, flags=re.S))
        if not name:
            continue
        quantity = _quantity(re.search(r'<span class="[^"]*packages[^"]*">(.*?)</span>', chunk, flags=re.S))
        total_price = _money(re.search(r'<div class="amount[^"]*">(.*?)</div>', chunk, flags=re.S))
        if re.search(r'<div class="amount[^"]*text-crossed', chunk):
            # Ціна змінилась, поки бронь чекала: закреслена — стара, поруч
            # у `info-changed--warning` лежить фактична. Нуль там означає
            # «Немає в наявності» — таку позицію користувач не викупив.
            # The warning contains the replacement quantity in the first span
            # and the replacement price in the following strong span. Reading
            # the first span used to turn "0.5 упаковки" into a price of 0.5.
            actual = _money(re.search(
                r'reservation__info-changed--warning.*?<span[^>]*class="[^"]*strong[^"]*"[^>]*>(.*?)</span>',
                chunk, flags=re.S,
            ))
            if not actual:
                continue
            total_price = actual
        image = re.search(r'<img[^>]*src="([^"]+)"', chunk)
        image_url = image.group(1) if image else None
        items.append(NormalizedItem(
            name=name,
            quantity=quantity or 1,
            unit="уп",
            unit_price=round(total_price / quantity, 2) if quantity else total_price,
            total_price=total_price,
            merchant_name=_text(re.search(
                r'<div class="reservation__quantity-info--manufacturer">(.*?)</div>', chunk, flags=re.S,
            )) or None,
            product_image_url=image_url,
            product_image_source_url=image_url,
        ))
    return items


def _search_text(pattern: str, block: str) -> str:
    match = re.search(pattern, block, flags=re.S)
    return _text(match.group(1)) if match else ""


def _parse_reservations(html_text: str) -> list[NormalizedReceipt]:
    receipts: list[NormalizedReceipt] = []
    seen: set[str] = set()
    for block in re.split(r'(?=<div class="reservation-group__small-container)', html_text)[1:]:
        code = re.search(r'data-order-code="(\d+)"', block)
        moment = re.search(r'data-text">\s*(\d{2})\.(\d{2})\.(\d{4})\s*в\s*(\d{2}):(\d{2})', block)
        if not code or not moment:
            continue
        external_id = code.group(1)
        if external_id in seen:
            continue
        seen.add(external_id)
        day, month, year, hour, minute = (int(part) for part in moment.groups())
        purchased_at = store_local_to_utc(dt.datetime(year, month, day, hour, minute))

        status = _search_text(r'<span class="tag-text">(.*?)</span>', block) or _search_text(
            r'<span class="tag state-tag[^"]*">(.*?)</span>', block,
        )
        delivery = _search_text(r'reservation-group__reservation-type strong">(.*?)</span>', block)
        # Назву аптеки сайт сам обрізає до ~18 символів («Аптека оптових ц..»);
        # беремо як є — це те, що бачить користувач у кабінеті.
        shop_name = _search_text(
            r'<div class="reservation-group__address">\s*<div>\s*<span class="strong">(.*?)</span>', block,
        ) or _search_text(r'status-details__contact-pharmacy">\s*<span class="strong">(.*?)</span>', block)
        shop_address = _search_text(r'<div class="status-details__address">(.*?)</div>', block)
        pharmacy_id = re.search(r'pharmacy-info" data-id="(\d+)"', block)

        items = _parse_items(block)
        total = _money(re.search(r'price-text strong ml-1">(.*?)</div>', block, flags=re.S))
        bought = status.strip() == _STATUS_BOUGHT
        receipts.append(NormalizedReceipt(
            external_id=external_id,
            purchased_at=purchased_at,
            total_amount=total or sum(item.total_price for item in items),
            shop_name=shop_name or "Tabletki.ua",
            shop_address=shop_address or None,
            channel="online" if delivery and "самовивіз" not in delivery.lower() else "offline",
            items=items,
            excluded=not bought,
            exclusion_reason=None if bought else f"Статус броні: {status or 'невідомий'}",
            order_status=status or None,
            # Keep the individual cabinet card: old records used to retain only
            # its status, which made later reconciliation impossible.
            raw={"status": status, "source": "pharmacy_preorder", "reservation_type": delivery,
                 "pharmacy_id": pharmacy_id.group(1) if pharmacy_id else None, "source_card_html": block},
        ))
    return receipts


def _parse_search(html_text: str) -> list[CatalogProduct]:
    products: list[CatalogProduct] = []
    for card in re.split(r'(?=<div class="card__category--info">)', html_text)[1:]:
        link = re.search(r'<a[^>]*title="([^"]+)"[^>]*href="([^"]+)"', card)
        price = re.search(r'<div class="card__category--price">(.*?)</div>', card, flags=re.S)
        if not link or not price:
            continue
        found = _search_text(r'card__category--info-where-found[^"]*">(.*?)</div>', card)
        products.append(CatalogProduct(
            name=html.unescape(link.group(1)),
            price=_money(price.group(1)),
            unit="уп",
            brand=_search_text(r'<div class="card__category--info-additional">\s*<div>(.*?)</div>', card) or None,
            product_url=urljoin(BASE_URL, html.unescape(link.group(2)).split("?")[0]),
            in_stock="аптек" in found.lower(),
        ))
    return products[:30]


def _cookies_from_token(access_token: str) -> dict[str, str]:
    try:
        cookies = json.loads(access_token)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AdapterError("Tabletki.ua: пошкоджена сесія, увійдіть повторно") from exc
    if not isinstance(cookies, dict) or not cookies:
        raise AdapterError("Tabletki.ua: порожня сесія, увійдіть повторно")
    return {str(key): str(value) for key, value in cookies.items()}


def _is_authenticated(html_text: str) -> bool:
    return _ANONYMOUS_MARKER not in html_text


class TabletkiAdapter(BaseAdapter):
    store_code = "tabletki"
    catalog_search_requires_auth = False
    login_field = "email"

    async def login_with_password(self, phone_number: str, password: str) -> AuthResult:
        """`phone_number` тут — email кабінету: сайт логінить тільки за email."""
        email = phone_number.strip()
        if "@" not in email:
            raise AdapterError("Tabletki.ua: вкажіть email, на який зареєстровано кабінет")
        try:
            async with AsyncClient(timeout=25, headers=_HEADERS) as client:
                await client.warmup(f"{BASE_URL}/uk/", follow_redirects=True)
                await client.get(LOGIN_PAGE_URL, follow_redirects=True)
                response = await client.post(
                    LOGIN_URL,
                    data={"email": email, "password": password, "pageType": ""},
                    headers={**_AJAX_HEADERS, "Referer": LOGIN_PAGE_URL},
                    follow_redirects=True,
                )
                if response.status_code >= 400:
                    raise AdapterError(f"Tabletki.ua: сервер відхилив вхід ({response.status_code})")
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                if payload.get("errorMessage"):
                    raise AdapterError(f"Tabletki.ua: {payload['errorMessage']}")
                if payload.get("type") != "success":
                    raise AdapterError("Tabletki.ua: невірний email або пароль")
                history = await client.get(HISTORY_URL, follow_redirects=True)
                if not _is_authenticated(history.text):
                    raise AdapterError("Tabletki.ua: вхід не підтвердився, спробуйте ще раз")
                session = json.dumps(client.cookies, ensure_ascii=False, separators=(",", ":"))
        except RequestException as exc:
            raise AdapterError("Tabletki.ua: не вдалося з'єднатися із сайтом") from exc
        return AuthResult(access_token=session, extra={"session_kind": "cookies"})

    async def fetch_receipts(
        self, access_token: str, extra: dict, since: dt.datetime | None = None
    ) -> list[NormalizedReceipt]:
        cookies = _cookies_from_token(access_token)
        since_utc = since.replace(tzinfo=dt.timezone.utc) if since and since.tzinfo is None else since
        try:
            async with AsyncClient(timeout=30, headers=_HEADERS, cookies=cookies) as client:
                response = await client.get(HISTORY_URL, follow_redirects=True)
                if response.status_code >= 400:
                    raise AdapterError(f"Tabletki.ua: історія броней недоступна ({response.status_code})")
                if not _is_authenticated(response.text):
                    raise AdapterError("Tabletki.ua: сесія завершилася, увійдіть повторно")
                page = response.text
                receipts = _parse_reservations(page)
                total = re.search(r'id="hdnOrderTotalQuantity" value="(\d+)"', page)
                expected = int(total.group(1)) if total else len(receipts)
                for _ in range(_MAX_PAGES):
                    if len(receipts) >= expected or _reached(receipts, since_utc):
                        break
                    more = await client.post(
                        HISTORY_MORE_URL,
                        data={"quantity": len(receipts) + _PAGE_SIZE},
                        headers={**_AJAX_HEADERS, "Referer": HISTORY_URL},
                        follow_redirects=True,
                    )
                    if more.status_code >= 400:
                        break
                    batch = _parse_reservations(more.text)
                    if len(batch) <= len(receipts):
                        break
                    receipts = batch
        except RequestException as exc:
            raise AdapterError("Tabletki.ua: не вдалося отримати історію броней") from exc
        return [
            receipt for receipt in receipts
            if receipt.excluded or since_utc is None or receipt.purchased_at >= since_utc
        ]

    async def search_catalog(self, access_token: str | None, extra: dict, query: str) -> list[CatalogProduct]:
        text = query.strip()
        if not text:
            return []
        try:
            async with AsyncClient(timeout=25, headers=_HEADERS) as client:
                response = await client.get(
                    f"{BASE_URL}/uk/search/{quote(text)}/", follow_redirects=True,
                )
        except RequestException as exc:
            raise AdapterError("Tabletki.ua: пошук по каталогу недоступний") from exc
        if response.status_code >= 400:
            raise AdapterError(f"Tabletki.ua: пошук повернув {response.status_code}")
        return _parse_search(response.text)


def _reached(receipts: list[NormalizedReceipt], since: dt.datetime | None) -> bool:
    """Чи дійшли вже до броней, старших за `since` — далі гортати немає сенсу."""
    if since is None or not receipts:
        return False
    return min(receipt.purchased_at for receipt in receipts) < since
