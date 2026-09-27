"""Епіцентр К (ua.epicentrk).

Auth: phone + SMS OTP, but the token has to be bootstrapped from an *anonymous*
session first — this is a genuine 5-step dance, reverse-engineered end to end:

  1. POST /api/v2/auth/anonymous-token   {"appId": <client uuid4>}
     -> {"accessToken": <anon JWT, sub=00000000-0000-...>, "refreshToken", "expiresIn"}
  2. GET  /api/v2/auth/info/site-user    (Authorization: Bearer <anon token>, no query)
     -> {"userId": null, "fUserId": <int>, "sessionId": <string>}
     This is what allocates `sessionId` — every other call (including the OTP ones)
     sends it back as the `session_id` query param, and also sends `fUserId` as an
     `X-Fuser-Id` header.
  3. POST /api/v2/auth/otp-codes/generate?session_id=<sessionId>
          (Bearer <anon token>, X-Fuser-Id: <anon fUserId>)   {"phoneNumber": "380..."}
     -> {"id", "phoneNumber", "dateOfExpired", "hash"}    (sends the actual SMS)
  4. POST /api/v2/auth/otp-codes/process?session_id=<sessionId>
          (same headers)   {"phoneNumber": "380...", "code": "123456"}
     -> {"accessToken": <real JWT, site_user_id/phone_number claims>, "refreshToken",
         "expiresIn": 2592000}
  5. GET  /api/v2/auth/info/site-user?session_id=<sessionId>  (Bearer <REAL token>,
          X-Fuser-Id: <anon fUserId> — anything works here, this call is what changes it)
     -> {"userId": <site user id>, "fUserId": <different int!>, "sessionId": <same>}
     `sessionId` survives the anonymous->real-user upgrade, but `fUserId` does NOT —
     it's actually the account's own customer_id, only assigned once the token being
     used is a real (not anonymous) one. Skipping this step and reusing the anonymous
     fUserId for later calls gets a 500 from the orders endpoints (confirmed live) —
     this isn't an optional optimization, the real fUserId has to be fetched fresh
     after OTP succeeds.

No captcha anywhere in this flow (unlike Fozzy/ATB), so fully automatable. Checked
the public Swagger docs (mobile-api.epicentrk.ua/swagger/docs/v2/authenticationV2)
for a token-refresh endpoint — there isn't one; `expiresIn` is 30 days same as
Silpo/Fozzy, but when it lapses the only way back is redoing this whole phone+OTP
dance (`refresh()` below is deliberately unsupported, same as atb.py's default).

Two receipt sources, mirroring Fora's shape (list call has totals only, detail call
has line items) — confirmed against the live Swagger schema
(mobile-api.epicentrk.ua/swagger/docs/v2/userOrdersV2):
  GET /api/v2/sales/orders/offline?Page=&Limit=&From=&To=&session_id=
    + GET /api/v2/sales/orders/offline/{id}?session_id=          in-store
  GET /api/v2/sales/orders/online?Page=&Limit=&Status=All&From=&To=&session_id=
    + GET /api/v2/sales/orders/online/{id}?session_id=           delivery/pickup

Detail response gives header-level price/oldPrice and per-item price/oldLinesTotal —
discount is the price/oldPrice gap when Epicentr actually discounted something
(not observed live in a real receipt during research, but it's the same oldPrice
pattern Fora uses, and the field is right there in the schema).
"""
from __future__ import annotations

import datetime as dt
from app.core.time import store_local_to_utc
import asyncio
import calendar
import html
import logging
import re
import uuid
from difflib import SequenceMatcher

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

logger = logging.getLogger(__name__)

BASE_URL = "https://mobile-api.epicentrk.ua"
# Product-catalog search lives on a *different* host — the general/web-shop API,
# not the mobile-app-only one above. It does NOT use the account's own OTP-issued
# JWT: confirmed live across a full logout/re-login cycle that the Bearer token
# this host actually wants is a fixed, app-wide value (401s with the account's
# own JWT: "Bearer Token is not valid") — same role as ATB's multisearch.io key
# or Fora's user-info blob, just also called "Bearer" confusingly. X-Fuser-Id
# (the account's own customer id) and session_id are still per-account/session,
# same as everywhere else in this adapter.
CATALOG_BASE_URL = "https://api.epicentrk.ua"
CATALOG_APP_TOKEN = "m3NQfLUPcPjF2z5izd6B4ZHBfo5pP7XQbpLIdudx"
PRODUCT_PAGE_BASE = "https://epicentrk.ua"
# This account's currently selected physical store/market id (set via
# PATCH /api/user/profiles/current in the real app) — search results/prices/
# availability are scoped to it. Captured live; re-capture if it ever needs to
# reflect a different store.
CATALOG_STORE_ID = "2"
WARRANTY_MIN_PRICE = 1000.0

# Static per-app credential, same role as ATB's REGISTER_BASIC_AUTH / Fora's user-info
# blob — the gateway just checks it's present and valid for *some* app build, not that
# it matches a specific device.
_API_KEY = "p0YLLPSqODXA1fUS90aAaK7bdWETHtRp"
_MOBILE_VERSION = "4.11.2762"
_USER_AGENT = "Dalvik/2.1.0 (Linux; U; Android 13; 2207117BPG Build/TP1A.220624.014)"


def _base_headers() -> dict:
    return {
        "Content-Type": "application/json; charset=utf-8",
        "Platform": "android",
        "X-Epicentr-Lang": "uk",
        "SessionId": "5040",  # app-instance tracking header, not the auth session_id below
        "X-API-KEY": _API_KEY,
        "Mobile-Version": _MOBILE_VERSION,
        "User-Agent": _USER_AGENT,
    }


def _plain_product_text(markup: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", markup))).strip()


def _parse_product_warranty(markup: str) -> tuple[str, int] | None:
    text = _plain_product_text(markup)
    match = re.search(
        r"\bГарантія\s*:\s*(\d+)\s*(міс\.?|місяц(?:ь|і|ів)?|рік|роки|років)",
        text,
        re.I,
    )
    if not match:
        return None
    duration = int(match.group(1))
    months = duration * 12 if match.group(2).lower().startswith(("рік", "рок")) else duration
    return match.group(0), months


def _add_months(moment: dt.datetime, months: int) -> dt.datetime:
    month_index = moment.month - 1 + months
    year = moment.year + month_index // 12
    month = month_index % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def _normalized_product_name(value: str) -> str:
    return " ".join(re.findall(r"[a-zа-яіїєґ0-9]+", value.lower()))


def _normalized_code(value: str | None) -> str:
    return (value or "").strip().lstrip("0")


class EpicentrWarrantyScanner:
    """Find Epicentr product cards and enrich purchased items with warranty data."""

    def __init__(self, access_token: str, extra: dict):
        self.access_token = access_token
        self.extra = extra
        self.client: httpx.AsyncClient | None = None
        self.session_id = str(extra.get("session_id") or "")
        self.f_user_id = str(extra.get("f_user_id") or "")
        self.cache: dict[str, dict | None] = {}

    async def __aenter__(self):
        self.client = httpx.AsyncClient(timeout=30, follow_redirects=True)
        headers = {
            **_base_headers(),
            "Authorization": f"Bearer {self.access_token}",
            "X-Fuser-Id": self.f_user_id,
        }
        refreshed = await self.client.get(
            f"{BASE_URL}/api/v2/auth/info/site-user",
            headers=headers,
            params={"session_id": self.session_id},
        )
        if refreshed.status_code == 200:
            info = refreshed.json()
            self.session_id = str(info.get("sessionId") or self.session_id)
            self.f_user_id = str(info.get("fUserId") or self.f_user_id)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.client is not None:
            await self.client.aclose()

    async def enrich_receipts(self, receipts: list[NormalizedReceipt]) -> None:
        semaphore = asyncio.Semaphore(5)

        async def enrich(receipt: NormalizedReceipt, item: NormalizedItem) -> None:
            async with semaphore:
                await self.enrich_item(item, receipt.purchased_at)

        await asyncio.gather(*(
            enrich(receipt, item)
            for receipt in receipts if not receipt.excluded
            for item in receipt.items
        ))

    async def enrich_item(self, item: NormalizedItem, purchased_at: dt.datetime) -> None:
        quantity = item.quantity or 1
        paid_unit_price = item.total_price / quantity
        checked_at = dt.datetime.now(dt.timezone.utc)
        if paid_unit_price <= WARRANTY_MIN_PRICE:
            item.has_warranty = False
            item.warranty_checked_at = checked_at
            return

        key = _normalized_code(item.barcode) or _normalized_product_name(item.name)
        try:
            if key not in self.cache:
                self.cache[key] = await self._lookup(item.name, item.barcode)
            result = self.cache[key]
        except (httpx.HTTPError, ValueError):
            logger.warning("Епіцентр К: не вдалося перевірити гарантію для %s", item.name, exc_info=True)
            return

        item.warranty_checked_at = checked_at
        if result is None:
            item.has_warranty = False
            return
        item.product_url = result.get("product_url")
        item.product_image_url = result.get("image_url")
        item.warranty_text = result.get("warranty_text")
        months = result.get("months")
        item.has_warranty = bool(months)
        if months:
            item.warranty_expires_at = _add_months(purchased_at, months)

    async def _lookup(self, name: str, barcode: str | None) -> dict | None:
        if self.client is None:
            raise RuntimeError("EpicentrWarrantyScanner must be used as an async context manager")
        target_code = _normalized_code(barcode)
        latin_model = " ".join(re.findall(r"[A-Za-z0-9][A-Za-z0-9./+\-]*", name))
        queries = list(dict.fromkeys(query for query in (name, latin_model) if len(query) >= 3))
        candidates: list[dict] = []
        candidate = None
        for query in queries:
            # Epicentr search intermittently returns an empty item list for a
            # valid exact query; one immediate retry is enough in observed use.
            for _ in range(2):
                response = await self.client.get(
                    f"{CATALOG_BASE_URL}/api/v1/search",
                    headers={
                        **_base_headers(),
                        "Authorization": f"Bearer {CATALOG_APP_TOKEN}",
                        "X-Fuser-Id": self.f_user_id,
                    },
                    params={
                        "find": query,
                        "lang": "ua",
                        "page": "1",
                        "search_size": "40",
                        "store_id": CATALOG_STORE_ID,
                        "session_id": self.session_id,
                    },
                )
                if response.status_code != 200:
                    response.raise_for_status()
                candidates = ((response.json().get("data") or {}).get("items")) or []
                if candidates:
                    break
            if target_code:
                candidate = next(
                    (
                        row
                        for row in candidates
                        if target_code
                        in {
                            _normalized_code(row.get("id")),
                            _normalized_code(row.get("article1C")),
                            _normalized_code(row.get("articlePotamus")),
                        }
                    ),
                    None,
                )
            if candidate is not None:
                break

        # Receipt articles are authoritative. Fuzzy matching is only a fallback
        # for sources that genuinely do not provide an article at all.
        if candidate is None and not target_code and candidates:
            target_name = _normalized_product_name(name)
            scored = [
                (SequenceMatcher(None, target_name, _normalized_product_name(row.get("name") or "")).ratio(), row)
                for row in candidates
            ]
            score, best = max(scored, key=lambda pair: pair[0])
            candidate = best if score >= 0.82 else None
        if candidate is None or not candidate.get("url"):
            return None

        product_url = f"{PRODUCT_PAGE_BASE}{candidate['url']}"
        product_page = await self.client.get(product_url, headers={"User-Agent": "Mozilla/5.0"})
        if product_page.status_code != 200:
            return None
        warranty = _parse_product_warranty(product_page.text)
        return {
            "product_url": product_url,
            "image_url": (candidate.get("img") or {}).get("url"),
            "warranty_text": warranty[0] if warranty else None,
            "months": warranty[1] if warranty else None,
        }


class EpicentrKAdapter(BaseAdapter):
    store_code = "epicentrk"

    async def start_login(self, phone_number: str) -> AuthResult:
        phone_digits = phone_number.lstrip("+")
        try:
            async with httpx.AsyncClient(timeout=20, base_url=BASE_URL) as client:
                anon = await client.post(
                    "/api/v2/auth/anonymous-token",
                    headers=_base_headers(),
                    json={"appId": str(uuid.uuid4())},
                )
                if anon.status_code != 200:
                    raise AdapterError(f"Епіцентр К: не вдалось відкрити сесію ({anon.status_code})")
                anon_token = anon.json()["accessToken"]

                session_headers = {**_base_headers(), "Authorization": f"Bearer {anon_token}"}
                site_user = await client.get("/api/v2/auth/info/site-user", headers=session_headers)
                if site_user.status_code != 200:
                    raise AdapterError(f"Епіцентр К: не вдалось отримати сесію ({site_user.status_code})")
                info = site_user.json()
                f_user_id, session_id = info["fUserId"], info["sessionId"]

                otp_headers = {**session_headers, "X-Fuser-Id": str(f_user_id)}
                otp = await client.post(
                    f"/api/v2/auth/otp-codes/generate?session_id={session_id}",
                    headers=otp_headers,
                    json={"phoneNumber": phone_digits},
                )
                if otp.status_code != 200:
                    raise AdapterError(f"Епіцентр К: не вдалось надіслати SMS ({otp.status_code}): {otp.text[:200]}")
        except httpx.HTTPError as exc:
            raise AdapterError(f"Епіцентр К: помилка з'єднання ({exc})") from exc
        except (KeyError, ValueError) as exc:
            raise AdapterError(f"Епіцентр К: неочікувана відповідь сервера ({exc})") from exc

        return AuthResult(
            pending=True,
            extra={
                "anon_token": anon_token,
                "f_user_id": f_user_id,
                "session_id": session_id,
                "phone_number": phone_digits,
            },
        )

    async def confirm_login(self, extra: dict, code: str) -> AuthResult:
        anon_token = extra.get("anon_token")
        f_user_id = extra.get("f_user_id")
        session_id = extra.get("session_id")
        phone_digits = extra.get("phone_number")
        if not (anon_token and f_user_id and session_id and phone_digits):
            raise AdapterError("Епіцентр К: сесія логіну застаріла — почніть спочатку")

        headers = {
            **_base_headers(),
            "Authorization": f"Bearer {anon_token}",
            "X-Fuser-Id": str(f_user_id),
        }
        try:
            async with httpx.AsyncClient(timeout=20, base_url=BASE_URL) as client:
                resp = await client.post(
                    f"/api/v2/auth/otp-codes/process?session_id={session_id}",
                    headers=headers,
                    json={"phoneNumber": phone_digits, "code": code},
                )
                if resp.status_code != 200:
                    raise AdapterError("Епіцентр К: невірний код підтвердження")
                payload = resp.json()
                real_token = payload["accessToken"]

                # sessionId survives the anonymous->real upgrade, but fUserId does NOT —
                # it's actually the user's own customer_id, only assigned once the token
                # is a real (not anonymous) one. Re-deriving it with the anon fUserId
                # gives a value the orders endpoints 500 on (confirmed live), so this
                # second call is mandatory, not an optimization.
                site_user = await client.get(
                    f"/api/v2/auth/info/site-user?session_id={session_id}",
                    headers={**_base_headers(), "Authorization": f"Bearer {real_token}", "X-Fuser-Id": str(f_user_id)},
                )
                if site_user.status_code != 200:
                    raise AdapterError(f"Епіцентр К: не вдалось оновити сесію після входу ({site_user.status_code})")
                real_f_user_id = site_user.json()["fUserId"]
        except httpx.HTTPError as exc:
            raise AdapterError(f"Епіцентр К: помилка з'єднання ({exc})") from exc
        except (KeyError, ValueError) as exc:
            raise AdapterError(f"Епіцентр К: неочікувана відповідь сервера ({exc})") from exc

        expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=payload.get("expiresIn", 2592000))
        return AuthResult(
            access_token=real_token,
            refresh_token=payload.get("refreshToken"),
            expires_at=expires_at,
            extra={"f_user_id": real_f_user_id, "session_id": session_id},
        )

    async def fetch_receipts(
        self, access_token: str, extra: dict, since: dt.datetime | None = None
    ) -> list[NormalizedReceipt]:
        f_user_id = extra.get("f_user_id")
        session_id = extra.get("session_id")
        if not (f_user_id and session_id):
            raise AdapterError("Епіцентр К: немає даних сесії — увійдіть повторно")

        headers = {
            **_base_headers(),
            "Authorization": f"Bearer {access_token}",
            "X-Fuser-Id": str(f_user_id),
        }
        receipts: list[NormalizedReceipt] = []

        async with httpx.AsyncClient(timeout=20, base_url=BASE_URL) as client:
            # The session_id/fUserId pair goes stale well before the access_token
            # itself expires — /api/v2/sales/orders/online (and /active) 500 with a
            # downstream-401 once it has, while /offline doesn't seem to check it at
            # all. Confirmed live: replaying a *stale* session_id verbatim (even
            # byte-for-byte identical headers/token to a real captured request)
            # fails, but re-deriving a fresh one right here — same call as step 5 of
            # login, just re-run — fixes it immediately. Cheap, so just always do it.
            refreshed = await client.get(
                "/api/v2/auth/info/site-user", headers=headers, params={"session_id": session_id}
            )
            if refreshed.status_code == 200:
                info = refreshed.json()
                session_id = info.get("sessionId") or session_id
                f_user_id = info.get("fUserId") or f_user_id
                headers["X-Fuser-Id"] = str(f_user_id)

            receipts += await self._fetch_channel(client, headers, session_id, "offline", since)
            try:
                receipts += await self._fetch_channel(client, headers, session_id, "online", since)
            except AdapterError:
                logger.warning("Епіцентр К: online orders недоступні цього разу, пропускаю", exc_info=True)

        try:
            async with EpicentrWarrantyScanner(
                access_token,
                {**extra, "session_id": session_id, "f_user_id": f_user_id},
            ) as scanner:
                await scanner.enrich_receipts(receipts)
        except httpx.HTTPError:
            # Warranty enrichment is useful metadata, but a temporary catalog
            # outage must not discard otherwise valid receipts.
            logger.warning("Епіцентр К: каталог гарантій тимчасово недоступний", exc_info=True)

        return receipts

    async def get_loyalty_code(self, access_token: str, extra: dict) -> LoyaltyCode:
        f_user_id = extra.get("f_user_id")
        session_id = extra.get("session_id")
        if not (f_user_id and session_id):
            raise AdapterError("Епіцентр К: немає даних сесії — увійдіть повторно")

        headers = {
            **_base_headers(),
            "Authorization": f"Bearer {access_token}",
            "X-Fuser-Id": str(f_user_id),
        }
        async with httpx.AsyncClient(timeout=20, base_url=BASE_URL) as client:
            resp = await client.get(
                "/api/v2/user/bonuses", headers=headers, params={"session_id": session_id}
            )
        if resp.status_code != 200:
            raise AdapterError(f"Епіцентр К: не вдалось отримати код картки ({resp.status_code})")
        barcode = resp.json().get("barcode")
        if not barcode:
            raise AdapterError("Епіцентр К: код картки відсутній у відповіді")
        # The only store of the five where the real app shows a 1D barcode, not a QR.
        return LoyaltyCode(value=barcode, display="barcode")

    async def get_balance(self, access_token: str, extra: dict) -> LoyaltyBalance:
        f_user_id = extra.get("f_user_id")
        session_id = extra.get("session_id")
        if not (f_user_id and session_id):
            raise AdapterError("Епіцентр К: немає даних сесії — увійдіть повторно")

        headers = {
            **_base_headers(),
            "Authorization": f"Bearer {access_token}",
            "X-Fuser-Id": str(f_user_id),
        }
        async with httpx.AsyncClient(timeout=20, base_url=BASE_URL) as client:
            resp = await client.get(
                "/api/v2/user/bonuses", headers=headers, params={"session_id": session_id}
            )
        if resp.status_code != 200:
            raise AdapterError(f"Епіцентр К: не вдалось отримати баланс ({resp.status_code})")
        data = resp.json()
        if "total" not in data:
            raise AdapterError("Епіцентр К: баланс відсутній у відповіді")
        # activeBonuses/notActiveBonuses/total come back in kopecks, not hryvnias
        # (confirmed live: activeBonuses=726 was the real app's displayed 7.26 грн) —
        # every other money field this API returns (order price, item price, ...) is
        # already in hryvnias, so this one's a genuine oddity, not a pattern to expect
        # elsewhere.
        pending = data.get("notActiveBonuses")
        return LoyaltyBalance(
            total=float(data["total"]) / 100,
            pending=(float(pending) / 100) if pending is not None else None,
        )

    async def search_catalog(self, access_token: str | None, extra: dict, query: str) -> list[CatalogProduct]:
        """GET api.epicentrk.ua/api/v1/search?find=<query> — different host than
        receipts/balance, and (confirmed live) a different Bearer token too —
        see CATALOG_APP_TOKEN above. X-Fuser-Id/session_id are still this
        account's own — re-derives a fresh pair first via the account's real
        JWT, same session_id/fUserId-goes-stale caveat as fetch_receipts (see
        its docstring), cheap enough to always do.

        Epicentr sometimes answers a near-exact query with a category
        *redirect* instead of a product list (confirmed live, e.g. "труба
        профильная"/"унітаз" -> `type: "redirect...", data: null`) — treated
        as a (rare) miss rather than followed, to keep this a plain product
        search instead of a mini category-browser."""
        f_user_id = extra.get("f_user_id")
        session_id = extra.get("session_id")
        if not (access_token and f_user_id and session_id):
            raise AdapterError("Епіцентр К: немає даних сесії — увійдіть повторно")

        headers = {
            **_base_headers(),
            "Authorization": f"Bearer {access_token}",
            "X-Fuser-Id": str(f_user_id),
        }
        async with httpx.AsyncClient(timeout=20) as client:
            refreshed = await client.get(
                f"{BASE_URL}/api/v2/auth/info/site-user", headers=headers, params={"session_id": session_id}
            )
            if refreshed.status_code == 200:
                info = refreshed.json()
                session_id = info.get("sessionId") or session_id
                f_user_id = info.get("fUserId") or f_user_id

            resp = await client.get(
                f"{CATALOG_BASE_URL}/api/v1/search",
                headers={**headers, "Authorization": f"Bearer {CATALOG_APP_TOKEN}", "X-Fuser-Id": str(f_user_id)},
                params={
                    "find": query,
                    "lang": "ua",
                    "page": "1",
                    "search_size": "40",
                    "store_id": CATALOG_STORE_ID,
                    "session_id": session_id,
                },
            )
        if resp.status_code != 200:
            raise AdapterError(f"Епіцентр К: пошук товарів повернув {resp.status_code}")
        data = resp.json()
        items = ((data.get("data") or {}).get("items")) or []

        products: list[CatalogProduct] = []
        for item in items:
            try:
                price = float(item["price"])
            except (KeyError, TypeError, ValueError):
                continue
            brand = next(
                (p.get("values") for p in (item.get("properties") or []) if p.get("name") == "Бренд"),
                None,
            )
            availability = item.get("availabilityStatus") or {}
            products.append(
                CatalogProduct(
                    name=item.get("name") or "",
                    price=price,
                    unit=item.get("measure"),
                    brand=brand,
                    image_url=(item.get("img") or {}).get("url"),
                    product_url=f"{PRODUCT_PAGE_BASE}{item['url']}" if item.get("url") else None,
                    in_stock=True if availability.get("code") == 100 else None,
                )
            )
        return products

    async def _fetch_channel(
        self,
        client: httpx.AsyncClient,
        headers: dict,
        session_id: str,
        channel: str,
        since: dt.datetime | None,
    ) -> list[NormalizedReceipt]:
        out: list[NormalizedReceipt] = []
        page = 0
        limit = 40
        while True:
            params = {"Page": page, "Limit": limit, "session_id": session_id}
            if channel == "online":
                params["Status"] = "All"
            if since:
                params["From"] = since.astimezone(dt.timezone.utc).isoformat()

            resp = await client.get(f"/api/v2/sales/orders/{channel}", headers=headers, params=params)
            if resp.status_code != 200:
                raise AdapterError(f"Епіцентр К: список замовлень {channel} ({resp.status_code})")
            payload = resp.json()
            items = payload.get("items") or []
            if not items:
                break

            for row in items:
                detail_resp = await client.get(
                    f"/api/v2/sales/orders/{channel}/{row['id']}",
                    headers=headers,
                    params={"session_id": session_id},
                )
                if detail_resp.status_code != 200:
                    continue
                out.append(_normalize(detail_resp.json(), channel))

            if page + 1 >= payload.get("totalPages", 1):
                break
            page += 1

        return out


def _parse_dt(date_str: str | None, time_str: str | None) -> dt.datetime:
    day = dt.datetime.fromisoformat(date_str).date() if date_str else dt.date.today()
    if time_str:
        # "time" is declared as a timespan in the API's own schema, but comes through
        # as "HH:MM:SS.ffffff" — same shape as a clock time since midnight, which is
        # exactly what we want here.
        h, m, s = time_str.split(":")
        seconds = float(s)
        moment = dt.datetime.combine(day, dt.time(int(h), int(m), int(seconds), int((seconds % 1) * 1_000_000)))
    else:
        moment = dt.datetime.combine(day, dt.time.min)
    return store_local_to_utc(moment)


def _order_is_canceled(order: dict) -> bool:
    statuses = " ".join(
        str(order.get(key) or "").strip().lower()
        for key in ("orderStatus", "status", "statusGroup")
    )
    return any(marker in statuses for marker in ("canceled", "cancelled", "скасовано", "скасован"))


def _normalize(order: dict, channel: str) -> NormalizedReceipt:
    items = []
    for g in order.get("goods") or []:
        quantity = float(g.get("quantity") or 1)
        price = float(g.get("price") or 0)
        lines_total = float(g.get("linesTotal") if g.get("linesTotal") is not None else price * quantity)
        old_total = g.get("oldLinesTotal")
        discount = round(float(old_total) - lines_total, 2) if old_total is not None else 0.0
        items.append(
            NormalizedItem(
                name=(g.get("name") or "").strip(),
                quantity=quantity,
                unit=g.get("measure"),
                unit_price=price,
                total_price=lines_total,
                discount=discount,
                barcode=g.get("article") or g.get("articul1C"),
                product_code=g.get("article") or g.get("articul1C"),
                product_image_url=g.get("picture"),
            )
        )

    price = float(order.get("price") or 0)
    old_price = order.get("oldPrice")
    total_discount = round(float(old_price) - price, 2) if old_price is not None else None

    payment_type = None
    if channel == "online":
        payment_type = (order.get("payment") or {}).get("paySystemName")

    return NormalizedReceipt(
        external_id=f"{channel}:{order['id']}",
        purchased_at=_parse_dt(order.get("date"), order.get("time")),
        total_amount=price,
        shop_name=order.get("seller"),
        shop_address=order.get("address"),
        payment_type=payment_type,
        channel=channel,
        items=items,
        raw=order,
        total_discount=total_discount,
        bonuses=(order.get("bonuses") or {}).get("obtained"),
        bonuses_used=order.get("bonusUsed"),
        excluded=_order_is_canceled(order),
        exclusion_reason="Замовлення скасовано" if _order_is_canceled(order) else None,
    )
