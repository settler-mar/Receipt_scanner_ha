"""Fozzy (ua.fozzy.android).

The mobile auth flow is gated by an on-device reCAPTCHA token, but Fozzy also
provides an official browser OAuth/PKCE flow for fozzyshop.ua. We drive that
page in a real Chromium session, like the Dnipro-M adapter drives the store's
own login page: the page obtains its own Cloudflare Turnstile token and the user
only has to enter the SMS code in our UI.

  1. POST https://auth.fozzyshop.ua/SendOtp
     {"captchaInput": "<reCAPTCHA token, ~1500 chars>", "phoneNumber": "+380..."}
     -> {"secondsLeftTilNextTry": 300, "otpLength": 6, "token": "<otp_token JWT>"}
  2. POST https://auth.fozzyshop.ua/ConfirmPhone
     {"otpCode": "123456", "phoneNumber": "+380...", "token": "<otp_token>"}
     -> {"token": "<auth_token JWT with mobilephone/barcode/member claims>"}
  3. POST https://auth.fozzyshop.ua/connect/token
     Authorization: Basic Zm96enlfbW9iaWxlX2FuZHJvaWQ6Zm96enlfbW9iaWxlX2FuZHJvaWRfc2VjcmV0
     form: grant_type=auth_token&auth_token=<auth_token>&client_id=fozzy_mobile_android
     -> {"access_token": "<JWT, used as Bearer everywhere else>", "expires_in", "refresh_token", ...}

No CAPTCHA is solved or bypassed by the adapter. The official auth page runs
unchanged in Chromium and mints its own Turnstile token.

Both the mobile and web OAuth clients accept grant_type=refresh_token and rotate
the refresh token on every use. Credential.extra records which client issued the
token so refresh() uses the matching client authentication method.

Two receipt sources, each on its own API host but the same Bearer token works for
both:
  - loyaltyexpapi.fozzy.ua  in-store (offline) purchases — chequeheaders + chequeinfo
  - api.tecom.fozzyshop.ua  delivery/Click&Collect orders — a JSON-RPC gateway
    ("TEComGlobal", same {"Method", "data"} shape as Fora's FZGlobal but with the
    Data key lowercased): OrderHeadsList lists orders (and already includes the
    total discount as `sumActualDiscount`, negative), GetOrderDetail({"orderId": id})
    returns line items. Items with lineStateId==1 ("Видалено") were removed from the
    order during picking/substitution and have countActual=0 — skipped entirely.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import json
import logging
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from app.core.time import store_local_to_utc

import httpx
from patchright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from app.adapters.base import (
    AdapterError,
    AuthResult,
    BaseAdapter,
    CatalogProduct,
    LoyaltyCode,
    NormalizedItem,
    NormalizedReceipt,
)

BASE_URL = "https://loyaltyexpapi.fozzy.ua/api"
TECOM_URL = "https://api.tecom.fozzyshop.ua/api/2.0/exec/TEComGlobal"
AUTH_BASE = "https://auth.fozzyshop.ua"
WEB_CLIENT_ID = "fozzy_shop_client"
WEB_REDIRECT_URI = "https://fozzyshop.ua/login"
AUTH_SESSION_TTL = 10 * 60
CONNECT_BASIC_AUTH = "Basic Zm96enlfbW9iaWxlX2FuZHJvaWQ6Zm96enlfbW9iaWxlX2FuZHJvaWRfc2VjcmV0"
# Plain REST search — same host as the TEComGlobal RPC gateway (api.tecom.fozzyshop.ua)
# but a different API version/style, not a "Method" call. MerchantId=4 is a fixed
# "Fozzy" brand id within the shared Fozzy Group backend; FilialId=51 is this
# account's own/nearby store, captured live from a real in-app search (RESEARCH.md)
# — re-capture if search results ever look wrong for a different city.
CATALOG_URL = "https://api.tecom.fozzyshop.ua/api/3.0/Catalog/products"
CATALOG_MERCHANT_ID = "4"
CATALOG_FILIAL_ID = "51"

_FRACTION_RE = re.compile(r"\.(\d+)")
logger = logging.getLogger(__name__)


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _oauth_url(state: str, challenge: str) -> str:
    params = {
        "client_id": WEB_CLIENT_ID,
        "scope": "openid profile FozzyMobileApi offline_access",
        "redirect_uri": WEB_REDIRECT_URI,
        "state": state,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTH_BASE}/connect/authorize?{urllib.parse.urlencode(params)}"


async def _launch_browser() -> tuple[Playwright, Browser]:
    pw = await async_playwright().start()
    try:
        # Headed Chromium is rendered into Xvfb in Docker. The official page,
        # not this adapter, is responsible for its Turnstile challenge.
        return pw, await pw.chromium.launch(headless=False, channel="chrome")
    except Exception as exc:
        await pw.stop()
        raise AdapterError(
            "Fozzy: Chromium не запускається; встановіть браузер Patchright і перевірте sandbox runtime"
        ) from exc


@dataclass
class _LoginSession:
    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page
    verifier: str
    state: str
    expires_at: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def close(self) -> None:
        for step in (self.context.close, self.browser.close, self.playwright.stop):
            try:
                await step()
            except Exception:
                pass


_login_sessions: dict[str, _LoginSession] = {}
_sessions_lock = asyncio.Lock()


async def _expire_session_after(session_id: str, session: _LoginSession) -> None:
    await asyncio.sleep(AUTH_SESSION_TTL)
    async with _sessions_lock:
        if _login_sessions.get(session_id) is not session:
            return
        _login_sessions.pop(session_id, None)
    await session.close()


async def _pop_session(session_id: str | None) -> _LoginSession:
    if not session_id:
        raise AdapterError("Fozzy: сесія входу відсутня — почніть вхід спочатку")
    async with _sessions_lock:
        session = _login_sessions.pop(session_id, None)
    if session is None or session.expires_at <= time.monotonic():
        if session is not None:
            await session.close()
        raise AdapterError("Fozzy: сесія входу прострочена — почніть вхід спочатку")
    return session


async def _wait_for_oauth_code(page: Page, expected_state: str, timeout_s: float = 35) -> str:
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def on_request(request) -> None:
        if not request.url.startswith(WEB_REDIRECT_URI):
            return
        query = urllib.parse.parse_qs(urllib.parse.urlparse(request.url).query)
        if query.get("state", [None])[0] != expected_state:
            if not future.done():
                future.set_exception(
                    AdapterError("Fozzy: OAuth state не збігається — почніть вхід спочатку")
                )
            return
        code = query.get("code", [None])[0]
        if code and not future.done():
            future.set_result(code)

    page.on("request", on_request)
    try:
        return await asyncio.wait_for(future, timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        raise AdapterError("Fozzy: сайт не повернув код авторизації") from exc
    finally:
        page.remove_listener("request", on_request)


def _local_phone(phone_number: str) -> str:
    digits = re.sub(r"\D", "", phone_number)
    if digits.startswith("380"):
        digits = digits[3:]
    elif digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 9:
        raise AdapterError("Fozzy: некоректний номер телефону")
    return f"+380{digits}"


def _decode_jwt_claims(token: str) -> dict:
    """Unverified decode of a JWT's payload segment — fine here since we're only
    reading a claim out of a token we ourselves obtained and already trust as a
    Bearer credential, not authenticating anything with it."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except (IndexError, ValueError):
        return {}


def _parse_dt(value: str) -> dt.datetime:
    """TEComGlobal timestamps carry .NET-style fractional seconds with an
    arbitrary number of digits (".63", ".2087532", ...) — Python's fromisoformat
    only accepts exactly 3 or 6, so pad/truncate to 6 first."""
    value = _FRACTION_RE.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), value, count=1)
    return store_local_to_utc(dt.datetime.fromisoformat(value))


class FozzyAdapter(BaseAdapter):
    store_code = "fozzy"

    async def start_login(self, phone_number: str) -> AuthResult:
        phone = _local_phone(phone_number)
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(24)
        pw, browser = await _launch_browser()
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        page = await context.new_page()
        saved = False
        try:
            await page.goto(
                _oauth_url(state, challenge), wait_until="domcontentloaded", timeout=30_000
            )
            phone_input = page.locator("#phone").first
            await phone_input.wait_for(state="visible", timeout=20_000)
            await phone_input.fill(phone)
            await page.locator("#checkbox").check()
            await page.locator("#checkboxinfo").check()
            submit = page.get_by_role("button", name="ОТРИМАТИ КОД").first
            deadline = time.monotonic() + 35
            while not await submit.is_enabled():
                if time.monotonic() >= deadline:
                    raise AdapterError(
                        "Fozzy: сторінка авторизації не пройшла перевірку Turnstile"
                    )
                await page.wait_for_timeout(250)
            await submit.click()
            code_input = page.locator(
                "input[autocomplete='one-time-code'], input[name*='Otp'], "
                "input[id*='Otp'], input[inputmode='numeric']"
            ).last
            await code_input.wait_for(state="visible", timeout=30_000)

            session_id = secrets.token_urlsafe(32)
            session = _LoginSession(
                pw, browser, context, page, verifier, state,
                time.monotonic() + AUTH_SESSION_TTL,
            )
            async with _sessions_lock:
                _login_sessions[session_id] = session
            asyncio.create_task(_expire_session_after(session_id, session))
            saved = True
            return AuthResult(pending=True, extra={"auth_session_id": session_id})
        except AdapterError:
            raise
        except Exception as exc:
            logger.exception("Fozzy browser login failed while sending SMS")
            raise AdapterError(f"Fozzy: не вдалося надіслати SMS: {type(exc).__name__}") from exc
        finally:
            if not saved:
                await context.close()
                await browser.close()
                await pw.stop()

    async def confirm_login(self, extra: dict, code: str) -> AuthResult:
        session = await _pop_session(extra.get("auth_session_id"))
        async with session.lock:
            try:
                page = session.page
                code_input = page.locator(
                    "input[autocomplete='one-time-code'], input[name*='Otp'], "
                    "input[id*='Otp'], input[inputmode='numeric']"
                ).last
                callback = asyncio.create_task(_wait_for_oauth_code(page, session.state))
                await code_input.fill(re.sub(r"\D", "", code))
                submit = page.get_by_role("button").filter(
                    has_text=re.compile(r"ПІДТВЕРД|УВІЙТИ|ДАЛІ", re.I)
                ).first
                if await submit.count() and await submit.is_visible():
                    await submit.click()
                auth_code = await callback
                async with httpx.AsyncClient(timeout=20) as client:
                    result = await _exchange_web(
                        client,
                        {
                            "grant_type": "authorization_code",
                            "code": auth_code,
                            "redirect_uri": WEB_REDIRECT_URI,
                            "code_verifier": session.verifier,
                        },
                    )
                result.extra = {"auth_session_id": None, "oauth_client": "web"}
                return result
            except AdapterError:
                raise
            except Exception as exc:
                logger.exception("Fozzy browser login failed while confirming SMS")
                raise AdapterError(
                    f"Fozzy: не вдалося підтвердити SMS-код: {type(exc).__name__}"
                ) from exc
            finally:
                await session.close()

    async def get_loyalty_code(self, access_token: str, extra: dict) -> LoyaltyCode:
        """No separate API call needed — the access_token itself is a JWT with the
        loyalty barcode as a claim (confirmed: same value the real app's card
        screen shows). Static (doesn't rotate), so no expires_at."""
        claims = _decode_jwt_claims(access_token)
        barcode = claims.get("barcode")
        if not barcode:
            raise AdapterError("Fozzy: код картки відсутній у токені")
        return LoyaltyCode(value=barcode, display="qr")

    async def refresh(self, credential_extra: dict, refresh_token: str | None) -> AuthResult:
        if not refresh_token:
            raise AdapterError("Fozzy: немає refresh_token, потрібен повторний вхід")
        async with httpx.AsyncClient(timeout=20) as client:
            if credential_extra.get("oauth_client") == "web":
                result = await _exchange_web(
                    client, {"grant_type": "refresh_token", "refresh_token": refresh_token}
                )
                result.extra = {"oauth_client": "web"}
                return result
            return await _exchange(
                client, {"grant_type": "refresh_token", "refresh_token": refresh_token}
            )

    async def search_catalog(self, access_token: str | None, extra: dict, query: str) -> list[CatalogProduct]:
        """GET .../api/3.0/Catalog/products?CustomFilter=<query> — a plain REST
        search on the same host (api.tecom.fozzyshop.ua) fetch_receipts already
        talks to for online orders, just a different API version/style (not the
        TEComGlobal RPC gateway). Same Bearer token + same required headers as
        fetch_receipts."""
        if not access_token:
            raise AdapterError("Fozzy: пошук по каталогу потребує токена")
        headers = {
            "Authorization": f"Bearer {access_token}",
            "appVersionName": "1.108.0GMS",
            "clientSdkVersion": "33",
            "brand": "Xiaomi",
            "x-client-platform": "android",
        }
        params = {
            "MerchantId": CATALOG_MERCHANT_ID,
            "FilialId": CATALOG_FILIAL_ID,
            "DisplayOnSite": "true",
            "DeliveryType": "2",
            "CustomFilter": query,
            "PageSize": "36",
            "PageNum": "1",
        }
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(CATALOG_URL, headers=headers, params=params)
        if resp.status_code != 200:
            raise AdapterError(f"Fozzy: пошук товарів повернув {resp.status_code}")
        data = resp.json()
        products: list[CatalogProduct] = []
        for item in data.get("items") or []:
            try:
                price = float(item["price"])
            except (KeyError, TypeError, ValueError):
                continue
            products.append(
                CatalogProduct(
                    name=item.get("name") or "",
                    price=price,
                    unit=item.get("unit"),
                    image_url=item.get("mainImage") or None,
                    in_stock=(item.get("quantity") or 0) > 0,
                )
            )
        return products

    async def fetch_receipts(
        self, access_token: str, extra: dict, since: dt.datetime | None = None
    ) -> list[NormalizedReceipt]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            # loyaltyexpapi.fozzy.ua 401s without these — not otherwise validated,
            # just required to be present.
            "appVersionName": "1.108.0GMS",
            "clientSdkVersion": "33",
            "brand": "Xiaomi",
            "x-client-platform": "android",
        }
        receipts: list[NormalizedReceipt] = []

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{BASE_URL}/1.0/chequeheaders", headers=headers)
            resp.raise_for_status()
            payload = resp.json()

            headers_flat = [
                cheque
                for month in payload.get("chequeMonthList") or []
                for cheque in month.get("chequeList") or []
            ]

            for header in headers_flat:
                purchased_at = store_local_to_utc(dt.datetime.fromisoformat(header["created"].replace("Z", "+00:00")))
                if since and purchased_at < since:
                    continue

                detail = await _fetch_detail(client, headers, header)
                receipts.append(_normalize_offline(header, detail, purchased_at))

            online = await _tecom_rpc(client, headers, "OrderHeadsList", {"forms": [1, 4], "limit": 50, "offset": 0})
            for head in (online or []):
                purchased_at = _parse_dt(head["createdDate"])
                if since and purchased_at < since:
                    continue
                detail = await _tecom_rpc(client, headers, "GetOrderDetail", {"orderId": head["id"]})
                receipts.append(_normalize_online(head, detail, purchased_at))

        return receipts


async def _fetch_detail(client: httpx.AsyncClient, headers: dict, header: dict) -> dict:
    params = {
        "filialId": header["filialId"],
        "chequeId": header["chequeId"],
        "created": header["created"],
    }
    resp = await client.get(
        f"{BASE_URL}/2.0/chequeinfo?{urllib.parse.urlencode(params)}", headers=headers
    )
    resp.raise_for_status()
    return resp.json().get("chequeInfo") or {}


def _normalize_offline(header: dict, detail: dict, purchased_at: dt.datetime) -> NormalizedReceipt:
    lines = detail.get("chequeLines") or []
    items = [_normalize_item(line) for line in lines]

    return NormalizedReceipt(
        external_id=str(header["chequeId"]),
        purchased_at=purchased_at,
        total_amount=float(header.get("sumReg") or 0),
        shop_name=header.get("filialName"),
        shop_address=header.get("cityName"),
        payment_type=header.get("payTypeName"),
        channel="offline",
        items=items,
        raw={"header": header, "detail": detail},
    )


async def _tecom_rpc(client: httpx.AsyncClient, headers: dict, method: str, data: dict):
    resp = await client.post(TECOM_URL, headers=headers, json={"Method": method, "data": data})
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("error") or payload.get("eComError"):
        raise AdapterError(f"Fozzy: {method} — {payload.get('error') or payload.get('eComError')}")
    return payload.get("data")


def _normalize_online(head: dict, detail: dict, purchased_at: dt.datetime) -> NormalizedReceipt:
    items = [
        NormalizedItem(
            name=it.get("name", "").strip(),
            quantity=float(it.get("countActual") or 0),
            unit=it.get("unit"),
            unit_price=float(it.get("price") or 0),
            total_price=float(it.get("sumActual") or 0),
            product_image_url=it.get("mainImage") or it.get("image"),
        )
        for it in (detail.get("items") or [])
        if it.get("lineStateId") != 1 and (it.get("countActual") or 0) > 0
    ]

    return NormalizedReceipt(
        external_id=f"online:{head['id']}",
        purchased_at=purchased_at,
        total_amount=float(head.get("sumActual") or 0),
        shop_name=head.get("filialName"),
        shop_address=head.get("deliveryAddress"),
        payment_type=head.get("paymentTypeName"),
        channel="online",
        items=items,
        total_discount=abs(float(head.get("sumActualDiscount") or 0)),
        raw={"head": head, "detail": detail},
    )


def _normalize_item(line: dict) -> NormalizedItem:
    lager = line.get("lagerInfo") or {}
    quantity = float(line.get("amount") or 1)
    total_price = float(line.get("sumReg") or 0)

    # The API doesn't send a per-unit price on the cheque line itself — derive it
    # from what was actually charged (total/qty), falling back to the catalog price
    # (lagerInfo.price) only if quantity is missing/zero and division isn't possible.
    unit_price = round(total_price / quantity, 2) if quantity else lager.get("price")

    return NormalizedItem(
        name=lager.get("name", "").strip(),
        quantity=quantity,
        unit_price=unit_price,
        total_price=total_price,
        discount=float(line.get("sumDiscount") or 0),
        category=(lager.get("category") or {}).get("name"),
        product_image_url=lager.get("mainImage") or lager.get("image"),
    )


async def _exchange(client: httpx.AsyncClient, grant_data: dict) -> AuthResult:
    """POST /connect/token with either an auth_token or a refresh_token grant —
    same request/response shape either way, just different body fields."""
    resp = await client.post(
        f"{AUTH_BASE}/connect/token",
        headers={"Authorization": CONNECT_BASIC_AUTH, "Content-Type": "application/x-www-form-urlencoded"},
        data={"client_id": "fozzy_mobile_android", **grant_data},
    )
    if resp.status_code != 200:
        raise AdapterError(f"Fozzy: не вдалось обміняти токен ({resp.status_code}): {resp.text[:200]}")
    payload = resp.json()
    expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=payload.get("expires_in", 3600))
    return AuthResult(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token"),
        expires_at=expires_at,
    )


async def _exchange_web(client: httpx.AsyncClient, grant_data: dict) -> AuthResult:
    resp = await client.post(
        f"{AUTH_BASE}/connect/token",
        data={"client_id": WEB_CLIENT_ID, **grant_data},
    )
    if resp.status_code != 200:
        raise AdapterError(
            f"Fozzy: не вдалось обміняти веб-токен ({resp.status_code}): {resp.text[:200]}"
        )
    payload = resp.json()
    expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
        seconds=payload.get("expires_in", 3600)
    )
    return AuthResult(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token"),
        expires_at=expires_at,
    )
