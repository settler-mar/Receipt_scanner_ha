"""АТБ-Маркет (com.atb.atbmarket).

Реализация авторизации standard OIDC authorization_code + PKCE flow против mobapp.atbmarket.com
с прохождением SMS OTP шага.

Порядок запросов:
  1. POST /register/login              {"phoneNumber": "+380..."}   -> {"token": <otp_token>}
  2. GET  /auth/auth?client_id=third_mobile... -> 303 на /auth/third_mobile/interaction/<interaction_id>
  3. POST /auth/third_mobile/interaction/<interaction_id>/login (передача otp) -> {"returnTo": "<url>"}
  4. GET  <returnTo>                    -> 303, получение ?code=<auth_code>
  5. POST /auth/token                   -> {"access_token", "id_token", ...}

Технічна реальність щодо чеків (ereceipt.atbmarket.com/.../getReceipts):
цей ендпоінт стоїть за Cloudflare bot-management, який зупиняє запит ще на
рівні TLS-handshake (403 Forbidden), якщо клієнт не має TLS/JA3-відбитка
справжнього мобільного застосунку. Кроки 1–5 (авторизація) проходять успішно
через звичайний app.core.http.AsyncClient — блокується лише сам виклик
getReceipts. Підміна TLS-відбитка під браузер (curl_cffi impersonate,
Playwright request-context від імені Chromium тощо) навмисно не
використовується — це був би обхід антибот-захисту, а не легітимна
робота з API, і суперечить принципу проєкту (див. adapters/fozzy.py,
scripts/silpo_login.py).

Легітимний шлях отримання чеків АТБ — метод `parse_import` нижче: користувач
сам, у своєму браузері/застосунку, отримує відповідь getReceipts (так само,
як для Сільпо логін проходить у реальному браузері — scripts/silpo_login.py),
і бекенд лише парсить вже отриманий JSON, не видаючи себе за браузер.
`fetch_receipts` навмисно залишений як є і чесно падає на 403 — це не баг.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import re
import secrets
import urllib.parse

import httpx

from app.adapters.base import (
  AdapterError,
  AuthResult,
  BaseAdapter,
  CatalogProduct,
  LoyaltyBalance,
  LoyaltyCode,
  NormalizedItem,
  NormalizedReceipt,
)
from app.core.http import AsyncClient
from app.core.time import store_local_to_utc

BASE_URL = "https://ereceipt.atbmarket.com/EReceipt_MP/eReceipts/1.0"
AUTH_BASE = "https://mobapp.atbmarket.com"
# Separate white-label loyalty backend (shared across several UA retail chains,
# going by the domain) — not behind the same Cloudflare bot-management as
# mobapp/ereceipt, so unlike getReceipts this one isn't blocked from the server.
WALLET_URL = "https://wallet-prod.pc-service.ua"
# Product-catalog search for the "zakaz" (online order) storefront — a *third-party*
# search widget (api.multisearch.io), not an ATB-owned host, and needs no auth at
# all: just this static site id + public widget key, both captured live from the
# real app's own traffic (RESEARCH.md). Confirmed live with a plain, unmodified
# httpx request (see search_catalog below) — no TLS/JA3 impersonation needed or
# used here, unlike the getReceipts situation documented at the top of this file.
CATALOG_SEARCH_URL = "https://api.multisearch.io/"
CATALOG_SEARCH_KEY = "63a6d0a760fd2d0562c4061b78e64754"
CATALOG_SEARCH_SITE_ID = "11280"
# multisearch.io's own city/location id for this account's selected ATB store —
# not the same numbering as zakaz.atbmarket.com's cityId (395 there). Hardcoded
# from a live capture; if results ever look off for a different city, this is
# the value to re-capture.
CATALOG_SEARCH_LOCATION = "1075"
# Static per-app Basic auth credential the mobile client uses to call /register/login.
REGISTER_BASIC_AUTH = "Basic cmVnX3VzZXI6YmFzaWMqODhwYXNzd29yZCFwcm9kOTk="

_AUTH_HEADERS = {
  "Accept": "application/json, text/plain, */*",
  "Accept-Encoding": "gzip, deflate, br",
  "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}


def _pkce_pair() -> tuple[str, str]:
  verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
  challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
  return verifier, challenge


_FRACTION_RE = re.compile(r"\.(\d+)")


def _parse_dt(value: str) -> dt.datetime:
  """wallet-prod.pc-service.ua's expiredIn carries .NET-style fractional seconds
  with an arbitrary digit count (e.g. ".5944481", 7 digits) — same issue as
  Fozzy's timestamps (see fozzy.py), Python's fromisoformat only accepts 3 or 6."""
  value = _FRACTION_RE.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), value, count=1)
  return store_local_to_utc(dt.datetime.fromisoformat(value))


class AtbAdapter(BaseAdapter):
  store_code = "atb"

  async def start_login(self, phone_number: str) -> AuthResult:
    code_verifier, code_challenge = _pkce_pair()

    async with AsyncClient(timeout=20, headers=_AUTH_HEADERS) as client:
      resp = await client.post(
        f"{AUTH_BASE}/register/login",
        headers={"Authorization": REGISTER_BASIC_AUTH, "Content-Type": "application/json"},
        json={"phoneNumber": phone_number},
      )
      if resp.status_code != 201:
        raise AdapterError(f"АТБ: не вдалось надіслати SMS ({resp.status_code}): {resp.text[:200]}")
      otp_token = resp.json()["token"]

      auth_resp = await client.get(
        f"{AUTH_BASE}/auth/auth",
        params={
          "client_id": "third_mobile",
          "scope": "openid phone",
          "prompt": "login",
          "response_type": "code",
          "code_challenge_method": "S256",
          "code_challenge": code_challenge,
        },
        follow_redirects=False,
      )
      if auth_resp.status_code != 303 or "location" not in auth_resp.headers:
        raise AdapterError(f"АТБ: неочікувана відповідь /auth/auth ({auth_resp.status_code})")
      interaction_id = auth_resp.headers["location"].rsplit("/", 1)[-1]
      cookies = dict(client.cookies)

    return AuthResult(
      pending=True,
      extra={
        "otp_confirm_token": otp_token,
        "interaction_id": interaction_id,
        "code_verifier": code_verifier,
        "auth_cookies": cookies,
      },
    )

  async def confirm_login(self, extra: dict, code: str) -> AuthResult:
    interaction_id = extra.get("interaction_id")
    otp_token = extra.get("otp_confirm_token")
    code_verifier = extra.get("code_verifier")
    cookies = extra.get("auth_cookies") or {}
    if not (interaction_id and otp_token and code_verifier):
      raise AdapterError("АТБ: сесія логіну застаріла — почніть спочатку")

    async with AsyncClient(timeout=20, cookies=cookies, headers=_AUTH_HEADERS) as client:
      login_resp = await client.post(
        f"{AUTH_BASE}/auth/third_mobile/interaction/{interaction_id}/login",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"token": otp_token, "otp": code},
      )
      if login_resp.status_code != 200:
        raise AdapterError("АТБ: невірний код підтвердження")
      return_to = login_resp.json()["returnTo"]

      resume_resp = await client.get(return_to, follow_redirects=False)
      if resume_resp.status_code != 303 or "location" not in resume_resp.headers:
        raise AdapterError(f"АТБ: неочікувана відповідь при резюмуванні ({resume_resp.status_code})")
      query = urllib.parse.urlparse(resume_resp.headers["location"]).query
      auth_code = urllib.parse.parse_qs(query).get("code", [None])[0]
      if not auth_code:
        raise AdapterError("АТБ: не отримано код авторизації")

      token_resp = await client.post(
        f"{AUTH_BASE}/auth/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
          "code": auth_code,
          "code_verifier": code_verifier,
          "grant_type": "authorization_code",
          "client_id": "third_mobile",
        },
      )
      if token_resp.status_code != 200:
        raise AdapterError(f"АТБ: не вдалось обміняти код на токен ({token_resp.status_code})")
      payload = token_resp.json()

    expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=payload.get("expires_in", 3600))
    return AuthResult(access_token=payload["id_token"], expires_at=expires_at)

  async def fetch_receipts(
      self, access_token: str, extra: dict, since: dt.datetime | None = None
  ) -> list[NormalizedReceipt]:
    """Очікувано повертає 403 Forbidden — /getReceipts стоїть за Cloudflare
    bot-management, який ріже запит на рівні TLS-handshake для будь-якого
    клієнта без TLS-відбитка справжнього мобільного застосунку (див.
    docstring модуля). Це не помилка реалізації і не привід підміняти
    TLS-відбиток під браузер — легітимний шлях отримати чеки тут це
    `parse_import`, куди користувач сам приносить вже отриманий JSON
    зі свого браузера/застосунку."""
    phone = extra.get("phone_number")
    if not phone:
      raise ValueError("АТБ: в extra отсутствует phone_number (clientId)")

    headers = {**_AUTH_HEADERS, "Authorization": f"Bearer {access_token}"}
    receipts: list[NormalizedReceipt] = []
    offset = 1
    page_size = 20

    async with AsyncClient(timeout=20) as client:
      while True:
        resp = await client.post(
          f"{BASE_URL}/getReceipts",
          headers=headers,
          json={
            "clientId": phone,
            "checkSortBy": "date",
            "checkOffset": offset,
            "checkLimit": page_size,
          },
        )
        resp.raise_for_status()
        payload = resp.json()
        batch = payload.get("Data") or []
        if not batch:
          break

        stop = False
        for entry in batch:
          normalized = _normalize(entry)
          if since and normalized.purchased_at < since:
            stop = True
            break
          receipts.append(normalized)

        if stop or len(batch) < page_size:
          break
        offset += 1

    return receipts

  def parse_import(self, payload: dict) -> list[NormalizedReceipt]:
    """Парсинг структуры JSON данных, полученных из API getReceipts,
    и запуск стандартного процесса нормализации чеков."""
    entries = payload.get("Data") or []
    return [_normalize(e) for e in entries]

  async def get_loyalty_code(self, access_token: str, extra: dict) -> LoyaltyCode:
    """GET /api/v1/Client/pin on the separate wallet-prod.pc-service.ua host —
    confirmed live, not Cloudflare-gated like eReceipts. Returns a rotating
    code (~3h window observed) plus a short human-readable `pin` fallback; we
    only surface the code (as a QR, matching what the real app shows)."""
    async with AsyncClient(timeout=20) as client:
      resp = await client.get(
        f"{WALLET_URL}/api/v1/Client/pin",
        headers={**_AUTH_HEADERS, "Authorization": f"Bearer {access_token}"},
      )
    if resp.status_code != 200:
      raise AdapterError(f"АТБ: не вдалось отримати код картки ({resp.status_code})")
    result = resp.json().get("result") or {}
    barcode = result.get("barcode")
    if not barcode:
      raise AdapterError("АТБ: код картки відсутній у відповіді")
    expires_at = None
    if result.get("expiredIn"):
      expires_at = _parse_dt(result["expiredIn"])
    return LoyaltyCode(value=barcode, display="qr", expires_at=expires_at)

  async def get_balance(self, access_token: str, extra: dict) -> LoyaltyBalance:
    """Same wallet-prod.pc-service.ua host — this is a "round up the change"
    program (remainderParams describes the rounding rules), `balance` is what's
    accumulated so far."""
    async with AsyncClient(timeout=20) as client:
      resp = await client.get(
        f"{WALLET_URL}/api/v1/Client/balanceByClientId",
        headers={**_AUTH_HEADERS, "Authorization": f"Bearer {access_token}"},
      )
    if resp.status_code != 200:
      raise AdapterError(f"АТБ: не вдалось отримати баланс ({resp.status_code})")
    result = resp.json().get("result") or {}
    if "balance" not in result:
      raise AdapterError("АТБ: баланс відсутній у відповіді")
    return LoyaltyBalance(total=float(result["balance"]))

  async def search_catalog(self, access_token: str | None, extra: dict, query: str) -> list[CatalogProduct]:
    """No account/token needed at all — see CATALOG_SEARCH_* above. Plain httpx,
    deliberately not app.core.http.AsyncClient's curl_cffi client: this host
    doesn't need (and this code doesn't use) any TLS impersonation."""
    params = {
      "query": query,
      "q": query,
      "location": CATALOG_SEARCH_LOCATION,
      "p": "1",
      "id": CATALOG_SEARCH_SITE_ID,
      "lang": "uk",
      "key": CATALOG_SEARCH_KEY,
    }
    headers = {"accept": "application/json, text/plain, */*", "user-agent": "okhttp/4.9.2"}
    async with httpx.AsyncClient(timeout=15) as client:
      resp = await client.get(CATALOG_SEARCH_URL, params=params, headers=headers)
    if resp.status_code != 200:
      raise AdapterError(f"АТБ: пошук товарів повернув {resp.status_code}")
    data = resp.json()
    products: list[CatalogProduct] = []
    for group in ((data.get("results") or {}).get("item_groups") or []):
      for item in group.get("items") or []:
        try:
          price = float(item["price"])
        except (KeyError, TypeError, ValueError):
          continue
        products.append(
          CatalogProduct(
            name=item.get("name") or "",
            price=price,
            brand=item.get("brand") or None,
            image_url=item.get("picture") or None,
            product_url=item.get("url") or None,
            in_stock=item.get("is_presence"),
          )
        )
    return products


def _normalize(entry: dict) -> NormalizedReceipt:
  shop = entry.get("shop") or {}
  rcpt = entry.get("rcpt") or {}
  details = entry.get("details") or []

  purchased_at = store_local_to_utc(
    dt.datetime.strptime(rcpt["dt"], "%d.%m.%Y %H:%M:%S")
  )
  external_id = f"{rcpt.get('rroFiscal', shop.get('id'))}:{rcpt.get('rNum')}:{rcpt.get('zNum')}"

  items = [_normalize_item(d) for d in details]

  return NormalizedReceipt(
    external_id=external_id,
    purchased_at=purchased_at,
    total_amount=_to_float(rcpt.get("total")) or 0,
    shop_name=shop.get("name"),
    shop_address=shop.get("addr"),
    payment_type=None,
    items=items,
    raw=entry,
  )


def _to_float(value) -> float | None:
  if value in (None, ""):
    return None
  return float(str(value).replace(",", "."))


def _normalize_item(data: dict) -> NormalizedItem:
  """АТБ returns ``sum`` before the line discount, while ``rcpt.total`` is net.

  The normalized contract stores ``total_price`` net and keeps the discount in
  its own field. Keeping both on the same basis is required for receipt-level
  reconciliation and for the dashboard's gross/net toggle.
  """
  quantity = float(data.get("qty") or 1)
  gross_total = _to_float(data.get("sum")) or 0
  discount = _to_float(data.get("discount")) or 0
  gross_unit_price = _to_float(data.get("price"))
  unit_price = gross_unit_price
  if unit_price is not None and quantity > 0 and discount:
    unit_price = round(unit_price - discount / quantity, 2)
  return NormalizedItem(
    name=data.get("name", "").strip(),
    quantity=quantity,
    unit_price=unit_price,
    total_price=round(max(gross_total - discount, 0), 2),
    discount=discount,
    barcode=data.get("bar") or None,
  )
