"""Dnipro-M adapter.

dnipro-m.ua has no mobile API — the personal cabinet is a plain Yii2 site (PHP
session cookie + CSRF), so the "access token" this adapter stores is a Patchright
storage-state JSON blob (a captured cookie jar). `_browser_fetch` replays it
through a headless Chromium for every cabinet call.

Login IS automated (phone + SMS), driven from the panel's normal OTP UI:
`start_login` opens a headless Chromium, opens the auth modal from the homepage,
types the phone and clicks "Отримати код"; `confirm_login` types the SMS code,
then captures the logged-in cookie jar. The same browser context is kept alive
between the two calls (keyed by `auth_session_id` in Credential.extra, short TTL).

The login form runs an *invisible* reCAPTCHA v3 — the page mints the
`captcha_token` itself; we only fill fields and click, no CAPTCHA handling. It
passes headless *only* with human-like input: land via the homepage (not straight
on the login page) and type char-by-char with random pauses (see `_human_type`).
Instant `fill()` from headless gets scored as a bot. Verified live 2026-08-28.
`scripts/dnipro_m_login.py` remains as a manual fallback that posts a captured
session to `POST /api/accounts/{id}/token`.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import io
import logging
import os
import random
import re
import secrets
import time
from dataclasses import dataclass, field
from app.core.time import store_local_to_utc
from typing import Any
from urllib.parse import quote, urljoin

import httpx
from patchright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from pypdf import PdfReader

from app.adapters.base import (ROTATED_ACCESS_TOKEN_KEY, AdapterError, AuthResult,
    BaseAdapter, CatalogProduct, LoyaltyBalance, NormalizedItem, NormalizedReceipt,
    NormalizedReceiptComponent)

BASE_URL = "https://dnipro-m.ua"
AUTH_SESSION_TTL = 10 * 60
logger = logging.getLogger(__name__)

_WARRANTY_RANGE_RE = re.compile(
    r"[Гг]арант\S*\s+(?:ді[єе]|гарантія)\S*\s+(?:з\s+)?(\d{2}\.\d{2}\.\d{4})\s+(?:по|до)\s+(\d{2}\.\d{2}\.\d{4})",
    re.IGNORECASE,
)
_WARRANTY_UNTIL_RE = re.compile(
    r"(?:гаранті(?:я|йний)|гарантия|warranty)[^\n]{0,100}?(?:до|по|until)\s+(\d{2}[./]\d{2}[./]\d{4})",
    re.IGNORECASE,
)


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _warranty_expiry(pdf_bytes: bytes) -> dt.datetime | None:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        match = _WARRANTY_RANGE_RE.search(text)
        expiry = match.group(2) if match else None
        if not expiry:
            until = _WARRANTY_UNTIL_RE.search(text)
            expiry = until.group(1).replace("/", ".") if until else None
        return store_local_to_utc(dt.datetime.strptime(expiry, "%d.%m.%Y")) if expiry else None
    except Exception:
        return None


def _is_auth_failure(status: int, url: str, content_type: str, payload: Any) -> bool:
    if status in {401, 403} or "/login/" in url:
        return True
    if "text/html" in content_type:
        text = str(payload).lower()
        return "login" in text or "увійти" in text or "войти" in text
    if isinstance(payload, dict):
        text = " ".join(str(payload.get(k, "")) for k in ("status", "message", "error")).lower()
        return any(word in text for word in ("unauthor", "необхідно увійти", "необходимо войти"))
    return False


async def _launch_browser(
    *, channel: str | None = "chrome", headless: bool = True
) -> tuple[Playwright, Browser]:
    pw = await async_playwright().start()
    try:
        # Use installed Google Chrome; Patchright recommends a branded Chrome
        # channel without extra fingerprint overrides.
        kwargs = {"headless": headless}
        if channel:
            kwargs["channel"] = channel
        return pw, await pw.chromium.launch(**kwargs)
    except Exception as exc:
        await pw.stop()
        raise AdapterError(
            "Дніпро-М: Google Chrome не запускається; виконайте `patchright install chrome`"
        ) from exc


async def _human_type(locator, text: str) -> None:
    """Type char-by-char with small random pauses. The login form's invisible
    reCAPTCHA v3 scores instant `fill()` from a headless browser as a bot —
    realistic keystroke cadence (plus landing via the homepage, not straight on
    the login modal) is what gets a passing score. Verified live 2026-08-28."""
    await locator.click()
    for char in text:
        await locator.press(char)
        await asyncio.sleep(random.uniform(0.05, 0.15))


async def _open_auth_modal(page: Page) -> None:
    await page.goto(f"{BASE_URL}/", wait_until="domcontentloaded", timeout=45_000)
    await page.wait_for_timeout(3_000)
    trigger = page.locator('button[aria-label="Вхід/Реєстрація"]').first
    await trigger.wait_for(state="visible", timeout=15_000)
    await trigger.click()


def _to_local_phone(phone_number: str) -> str:
    """Any of +380XXXXXXXXX / 380XXXXXXXXX / 0XXXXXXXXX -> 0XXXXXXXXX, which is
    what the login form's mask (`+38 (0__) ___ __ __`) expects typed in."""
    digits = re.sub(r"\D", "", phone_number)
    if digits.startswith("380"):
        digits = "0" + digits[3:]
    elif not digits.startswith("0"):
        digits = "0" + digits
    return digits


def _is_captcha_error(message: str | None) -> bool:
    text = (message or "").lower()
    return any(w in text for w in ("captcha", "капч", "робот", "людин", "human", "bot"))


# Substrings that mark an error line in the auth modal (uk + ru). "невірн" covers
# both "невірно" (wrong code) and "невірний номер"; "неправильн" the ru/uk "wrong
# code confirmation"; the rest are the reCAPTCHA / rate-limit rejections.
_LOGIN_ERROR_MARKERS = (
    "не вдалося підтвердити", "не удалось подтвердить", "перевищено ліміт",
    "превышен", "неправильн", "невірн", "неверн", "помилк", "ошибк",
)


async def _modal_text(page: Page) -> str:
    for selector in (".auth-phone", "[class*='modal']", "body"):
        try:
            text = await page.locator(selector).first.inner_text(timeout=2_000)
        except Exception:
            continue
        if text.strip():
            return text.lower()
    return ""


def _login_error_in(text: str) -> str | None:
    for line in text.splitlines():
        if any(marker in line for marker in _LOGIN_ERROR_MARKERS):
            return line.strip()
    return None


async def _await_login_step(page: Page, ready, *, timeout_s: float = 30.0) -> None:
    """Wait until `ready(page)` is truthy or the auth modal shows an error line.

    Replaces `page.expect_response` around the submit click: wrapping the click
    in the CDP response-waiter makes the login form's invisible reCAPTCHA v3
    score the request as a bot (verified live 2026-08-28 — the identical flow
    passes without it and fails with it, back to back)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if await ready(page):
                return
        except Exception:
            pass
        error = _login_error_in(await _modal_text(page))
        if error:
            raise AdapterError(error)
        await page.wait_for_timeout(1_000)
    raise AdapterError("час очікування вичерпано")


async def _wait_response(page: Page, url_part: str, *, timeout_s: float = 35.0) -> tuple[int, Any]:
    """Passively capture a login response without wrapping the triggering action.

    An expect_response context changes the timing of the click enough to lower Dnipro-M's
    invisible reCAPTCHA score. A passive response listener keeps the browser
    interaction identical to a normal user click.
    """
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    async def read_response(response) -> None:
        if url_part not in response.url or response.request.method != "POST":
            return
        try:
            payload = await response.json()
        except Exception:
            try:
                payload = await response.text()
            except Exception:
                payload = None
        if not future.done():
            future.set_result((response.status, payload))

    def on_response(response) -> None:
        # Patchright's event emitter does not await async callbacks.
        asyncio.create_task(read_response(response))

    page.on("response", on_response)
    try:
        status, payload = await asyncio.wait_for(future, timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        raise AdapterError(f"Дніпро-М: не отримано відповідь {url_part}") from exc
    finally:
        page.remove_listener("response", on_response)
    return status, payload


async def _wait_json_response(page: Page, url_part: str, *, timeout_s: float = 35.0) -> dict:
    status, payload = await _wait_response(page, url_part, timeout_s=timeout_s)
    if status < 200 or status >= 300 or not isinstance(payload, dict):
        raise AdapterError(f"Дніпро-М: {url_part} повернув некоректну відповідь")
    if str(payload.get("status", "")).lower() not in {"success", "ok"}:
        raise AdapterError(str(payload.get("message") or payload.get("error") or "помилка авторизації"))
    return payload


@dataclass
class _LoginSession:
    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page
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
    logger.info("Dnipro-M login session %s reached its TTL", session_id)
    await session.close()


async def _pop_session(session_id: str | None) -> _LoginSession:
    if not session_id:
        raise AdapterError("Дніпро-М: сесія входу відсутня — почніть вхід спочатку")
    async with _sessions_lock:
        session = _login_sessions.pop(session_id, None)
    if session is None or session.expires_at <= time.monotonic():
        if session is not None:
            await session.close()
        raise AdapterError("Дніпро-М: сесія входу прострочена — почніть вхід спочатку")
    return session


class DniproMAdapter(BaseAdapter):
    store_code = "dnipro-m"

    async def start_login(self, phone_number: str) -> AuthResult:
        local_phone = _to_local_phone(phone_number)
        logger.info("Dnipro-M login: opening headed browser on DISPLAY=%s for %s", os.environ.get("DISPLAY"), local_phone)
        # Deliberately a bare context (no locale/timezone override) — an invisible
        # reCAPTCHA v3 scores locale/timezone inconsistencies, and the flow is
        # already borderline. Mirrors the reference script backend/dnipro.py. No
        # retry: a quick reload+retype after a rejection only lowers the score.
        # The real headless implementations are rejected by the site's
        # invisible reCAPTCHA. In Docker this headed browser renders into Xvfb
        # (DISPLAY=:99), so no physical window is shown.
        pw, browser = await _launch_browser(channel=None, headless=False)
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        page = await context.new_page()
        saved = False
        try:
            await _open_auth_modal(page)
            phone_input = page.locator("#phone-auth").first
            await phone_input.wait_for(state="visible", timeout=15_000)
            await _human_type(phone_input, local_phone)
            button = page.get_by_role("button", name="Отримати код").first
            await button.wait_for(state="visible", timeout=15_000)
            sms_response = asyncio.create_task(_wait_json_response(page, "/phone-verification/"))
            await button.click()
            try:
                # DOM #code exists before the request succeeds, so it is not a
                # reliable indication that an SMS was sent. Trust the JSON XHR.
                await sms_response
            except AdapterError as exc:
                hint = (
                    " (reCAPTCHA сайту тимчасово підозрює це оточення — спробуйте за годину "
                    "або скористайтесь scripts/dnipro_m_login.py)"
                    if _is_captcha_error(str(exc)) else ""
                )
                raise AdapterError(f"Дніпро-М: не вдалося надіслати SMS: {exc}{hint}")

            session_id = secrets.token_urlsafe(32)
            session = _LoginSession(pw, browser, context, page, time.monotonic() + AUTH_SESSION_TTL)
            async with _sessions_lock:
                _login_sessions[session_id] = session
            asyncio.create_task(_expire_session_after(session_id, session))
            saved = True
            return AuthResult(pending=True, extra={"auth_session_id": session_id})
        except AdapterError:
            raise
        except Exception as exc:
            logger.exception("Dnipro-M login failed while sending SMS")
            raise AdapterError(f"Дніпро-М: не вдалося надіслати SMS: {type(exc).__name__}") from exc
        finally:
            if not saved and "sms_response" in locals() and not sms_response.done():
                sms_response.cancel()
            if not saved:
                await context.close()
                await browser.close()
                await pw.stop()

    async def confirm_login(self, extra: dict, code: str) -> AuthResult:
        session = await _pop_session(extra.get("auth_session_id"))
        async with session.lock:
            try:
                page = session.page
                code_input = page.locator("#code").first
                await code_input.wait_for(state="visible", timeout=10_000)
                await _human_type(code_input, re.sub(r"\D", "", code))
                # Keep this sequence identical to the proven backend/dnipro.py
                # flow. The page auto-submits the final digit and needs a short
                # quiet period to finish its own callback and establish cookies.
                await page.wait_for_timeout(3_000)
                try:
                    await page.wait_for_function(
                        """async () => {
                            try {
                                const response = await fetch('/auth/auth/user-info/', {
                                    credentials: 'same-origin',
                                    headers: {'X-Requested-With': 'XMLHttpRequest'},
                                });
                                const data = await response.json();
                                return data && data.auth === true;
                            } catch (_) {
                                return false;
                            }
                        }""",
                        timeout=30_000,
                    )
                except Exception as exc:
                    error = _login_error_in(await _modal_text(page))
                    if error:
                        raise AdapterError(f"Дніпро-М: не вдалося підтвердити SMS-код: {error}") from exc
                    raise AdapterError(
                        "Дніпро-М: не вдалося підтвердити SMS-код: час очікування вичерпано"
                    ) from exc
                state = await session.context.storage_state()
                return AuthResult(
                    access_token=json.dumps(state, separators=(",", ":")),
                    extra={"auth_session_id": None},
                )
            except AdapterError:
                raise
            except Exception as exc:
                logger.exception("Dnipro-M login failed while confirming SMS")
                raise AdapterError(f"Дніпро-М: не вдалося підтвердити SMS-код: {type(exc).__name__}") from exc
            finally:
                await session.close()

    def parse_import(self, payload: dict) -> list[NormalizedReceipt]:
        return [_normalize(entry) for entry in (payload.get("orders") or [])]

    async def _browser_json(self, access_token: str, extra: dict, path: str) -> dict:
        result = await self._browser_fetch(access_token, path, binary=False, extra=extra)
        if not isinstance(result, dict):
            raise AdapterError(f"Дніпро-М: {path} повернув некоректну відповідь")
        return result

    async def _browser_fetch(
        self, access_token: str, path: str, *, binary: bool, extra: dict | None = None
    ) -> Any:
        current_token = (extra or {}).get(ROTATED_ACCESS_TOKEN_KEY) or access_token
        try:
            state = json.loads(current_token)
            if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
                raise ValueError("storage state has no cookie list")
        except (TypeError, json.JSONDecodeError) as exc:
            raise AdapterError("Дніпро-М: збережена сесія пошкоджена — увійдіть повторно") from exc
        except ValueError as exc:
            raise AdapterError("Дніпро-М: збережена сесія пошкоджена — увійдіть повторно") from exc
        pw, browser = await _launch_browser()
        context = await browser.new_context(storage_state=state, locale="uk-UA")
        page = await context.new_page()
        try:
            await page.goto(f"{BASE_URL}/ru/", wait_until="domcontentloaded", timeout=25_000)
            result = await page.evaluate("""async ({path, binary}) => {
                const r = await fetch(path, {credentials: 'same-origin', headers: {
                    'Accept': binary ? 'application/pdf' : 'application/json, text/plain, */*',
                    'X-Requested-With': 'XMLHttpRequest'}});
                const contentType = r.headers.get('content-type') || '';
                let body;
                if (binary) {
                    const bytes = new Uint8Array(await r.arrayBuffer()); let value = '';
                    for (let i=0; i<bytes.length; i+=32768) value += String.fromCharCode(...bytes.subarray(i,i+32768));
                    body = btoa(value);
                } else { const text = await r.text(); try { body=JSON.parse(text); } catch (_) { body=text; } }
                return {status:r.status, url:r.url, contentType, body};
            }""", {"path": path, "binary": binary})
            logger.info("Dnipro-M request endpoint=%s status=%s redirect=%s", path, result["status"],
                        result["url"] != f"{BASE_URL}{path}")
            if _is_auth_failure(result["status"], result["url"], result["contentType"], result["body"]):
                raise AdapterError("Дніпро-М: браузерна сесія завершилась — потрібна повторна авторизація")
            if not 200 <= result["status"] < 300:
                raise AdapterError(f"Дніпро-М: {path} повернув {result['status']}")
            if extra is not None:
                refreshed_state = await context.storage_state()
                extra[ROTATED_ACCESS_TOKEN_KEY] = json.dumps(
                    refreshed_state, separators=(",", ":")
                )
            return base64.b64decode(result["body"]) if binary else result["body"]
        except AdapterError:
            raise
        except Exception as exc:
            logger.exception("Dnipro-M browser request failed endpoint=%s", path)
            raise AdapterError(f"Дніпро-М: помилка браузерного запиту {path}: {type(exc).__name__}") from exc
        finally:
            await context.close()
            await browser.close()
            await pw.stop()

    async def fetch_receipts(self, access_token: str, extra: dict,
                             since: dt.datetime | None = None) -> list[NormalizedReceipt]:
        receipts: list[NormalizedReceipt] = []
        offset = 0
        while True:
            path = "/cabinet/order/history/" + (f"?offset={offset}" if offset else "")
            data = await self._browser_json(access_token, extra, path)
            if data.get("status") != "success":
                if _is_auth_failure(200, path, "application/json", data):
                    raise AdapterError("Дніпро-М: браузерна сесія завершилась — потрібна повторна авторизація")
                raise AdapterError(f"Дніпро-М: історія замовлень: {data.get('message') or data.get('status')}")
            orders = data.get("orders") or []
            if not orders:
                break
            stop = False
            for order in orders:
                receipt = _normalize(order)
                if since and receipt.purchased_at < since:
                    stop = True
                    break
                if receipt.has_warranty:
                    try:
                        document = await self.get_warranty_document(access_token, extra, receipt.external_id)
                        expiry = _warranty_expiry(document)
                        receipt.warranty_expires_at = expiry
                        for item in receipt.items:
                            if item.has_warranty:
                                item.warranty_expires_at = expiry
                    except AdapterError:
                        logger.warning("Дніпро-М: гарантійний PDF %s недоступний", receipt.external_id, exc_info=True)
                receipts.append(receipt)
            if stop or not data.get("orders_left"):
                break
            offset = data.get("offset") or (offset + len(orders))
        return receipts

    async def get_balance(self, access_token: str, extra: dict) -> LoyaltyBalance:
        data = (
            await self._browser_json(access_token, extra, "/cabinet/default/bonuses-info/")
        ).get("data") or {}
        if "total_bonuses" not in data:
            raise AdapterError("Дніпро-М: баланс відсутній у відповіді")
        return LoyaltyBalance(total=float(data["total_bonuses"]), discount_percent=_to_float(data.get("rate")))

    async def search_catalog(self, access_token: str | None, extra: dict, query: str) -> list[CatalogProduct]:
        headers = {"Accept": "application/json, text/plain, */*", "X-Requested-With": "XMLHttpRequest"}
        async with httpx.AsyncClient(timeout=15, base_url=BASE_URL) as client:
            response = await client.get("/search/drop-down/", headers=headers, params={"q": query})
        if response.status_code != 200:
            raise AdapterError(f"Дніпро-М: пошук товарів повернув {response.status_code}")
        products = []
        for item in response.json().get("products") or []:
            price = _to_float(item.get("price_new"))
            if price is None:
                continue
            name = item.get("name") or {}
            products.append(CatalogProduct(name=name.get("uk") or name.get("ru") or "", price=price,
                image_url=item.get("product_photo"), product_url=item.get("url"), in_stock=item.get("is_available")))
        return products

    async def get_warranty_document(self, access_token: str | None, extra: dict, external_id: str) -> bytes:
        if not access_token:
            raise AdapterError("Дніпро-М: сесія відсутня — потрібна повторна авторизація")
        content = await self._browser_fetch(
            access_token,
            f"/cabinet/order/get-order-warranty-card/?order_id={quote(external_id)}",
            binary=True,
            extra=extra,
        )
        if not content or not content.startswith(b"%PDF"):
            raise AdapterError("Дніпро-М: сервер не повернув гарантійний PDF")
        return content


def _normalize(o: dict) -> NormalizedReceipt:
    purchased_at = store_local_to_utc(dt.datetime.fromisoformat(o["created_at"]))
    delivery_data = o.get("delivery_data") or {}
    current_bonus = o.get("current_purchase_bonuses")
    debit = o.get("bonuses_debiting_amount")
    items = [_item_from(item) for item in (o.get("order_items") or [])]
    has_warranty = bool(o.get("has_warranty_card"))
    if has_warranty:
        candidates = items if len(items) == 1 else [item for item in items if item.total_price / (item.quantity or 1) > 1000]
        checked_at = dt.datetime.now(dt.timezone.utc)
        for item in candidates:
            item.has_warranty = True
            item.warranty_text = "Гарантійний талон Дніпро-М"
            item.warranty_checked_at = checked_at
    components = ([NormalizedReceiptComponent(
        kind="discount", amount=-abs(_to_float(debit) or 0.0), code="bonuses_debiting_amount",
        label="Списані бонуси", source="dnipro_m.order",
        raw={"bonuses_debiting_amount": debit},
    )] if _to_float(debit) else [])
    return NormalizedReceipt(external_id=str(o["order_number"]), purchased_at=purchased_at,
        total_amount=_to_float(o.get("total")) or 0.0, shop_name=o.get("delivery_name"),
        shop_address=delivery_data.get("address") or o.get("delivery_city_name"),
        payment_type=o.get("payment_name"), channel="online" if o.get("creation_provider") == "site" else "offline",
        items=items,
        components=components,
        bonuses=_to_float(current_bonus) if current_bonus not in (None, "") else None,
        bonuses_used=_to_float(debit) if debit is not None else None,
        has_warranty=has_warranty, raw=o)


def _item_from(it: dict) -> NormalizedItem:
    price = _to_float(it.get("price")) or 0.0
    qty = _to_float(it.get("itemQuantity")) or 1.0
    discounted = _to_float(it.get("discount_cost"))
    unit_after = discounted if discounted is not None else price
    return NormalizedItem(name=(it.get("name") or "").strip(), quantity=qty, unit_price=price,
        total_price=round(unit_after * qty, 2), discount=round((price - unit_after) * qty, 2),
        barcode=it.get("code"),
        product_url=urljoin(BASE_URL, it["url"]) if it.get("url") else None,
        product_image_url=urljoin(BASE_URL, it["product_photo"]) if it.get("product_photo") else None)
