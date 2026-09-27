"""Interactive Prom.ua (unified Rozetka / Clerk account) login in a Docker-hosted
Patchright session.

Prom.ua moved buyer auth to a unified Rozetka account served through Clerk. There
is no password: the flow is phone number -> SMS one-time code. This service
mirrors ``aliexpress_login`` — a headed Chromium (rendered into Xvfb in Docker),
step-by-step progress, manual takeover with screenshot streaming and
click/key/text control — so the frontend browser-login panel is shared between
the two stores.

On success the captured ``storage_state`` JSON is the credential; PromAdapter
replays it against ``my.prom.ua/cabinet/user/graphql`` (see ``adapters/prom.py``).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import secrets
import time
from dataclasses import dataclass

from patchright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from app.adapters.base import AdapterError

HOME_URL = "https://prom.ua/ua/"
ORDERS_URL = "https://my.prom.ua/ua/cabinet/user/orders"
ROZETKA_HOME_URL = "https://rozetka.com.ua/ua/"
ROZETKA_ORDERS_URL = "https://rozetka.com.ua/ua/cabinet/orders/"
OLX_HOME_URL = "https://www.olx.ua/uk/"
OLX_ORDERS_URL = "https://www.olx.ua/uk/delivery/"
LOGIN_TTL = 10 * 60
VIEWPORT = {"width": 1400, "height": 1000}


@dataclass
class LoginSession:
    id: str
    account_id: str
    expires_at: float
    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page
    site: str = "prom"

    async def close(self) -> None:
        for step in (self.context.close, self.browser.close, self.playwright.stop):
            with contextlib.suppress(Exception):
                await step()


@dataclass
class LoginProgress:
    account_id: str
    step: str
    message: str
    expires_at: float
    page: Page | None = None
    manual_requested: bool = False


class ManualTakeover(Exception):
    pass


_sessions: dict[str, LoginSession] = {}
_progress: dict[str, LoginProgress] = {}
_lock = asyncio.Lock()


# --------------------------------------------------------------------------- #
# Progress helpers                                                             #
# --------------------------------------------------------------------------- #
def _set_progress(session_id: str, step: str, message: str) -> None:
    progress = _progress.get(session_id)
    if progress is not None:
        if not progress.manual_requested or step in {
            "manual", "saving_session", "importing_orders", "done", "error",
        }:
            progress.step = step
            progress.message = message
        progress.expires_at = time.monotonic() + LOGIN_TTL


def update_login_progress(session_id: str | None, step: str, message: str) -> None:
    if session_id:
        _set_progress(session_id, step, message)


def get_login_progress(account_id: str, session_id: str) -> dict:
    progress = _progress.get(session_id)
    if progress is None or progress.account_id != account_id:
        raise AdapterError("Prom.ua: сесію авторизації не знайдено")
    return {"session_id": session_id, "step": progress.step, "message": progress.message}


def _raise_if_manual_requested(session_id: str | None) -> None:
    if session_id and _progress.get(session_id) and _progress[session_id].manual_requested:
        raise ManualTakeover


# --------------------------------------------------------------------------- #
# Session bookkeeping                                                          #
# --------------------------------------------------------------------------- #
async def _cleanup() -> None:
    now = time.monotonic()
    async with _lock:
        ids = [sid for sid, item in _sessions.items() if item.expires_at <= now]
        expired = [_sessions.pop(sid) for sid in ids]
    for item in expired:
        await item.close()
    for sid, progress in list(_progress.items()):
        if progress.expires_at <= now:
            _progress.pop(sid, None)


async def _save_login_session(session: LoginSession) -> None:
    async with _lock:
        old = [sid for sid, item in _sessions.items() if item.account_id == session.account_id]
        previous = [_sessions.pop(sid) for sid in old]
        _sessions[session.id] = session
    for item in previous:
        await item.close()


async def _get(account_id: str, session_id: str) -> LoginSession:
    await _cleanup()
    async with _lock:
        session = _sessions.get(session_id)
    if session is None or session.account_id != account_id:
        raise AdapterError("Prom.ua: сесія входу завершилась — почніть знову")
    session.expires_at = time.monotonic() + LOGIN_TTL
    return session


async def close_login(session_id: str, *, keep_progress: bool = False) -> None:
    async with _lock:
        session = _sessions.pop(session_id, None)
    if session is not None:
        await session.close()
    if not keep_progress:
        _progress.pop(session_id, None)


# --------------------------------------------------------------------------- #
# Browser plumbing                                                             #
# --------------------------------------------------------------------------- #
async def _launch() -> tuple[Playwright, Browser]:
    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.launch(
            headless=False,
            channel="chrome",
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        return playwright, browser
    except Exception as exc:
        await playwright.stop()
        raise AdapterError("Prom.ua: не вдалося запустити Chromium") from exc


def _page_roots(page: Page):
    """The top document plus every child frame — the Clerk/Rozetka auth widget
    renders inside a cross-origin iframe."""
    yield page
    for frame in page.frames:
        if frame != page.main_frame:
            yield frame


async def _first_visible(page: Page, selectors: list[str]):
    for root in _page_roots(page):
        for selector in selectors:
            try:
                locator = root.locator(selector)
                if await locator.count() and await locator.first.is_visible():
                    return locator.first
            except Exception:
                continue
    return None


async def _click_named(page: Page, pattern: str, *, timeout: float = 8_000) -> bool:
    regex = re.compile(pattern, re.I)
    for root in _page_roots(page):
        for candidate in (
            root.get_by_role("button", name=regex),
            root.get_by_role("link", name=regex),
            root.get_by_text(regex, exact=True),
        ):
            try:
                if await candidate.count() and await candidate.first.is_visible():
                    await candidate.first.click(timeout=timeout)
                    return True
            except Exception:
                continue
    return False


def _to_intl_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if digits.startswith("380") and len(digits) == 12:
        return "+" + digits
    if digits.startswith("0") and len(digits) == 10:
        return "+38" + digits
    if len(digits) == 9:
        return "+380" + digits
    raise AdapterError("Prom.ua: номер телефону має формат +380XXXXXXXXX або 0XXXXXXXXX")


_PHONE_SELECTORS = [
    'input[type="tel"]',
    'input[name*="phone" i]',
    'input[autocomplete="tel"]',
    'input[placeholder*="телефон" i]',
    'input[placeholder*="Номер" i]',
]
# Tight, on purpose: a loose "numeric input" match false-positives on the
# cabinet's own order-search box once Clerk logs in straight from the phone step.
_OTP_STRONG_SELECTORS = [
    'input[autocomplete="one-time-code"]',
    'input[name*="otp" i]',
    'input[name*="verification" i]',
    'input[name*="verificationCode" i]',
    'input[id*="otp" i]',
    'input[name*="code" i]',
    'input[id*="code" i]',
    'input[aria-label*="code" i]',
    'input[aria-label*="код" i]',
    'input[placeholder*="код" i]',
    'input[placeholder*="code" i]',
]
# A row of single-character boxes only counts as an OTP field when there are
# several of them together.
_OTP_BOX_SELECTOR = 'input[maxlength="1"]'


async def _find_phone_input(page: Page):
    return await _first_visible(page, _PHONE_SELECTORS)


async def _otp_inputs(page: Page) -> list:
    for root in _page_roots(page):
        for selector in _OTP_STRONG_SELECTORS:
            try:
                locator = root.locator(selector)
                count = await locator.count()
                visible = [locator.nth(i) for i in range(count) if await locator.nth(i).is_visible()]
                if visible:
                    return visible
            except Exception:
                continue
        try:
            boxes = root.locator(_OTP_BOX_SELECTOR)
            count = await boxes.count()
            visible = [boxes.nth(i) for i in range(count) if await boxes.nth(i).is_visible()]
            if len(visible) >= 4:
                return visible
        except Exception:
            continue
    return []


async def _open_login_form(page: Page, session_id: str | None, home_url: str, site_name: str) -> object:
    """Land on the homepage and surface the phone field.

    The header "Кабінет" button opens a side panel with "Увійти або
    зареєструватись", which opens the Clerk/Rozetka modal. Some layouts open the
    modal straight away, so the field is probed between every step."""
    await page.goto(home_url, wait_until="domcontentloaded", timeout=90_000)
    await page.wait_for_timeout(2_000)
    _raise_if_manual_requested(session_id)

    for attempt in range(3):
        phone = await _find_phone_input(page)
        if phone is not None:
            return phone
        _set_progress(session_id or "", "opening_login", f"Відкриваємо форму входу {site_name}")
        await _click_named(page, r"Кабінет|Кабинет|Увійти|Войти")
        await page.wait_for_timeout(800)
        await _click_named(page, r"Увійти або зареєструватись|Войти или зарегистрироваться|Увійти|Войти")
        phone = await _wait_for(page, _find_phone_input, timeout_s=12.0, session_id=session_id)
        if phone is not None:
            return phone
        await page.wait_for_timeout(500 + attempt * 500)
    return None


async def _wait_for(page: Page, probe, *, timeout_s: float, session_id: str | None):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        _raise_if_manual_requested(session_id)
        if await _captcha(page):
            raise AdapterError("Prom.ua показав CAPTCHA. Автоматичний вхід зупинено без спроби обходу.")
        result = await probe(page)
        if result:
            return result
        await page.wait_for_timeout(500)
    return None


async def _captcha(page: Page) -> bool:
    for root in _page_roots(page):
        with contextlib.suppress(Exception):
            if await root.locator(
                'iframe[src*="captcha" i], [class*="gotcha" i], [id*="captcha" i], [class*="captcha" i]'
            ).count():
                return True
    with contextlib.suppress(Exception):
        text = (await page.locator("body").inner_text(timeout=3_000)).lower()
        if any(word in text for word in ("підтвердьте, що ви не робот", "проверка безопасности", "gotcha")):
            return True
    return False


async def _is_logged_in(page: Page, orders_url: str, marker: str) -> bool:
    with contextlib.suppress(Exception):
        await page.goto(orders_url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(1_500)
    return marker in page.url and "sign-in" not in page.url.lower()


async def _state(context: BrowserContext) -> str:
    return json.dumps(await context.storage_state(), separators=(",", ":"))


async def _type_human(locator, value: str) -> None:
    await locator.click()
    for char in value:
        await locator.press(char)
        await asyncio.sleep(0.05)


async def _enter_phone(locator, intl_phone: str) -> None:
    """The Clerk phone field ships with a ``+38`` prefix already in place, so
    typing the full international number produces ``+38380…``. Clear the field
    first; if the prefix survives (masked input), type only the part after it."""
    await locator.click()
    await locator.press("Control+A")
    await locator.press("Delete")
    with contextlib.suppress(Exception):
        await locator.fill("")
    await asyncio.sleep(0.1)
    current = ""
    with contextlib.suppress(Exception):
        current = (await locator.input_value()) or ""
    digits_present = re.sub(r"\D", "", current)
    remainder = intl_phone[1:]  # drop leading "+"
    if digits_present and remainder.startswith(digits_present):
        remainder = remainder[len(digits_present):]
    else:
        remainder = intl_phone
    for char in remainder:
        await locator.press(char)
        await asyncio.sleep(0.05)


async def _fill_otp(page: Page, code: str) -> bool:
    digits = re.sub(r"\D", "", code)
    inputs = await _otp_inputs(page)
    if not inputs:
        return False
    if len(inputs) == 1:
        await _type_human(inputs[0], digits)
        return True
    await inputs[0].click()
    for char in digits:
        await page.keyboard.press(char)
        await asyncio.sleep(0.05)
    return True


async def _submit_phone(page: Page, phone_input) -> bool:
    """Submit the auth form that owns the phone input before using text fallbacks."""
    with contextlib.suppress(Exception):
        form = phone_input.locator("xpath=ancestor::form[1]")
        submit = form.locator('button[type="submit"], input[type="submit"]')
        if await submit.count() and await submit.first.is_visible():
            await submit.first.click(timeout=8_000)
            return True
    for root in _page_roots(page):
        with contextlib.suppress(Exception):
            submit = root.locator('button[type="submit"], input[type="submit"]')
            visible = [submit.nth(i) for i in range(await submit.count()) if await submit.nth(i).is_visible()]
            if visible:
                await visible[0].click(timeout=8_000)
                return True
    return False


async def _submit_otp(page: Page) -> bool:
    """Click the submit control associated with the currently visible OTP field."""
    inputs = await _otp_inputs(page)
    if not inputs:
        return False
    with contextlib.suppress(Exception):
        form = inputs[0].locator("xpath=ancestor::form[1]")
        submit = form.locator('button[type="submit"], input[type="submit"]')
        if await submit.count() and await submit.first.is_visible():
            await submit.first.click(timeout=8_000)
            return True
    return await _click_named(page, r"Підтвердити|Подтвердить|Продовжити|Продолжить|Увійти|Войти|Continue|Verify")


# --------------------------------------------------------------------------- #
# Public flow                                                                  #
# --------------------------------------------------------------------------- #
async def start_login(account_id: str, phone: str, session_id: str | None = None, *, site: str = "prom") -> dict:
    intl_phone = _to_intl_phone(phone)
    await _cleanup()
    session_id = session_id or secrets.token_urlsafe(24)
    site_name = {"rozetka": "Rozetka.ua", "olx": "OLX", "prom": "Prom.ua"}.get(site, "Prom.ua")
    home_url = {"rozetka": ROZETKA_HOME_URL, "olx": OLX_HOME_URL}.get(site, HOME_URL)
    orders_url = {"rozetka": ROZETKA_ORDERS_URL, "olx": OLX_ORDERS_URL}.get(site, ORDERS_URL)
    marker = {"rozetka": "/cabinet/orders", "olx": "/delivery/"}.get(site, "/cabinet/user/orders")
    _progress[session_id] = LoginProgress(
        account_id, "starting", "Запускаємо Chromium у контейнері", time.monotonic() + LOGIN_TTL,
    )
    playwright, browser = await _launch()
    context = await browser.new_context(viewport=VIEWPORT, locale="uk-UA")
    page = await context.new_page()
    _progress[session_id].page = page
    saved = False
    try:
        _set_progress(session_id, "opening_site", f"Відкриваємо {site_name}")
        phone_input = await _open_login_form(page, session_id, home_url, site_name)
        _raise_if_manual_requested(session_id)
        if phone_input is None:
            if await _captcha(page):
                raise AdapterError("Prom.ua показав CAPTCHA. Автоматичний вхід зупинено без спроби обходу.")
            raise AdapterError("Prom.ua: не знайдено поле телефону")

        _set_progress(session_id, "entering_phone", "Вводимо номер телефону")
        await _enter_phone(phone_input, intl_phone)
        _raise_if_manual_requested(session_id)

        _set_progress(session_id, "submitting_phone", "Натискаємо «Продовжити»")
        if not await _submit_phone(page, phone_input) and not await _click_named(
            page, r"Продовжити|Продолжить|Далі|Дальше|Продовжуй|Continue|Next"
        ):
            await phone_input.press("Enter")

        # Two possible outcomes after "Продовжити":
        #  * an SMS code field appears -> OTP step;
        #  * Clerk completes sign-in straight away (trusted session / no second
        #    factor for this number) -> already in the cabinet.
        _set_progress(session_id, "waiting_code", "Очікуємо поле SMS-коду або завершення входу")
        otp: list = []
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            _raise_if_manual_requested(session_id)
            if await _captcha(page):
                raise AdapterError("Prom.ua показав CAPTCHA. Автоматичний вхід зупинено без спроби обходу.")
            otp = await _otp_inputs(page)
            if otp:
                break
            site_host = {"rozetka": "rozetka.com.ua", "olx": "olx.ua"}.get(site, "prom.ua")
            cabinet_marker = "/delivery/" if site == "olx" else "/cabinet/"
            if site_host in page.url and cabinet_marker in page.url and "sign-in" not in page.url.lower():
                break
            await page.wait_for_timeout(700)

        if not otp:
            _set_progress(session_id, "checking_login", "Перевіряємо, чи вхід уже завершено")
            if await _is_logged_in(page, orders_url, marker):
                state = await _state(context)
                _set_progress(session_id, "saving_session", "Вхід завершено без SMS — зберігаємо сесію")
                await context.close()
                await browser.close()
                await playwright.stop()
                saved = True
                if session_id in _progress:
                    _progress[session_id].page = None
                return {"session_id": session_id, "step": "done", "storage_state": state}
            raise AdapterError("Prom.ua: після «Продовжити» не з'явилося ні поле SMS-коду, ні кабінет")

        session = LoginSession(
            session_id, account_id, time.monotonic() + LOGIN_TTL, playwright, browser, context, page, site,
        )
        await _save_login_session(session)
        saved = True
        _set_progress(session_id, "otp", "Prom.ua надіслав SMS-код")
        return {
            "session_id": session_id, "step": "otp",
            "message": "Prom.ua надіслав SMS-код. Введіть його нижче.",
        }
    except ManualTakeover:
        session = LoginSession(
            session_id, account_id, time.monotonic() + LOGIN_TTL, playwright, browser, context, page, site,
        )
        await _save_login_session(session)
        saved = True
        _set_progress(session_id, "manual", "Керування передано вам. Продовжуйте у браузері.")
        return {
            "session_id": session_id, "step": "manual",
            "message": "Керування передано вам. Продовжуйте у браузері.",
        }
    except AdapterError as exc:
        _set_progress(session_id, "error", str(exc))
        raise
    except Exception as exc:
        message = f"{site_name}: помилка входу ({type(exc).__name__})"
        _set_progress(session_id, "error", message)
        raise AdapterError(message) from exc
    finally:
        if not saved:
            if session_id in _progress:
                _progress[session_id].page = None
            with contextlib.suppress(Exception):
                await context.close()
            with contextlib.suppress(Exception):
                await browser.close()
            with contextlib.suppress(Exception):
                await playwright.stop()


async def submit_otp(account_id: str, session_id: str, code: str) -> dict:
    session = await _get(account_id, session_id)
    _set_progress(session_id, "submitting_otp", "Надсилаємо SMS-код")
    if not await _fill_otp(session.page, code):
        orders_url = {"rozetka": ROZETKA_ORDERS_URL, "olx": OLX_ORDERS_URL}.get(session.site, ORDERS_URL)
        marker = {"rozetka": "/cabinet/orders", "olx": "/delivery/"}.get(session.site, "/cabinet/user/orders")
        if await _is_logged_in(session.page, orders_url, marker):
            state = await _state(session.context)
            await close_login(session_id, keep_progress=True)
            return {"session_id": session_id, "step": "done", "storage_state": state}
        raise AdapterError("Prom.ua: поле коду більше не доступне — почніть вхід знову")

    await _submit_otp(session.page)
    await session.page.wait_for_timeout(3_000)
    if await _captcha(session.page):
        raise AdapterError("Prom.ua показав CAPTCHA. Автоматичний вхід зупинено.")
    orders_url = {"rozetka": ROZETKA_ORDERS_URL, "olx": OLX_ORDERS_URL}.get(session.site, ORDERS_URL)
    marker = {"rozetka": "/cabinet/orders", "olx": "/delivery/"}.get(session.site, "/cabinet/user/orders")
    if not await _is_logged_in(session.page, orders_url, marker):
        raise AdapterError("Prom.ua: код не підтверджено або сесія не створена")

    state = await _state(session.context)
    _set_progress(session_id, "saving_session", "Авторизацію підтверджено — зберігаємо сесію")
    await close_login(session_id, keep_progress=True)
    return {"session_id": session_id, "step": "done", "storage_state": state}


async def check_manual_login(account_id: str, session_id: str) -> dict:
    session = await _get(account_id, session_id)
    _set_progress(session_id, "checking_login", "Перевіряємо результат ручного входу")
    if await _captcha(session.page):
        return {
            "session_id": session_id, "step": "manual",
            "message": "Завершіть перевірку у браузері та перевірте ще раз.",
        }
    orders_url = {"rozetka": ROZETKA_ORDERS_URL, "olx": OLX_ORDERS_URL}.get(session.site, ORDERS_URL)
    marker = {"rozetka": "/cabinet/orders", "olx": "/delivery/"}.get(session.site, "/cabinet/user/orders")
    if not await _is_logged_in(session.page, orders_url, marker):
        _set_progress(session_id, "manual", "Вхід ще не підтверджено — продовжуйте у браузері")
        return {
            "session_id": session_id, "step": "manual",
            "message": "Вхід ще не підтверджено. Продовжуйте у браузері.",
        }
    state = await _state(session.context)
    _set_progress(session_id, "saving_session", "Авторизацію підтверджено — зберігаємо сесію")
    await close_login(session_id, keep_progress=True)
    return {"session_id": session_id, "step": "done", "storage_state": state}


async def request_manual_takeover(account_id: str, session_id: str) -> dict:
    progress = _progress.get(session_id)
    if progress is None or progress.account_id != account_id:
        raise AdapterError("Prom.ua: сесію авторизації не знайдено")
    session = _sessions.get(session_id)
    if session is not None and session.account_id == account_id:
        session.expires_at = time.monotonic() + LOGIN_TTL
    progress.manual_requested = True
    progress.step = "manual"
    progress.message = "Керування передано вам. Продовжуйте у браузері."
    progress.expires_at = time.monotonic() + LOGIN_TTL
    return {"session_id": session_id, "step": progress.step, "message": progress.message}


# --------------------------------------------------------------------------- #
# Manual control                                                               #
# --------------------------------------------------------------------------- #
async def _control_page(account_id: str, session_id: str) -> Page:
    session = _sessions.get(session_id)
    if session is not None and session.account_id == account_id:
        session.expires_at = time.monotonic() + LOGIN_TTL
        return session.page
    progress = _progress.get(session_id)
    if progress is None or progress.account_id != account_id or progress.page is None:
        raise AdapterError("Prom.ua: браузер зараз недоступний")
    progress.expires_at = time.monotonic() + LOGIN_TTL
    return progress.page


def _validate_pointer(x: float, y: float) -> None:
    if not 0 <= x <= VIEWPORT["width"] or not 0 <= y <= VIEWPORT["height"]:
        raise AdapterError("Prom.ua: координати вказівника поза межами браузера")


async def control_login_click(account_id: str, session_id: str, x: float, y: float) -> None:
    page = await _control_page(account_id, session_id)
    _validate_pointer(x, y)
    await page.mouse.click(x, y)
    _set_progress(session_id, "manual", f"Ручне керування: клік ({round(x)}, {round(y)})")


async def control_login_pointer(
    account_id: str, session_id: str, action: str, x: float, y: float,
) -> None:
    page = await _control_page(account_id, session_id)
    _validate_pointer(x, y)
    if action == "down":
        await page.mouse.move(x, y)
        await page.mouse.down(button="left")
    elif action == "move":
        await page.mouse.move(x, y)
    elif action == "up":
        await page.mouse.move(x, y)
        await page.mouse.up(button="left")
    else:
        raise AdapterError("Prom.ua: невідома дія вказівника")


async def control_login_key(
    account_id: str, session_id: str, key: str,
    *, alt: bool = False, ctrl: bool = False, meta: bool = False, shift: bool = False,
) -> None:
    page = await _control_page(account_id, session_id)
    if len(key) == 1 and not any((alt, ctrl, meta)):
        await page.keyboard.insert_text(key.upper() if shift else key)
        return
    allowed = {
        "Enter", "Tab", "Escape", "Backspace", "Delete", "Space",
        "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown",
    }
    if len(key) != 1 and key not in allowed:
        raise AdapterError("Prom.ua: ця клавіша не підтримується")
    modifiers = [name for enabled, name in (
        (ctrl, "Control"), (alt, "Alt"), (shift, "Shift"), (meta, "Meta"),
    ) if enabled]
    await page.keyboard.press("+".join([*modifiers, key]))


async def control_login_text(account_id: str, session_id: str, value: str) -> None:
    if not value or len(value) > 500:
        raise AdapterError("Prom.ua: текст для вводу порожній або надто довгий")
    page = await _control_page(account_id, session_id)
    await page.keyboard.type(value, delay=80)
    _set_progress(session_id, "manual", "Ручне керування: текст введено")


async def get_login_screenshot(account_id: str, session_id: str) -> bytes:
    progress = _progress.get(session_id)
    if progress is None or progress.account_id != account_id or progress.page is None:
        raise AdapterError("Prom.ua: браузер зараз недоступний")
    try:
        return await progress.page.screenshot(type="jpeg", quality=65)
    except Exception as exc:
        raise AdapterError("Prom.ua: не вдалося отримати зображення браузера") from exc
