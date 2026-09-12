"""One-time AliExpress phone/password login in a Docker-hosted Patchright session."""
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

ORDERS_URL = "https://www.aliexpress.com/p/order/index.html"
HOME_URL = "https://www.aliexpress.com/"
LOGIN_TTL = 10 * 60


@dataclass
class LoginSession:
    id: str
    account_id: str
    expires_at: float
    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.context.close()
        with contextlib.suppress(Exception):
            await self.browser.close()
        with contextlib.suppress(Exception):
            await self.playwright.stop()


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


async def start_login(account_id: str, login: str, password: str, session_id: str | None = None) -> dict:
    if not login.strip() or not password:
        raise AdapterError("AliExpress: вкажіть телефон/email та пароль")
    await _cleanup()
    session_id = session_id or secrets.token_urlsafe(24)
    _progress[session_id] = LoginProgress(
        account_id, "starting", "Запускаємо Chromium у контейнері", time.monotonic() + LOGIN_TTL,
    )
    playwright = await async_playwright().start()
    browser = None
    context = None
    page = None
    saved = False
    try:
        _set_progress(session_id, "opening_site", "Відкриваємо головну сторінку AliExpress")
        browser = await playwright.chromium.launch(
            headless=False,
            channel="chrome",
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            locale="en-US", viewport={"width": 1400, "height": 1000},
        )
        page = await context.new_page()
        _progress[session_id].page = page
        await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(2_000)
        _raise_if_manual_requested(session_id)

        _set_progress(session_id, "opening_account_menu", "Відкриваємо меню акаунта")
        login_input = await _open_login_form(page, session_id)
        _raise_if_manual_requested(session_id)
        if login_input is None:
            if await _is_logged_in(page):
                _set_progress(session_id, "saving_session", "Авторизацію підтверджено — зберігаємо сесію")
                return {"session_id": session_id, "step": "done", "storage_state": await _state(context)}
            raise AdapterError("AliExpress: не вдалося відкрити форму входу")

        # The current form is two-stage: login -> Continue -> password.  For a
        # Ukrainian phone AliExpress renders +380 as a separate country-code
        # control, so only the national part belongs in the input.
        _set_progress(session_id, "entering_login", "Вводимо телефон або email")
        await login_input.fill(_login_field_value(login))
        _raise_if_manual_requested(session_id)
        _set_progress(session_id, "submitting_login", "Натискаємо Continue")
        await _click_named_button(page, r"continue|продовжити|продолжить")
        _set_progress(session_id, "waiting_password", "Очікуємо поле пароля")
        password_input = await _wait_for_password(page, session_id)
        _raise_if_manual_requested(session_id)
        if password_input is None:
            if await _captcha(page):
                raise AdapterError("AliExpress показав CAPTCHA. Автоматичний вхід зупинено без спроби обходу.")
            raise AdapterError(await _login_error(page) or "AliExpress: після Continue не з'явилося поле пароля")

        _set_progress(session_id, "entering_password", "Вводимо пароль")
        await password_input.fill(password)
        _raise_if_manual_requested(session_id)
        _set_progress(session_id, "submitting_password", "Надсилаємо форму входу")
        if not await _click_named_button(page, r"sign in|увійти|войти"):
            await password_input.press("Enter")
        manual_control = await _recover_after_password_submit(page, session_id)
        if manual_control:
            await _save_login_session(
                LoginSession(
                    session_id, account_id, time.monotonic() + LOGIN_TTL,
                    playwright, browser, context, page,
                )
            )
            saved = True
            _set_progress(
                session_id,
                "manual",
                "Автоматичне відновлення не спрацювало — керуйте браузером вручну",
            )
            return {
                "session_id": session_id,
                "step": "manual",
                "message": "Керуйте браузером у панелі, потім натисніть «Перевірити вхід».",
            }

        _set_progress(session_id, "checking_login", "Перевіряємо авторизацію та відкриваємо замовлення")
        if await _captcha(page):
            raise AdapterError("AliExpress показав CAPTCHA. Автоматичний вхід зупинено без спроби обходу.")
        if await _is_logged_in(page):
            _set_progress(session_id, "saving_session", "Авторизацію підтверджено — зберігаємо сесію")
            return {"session_id": session_id, "step": "done", "storage_state": await _state(context)}
        if await _otp_input(page) is not None:
            session = LoginSession(
                session_id, account_id, time.monotonic() + LOGIN_TTL,
                playwright, browser, context, page,
            )
            await _save_login_session(session)
            saved = True
            _set_progress(session_id, "otp", "AliExpress надіслав код підтвердження")
            return {
                "session_id": session_id, "step": "otp",
                "message": "AliExpress надіслав код підтвердження. Введіть його нижче.",
            }
        error = await _login_error(page)
        raise AdapterError(error or "AliExpress: вхід не підтверджено")
    except ManualTakeover:
        if browser is None or context is None or page is None:
            raise AdapterError("AliExpress: браузер ще не готовий до ручного керування")
        await _save_login_session(
            LoginSession(
                session_id, account_id, time.monotonic() + LOGIN_TTL,
                playwright, browser, context, page,
            )
        )
        saved = True
        _set_progress(session_id, "manual", "Керування передано користувачу")
        return {
            "session_id": session_id, "step": "manual",
            "message": "Керування передано вам. Продовжуйте у браузері.",
        }
    except AdapterError as exc:
        _set_progress(session_id, "error", str(exc))
        raise
    except Exception as exc:
        if type(exc).__name__ == "TargetClosedError":
            message = (
                "AliExpress: Chromium несподівано закрився. "
                "Сесію очищено — почніть вхід ще раз."
            )
        else:
            message = f"AliExpress: помилка входу ({type(exc).__name__})"
        _set_progress(session_id, "error", message)
        raise AdapterError(message) from exc
    finally:
        # Password is never retained. The browser itself is kept only for OTP.
        password = ""
        if not saved:
            if session_id in _progress:
                _progress[session_id].page = None
            if context is not None:
                await context.close()
            if browser is not None:
                await browser.close()
            await playwright.stop()


async def submit_otp(account_id: str, session_id: str, code: str) -> dict:
    session = await _get(account_id, session_id)
    session.expires_at = time.monotonic() + LOGIN_TTL
    _set_progress(session_id, "submitting_otp", "Надсилаємо код підтвердження")
    otp = await _otp_input(session.page)
    if otp is None:
        if await _is_logged_in(session.page):
            state = await _state(session.context)
            await close_login(session_id)
            return {"session_id": session_id, "step": "done", "storage_state": state}
        raise AdapterError("AliExpress: поле коду більше не доступне — почніть вхід знову")
    await otp.fill(re.sub(r"\s+", "", code))
    submit = await _first_visible(session.page, ['button[type="submit"]', '.fm-button'])
    if submit is not None:
        await submit.click()
    else:
        await otp.press("Enter")
    await session.page.wait_for_timeout(3_000)
    if await _captcha(session.page):
        raise AdapterError("AliExpress показав CAPTCHA. Автоматичний вхід зупинено.")
    if not await _is_logged_in(session.page):
        raise AdapterError(await _login_error(session.page) or "AliExpress: код не прийнято")
    state = await _state(session.context)
    _set_progress(session_id, "saving_session", "Авторизацію підтверджено — зберігаємо сесію")
    await close_login(session_id, keep_progress=True)
    return {"session_id": session_id, "step": "done", "storage_state": state}


async def close_login(session_id: str, *, keep_progress: bool = False) -> None:
    async with _lock:
        session = _sessions.pop(session_id, None)
    if session is not None:
        await session.close()
    if not keep_progress:
        _progress.pop(session_id, None)


async def _save_login_session(session: LoginSession) -> None:
    async with _lock:
        old = [sid for sid, item in _sessions.items() if item.account_id == session.account_id]
        previous = [_sessions.pop(sid) for sid in old]
        _sessions[session.id] = session
    for item in previous:
        await item.close()


async def control_login_click(account_id: str, session_id: str, x: float, y: float) -> None:
    page = await _get_control_page(account_id, session_id)
    _validate_pointer_coordinates(x, y)
    await page.mouse.click(x, y)
    _set_progress(session_id, "manual", f"Ручне керування: клік ({round(x)}, {round(y)})")


async def control_login_pointer(
    account_id: str, session_id: str, action: str, x: float, y: float,
) -> None:
    page = await _get_control_page(account_id, session_id)
    _validate_pointer_coordinates(x, y)
    if action == "down":
        await page.mouse.move(x, y)
        await page.mouse.down(button="left")
        _set_progress(session_id, "manual", "Ручне керування: кнопку миші затиснуто")
    elif action == "move":
        await page.mouse.move(x, y)
    elif action == "up":
        await page.mouse.move(x, y)
        await page.mouse.up(button="left")
        _set_progress(session_id, "manual", "Ручне керування: перетягування завершено")
    else:
        raise AdapterError("AliExpress: невідома дія вказівника")


def _validate_pointer_coordinates(x: float, y: float) -> None:
    if not 0 <= x <= 1400 or not 0 <= y <= 1000:
        raise AdapterError("AliExpress: координати вказівника поза межами браузера")


async def control_login_key(
    account_id: str, session_id: str, key: str,
    *, alt: bool = False, ctrl: bool = False, meta: bool = False, shift: bool = False,
) -> None:
    page = await _get_control_page(account_id, session_id)
    if len(key) == 1 and not any((alt, ctrl, meta)):
        await page.keyboard.insert_text(key.upper() if shift else key)
    elif len(key) == 1:
        modifiers = [name for enabled, name in (
            (ctrl, "Control"), (alt, "Alt"), (shift, "Shift"), (meta, "Meta"),
        ) if enabled]
        await page.keyboard.press("+".join([*modifiers, key]))
    else:
        allowed = {
            "Enter", "Tab", "Escape", "Backspace", "Delete", "Space",
            "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End",
            "PageUp", "PageDown",
        }
        if key not in allowed:
            raise AdapterError("AliExpress: ця клавіша не підтримується")
        modifiers = [name for enabled, name in (
            (ctrl, "Control"), (alt, "Alt"), (shift, "Shift"), (meta, "Meta"),
        ) if enabled]
        await page.keyboard.press("+".join([*modifiers, key]))
    _set_progress(session_id, "manual", f"Ручне керування: клавіша {key}")


async def request_manual_takeover(account_id: str, session_id: str) -> dict:
    progress = _progress.get(session_id)
    if progress is None or progress.account_id != account_id:
        raise AdapterError("AliExpress: сесію авторизації не знайдено")
    session = _sessions.get(session_id)
    if session is not None and session.account_id == account_id:
        # OTP and fallback sessions are already detached from start_login(),
        # so there is no automation loop left to observe manual_requested.
        # They can become interactive immediately.
        session.expires_at = time.monotonic() + LOGIN_TTL
        progress.manual_requested = True
        progress.step = "manual"
        progress.message = "Керування передано користувачу"
        progress.expires_at = time.monotonic() + LOGIN_TTL
        return {"session_id": session_id, "step": progress.step, "message": progress.message}
    progress.manual_requested = True
    progress.step = "manual_requested"
    progress.message = "Зупиняємо автоматизацію та передаємо керування"
    progress.expires_at = time.monotonic() + LOGIN_TTL
    return {"session_id": session_id, "step": progress.step, "message": progress.message}


async def control_login_text(account_id: str, session_id: str, value: str) -> None:
    if not value or len(value) > 500:
        raise AdapterError("AliExpress: текст для вводу порожній або надто довгий")
    page = await _get_control_page(account_id, session_id)
    await page.keyboard.type(value, delay=100)
    _set_progress(session_id, "manual", "Ручне керування: текст введено")


async def _get_control_page(account_id: str, session_id: str) -> Page:
    session = _sessions.get(session_id)
    if session is not None and session.account_id == account_id:
        session.expires_at = time.monotonic() + LOGIN_TTL
        return session.page
    progress = _progress.get(session_id)
    if progress is None or progress.account_id != account_id or progress.page is None:
        raise AdapterError("AliExpress: браузер зараз недоступний")
    progress.expires_at = time.monotonic() + LOGIN_TTL
    return progress.page


async def check_manual_login(account_id: str, session_id: str) -> dict:
    session = await _get(account_id, session_id)
    _set_progress(session_id, "checking_login", "Перевіряємо результат ручного входу")
    if await _captcha(session.page):
        return {
            "session_id": session_id, "step": "manual",
            "message": "Завершіть перевірку AliExpress у браузері та перевірте ще раз.",
        }
    if await _otp_input(session.page) is not None:
        _set_progress(session_id, "otp", "AliExpress надіслав код підтвердження")
        return {
            "session_id": session_id, "step": "otp",
            "message": "AliExpress надіслав код підтвердження. Введіть його нижче.",
        }
    if not await _is_logged_in(session.page):
        _set_progress(session_id, "manual", "Вхід ще не підтверджено — продовжуйте у браузері")
        return {
            "session_id": session_id, "step": "manual",
            "message": "Вхід ще не підтверджено. Продовжуйте у браузері.",
        }
    state = await _state(session.context)
    _set_progress(session_id, "saving_session", "Авторизацію підтверджено — зберігаємо сесію")
    await close_login(session_id, keep_progress=True)
    return {"session_id": session_id, "step": "done", "storage_state": state}


def get_login_progress(account_id: str, session_id: str) -> dict:
    progress = _progress.get(session_id)
    if progress is None or progress.account_id != account_id:
        raise AdapterError("AliExpress: сесію авторизації не знайдено")
    return {"session_id": session_id, "step": progress.step, "message": progress.message}


def update_login_progress(session_id: str | None, step: str, message: str) -> None:
    if session_id:
        _set_progress(session_id, step, message)


async def get_login_screenshot(account_id: str, session_id: str) -> bytes:
    progress = _progress.get(session_id)
    if progress is None or progress.account_id != account_id or progress.page is None:
        raise AdapterError("AliExpress: браузер зараз недоступний")
    try:
        return await progress.page.screenshot(type="jpeg", quality=65)
    except Exception as exc:
        raise AdapterError("AliExpress: не вдалося отримати зображення браузера") from exc


def _set_progress(session_id: str, step: str, message: str) -> None:
    progress = _progress.get(session_id)
    if progress is not None:
        if not progress.manual_requested or step in {
            "manual", "saving_session", "importing_orders", "done", "error",
        }:
            progress.step = step
            progress.message = message
        progress.expires_at = time.monotonic() + LOGIN_TTL


def _raise_if_manual_requested(session_id: str | None) -> None:
    if session_id and _progress.get(session_id) and _progress[session_id].manual_requested:
        raise ManualTakeover


async def _recover_after_password_submit(page: Page, session_id: str) -> bool:
    """Wait for the login result and recover widget network failures."""
    retries = 0
    for _ in range(60):
        _raise_if_manual_requested(session_id)
        if retries < 5 and await _recover_login_network_error(page):
            retries += 1
            _set_progress(
                session_id,
                "retrying_login_submit",
                f"Помилка мережі після входу — повторюємо завантаження ({retries}/5)",
            )
            await page.wait_for_timeout(1_500)
            if retries >= 5:
                return True
            continue

        if await _otp_input(page) is not None:
            return False
        login_dialog = page.locator("#batman-dialog-wrap")
        with contextlib.suppress(Exception):
            if not await login_dialog.count() or not await login_dialog.first.is_visible():
                return False
        await page.wait_for_timeout(500)
    return False


async def _get(account_id: str, session_id: str) -> LoginSession:
    await _cleanup()
    async with _lock:
        session = _sessions.get(session_id)
    if session is None or session.account_id != account_id:
        raise AdapterError("AliExpress: сесія входу завершилась — почніть знову")
    return session


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


async def _first_visible(page: Page, selectors: list[str]):
    for root in _page_roots(page):
        for selector in selectors:
            try:
                locator = root.locator(selector)
                if await locator.count() and await locator.first.is_visible():
                    return locator.first
            except Exception:
                # Ad and tracking iframes are short-lived and may detach while
                # the login bundle is loading.
                continue
    return None


def _page_roots(page: Page):
    """Yield the top document and login iframes used by the current site."""
    yield page
    for frame in page.frames:
        if frame != page.main_frame:
            yield frame


async def _login_input(page: Page):
    return await _first_visible(page, [
        '#batman-dialog-wrap input:not([type="hidden"]):not([type="password"])',
        'input[placeholder*="Email or phone" i]',
        '#fm-login-id', 'input[name="loginId"]', 'input[name="email"]',
        'input[type="email"]', 'input[type="tel"]',
    ])


async def _open_login_form(page: Page, session_id: str | None = None):
    current = await _login_input(page)
    if current is not None:
        return current

    # The account header either opens the form directly or first opens a menu
    # with a second Sign in button. Ads may visually cover these controls, so
    # force is intentional: it targets the semantic control underneath without
    # depending on short-lived promotion CSS classes.
    account_targets = [
        page.get_by_text(re.compile(r"sign in\s*/\s*register", re.I)),
        page.get_by_role("button", name=re.compile(r"sign in\s*/\s*register", re.I)),
        page.get_by_role("link", name=re.compile(r"sign in\s*/\s*register", re.I)),
    ]
    for target in account_targets:
        if await target.count() and await target.first.is_visible():
            with contextlib.suppress(Exception):
                await target.first.hover(timeout=5_000)
                await page.wait_for_timeout(500)
                if session_id:
                    _set_progress(session_id, "opening_login", "Натискаємо Sign in")
                if await _click_named_button(page, r"^sign in$|^увійти$|^войти$"):
                    return await _wait_for_login_input(page, session_id)
            # Some variants open the account menu on hover, while others open
            # the login modal on click.
            with contextlib.suppress(Exception):
                await target.first.click(force=True, timeout=5_000)
            return await _wait_for_login_input(page, session_id)
    return None


async def _wait_for_login_input(page: Page, session_id: str | None = None):
    # AliExpress first displays an empty modal with a spinner. In Docker its
    # authentication bundle can take tens of seconds to initialize.
    network_retries = 0
    for _ in range(90):
        _raise_if_manual_requested(session_id)
        if session_id:
            _set_progress(session_id, "loading_login_form", "Чекаємо завантаження форми AliExpress")
        dismissed = await _dismiss_promotions(page)
        if dismissed and session_id:
            _set_progress(session_id, "closing_ad", "Закриваємо рекламне вікно поверх форми")
        switched = await _switch_from_registration(page)
        if switched and session_id:
            _set_progress(
                session_id,
                "switching_to_login",
                "AliExpress показав реєстрацію — перемикаємося на вхід",
            )
        if network_retries < 5 and await _recover_login_network_error(page):
            network_retries += 1
            if session_id:
                _set_progress(
                    session_id,
                    "retrying_login_form",
                    f"Помилка мережі у формі — повторюємо завантаження ({network_retries}/5)",
                )
            await page.wait_for_timeout(1_000)
        current = await _login_input(page)
        if current is not None:
            return current
        await page.wait_for_timeout(500)
    return None


async def _recover_login_network_error(page: Page) -> bool:
    """Use the login widget's own reload action; never loop indefinitely."""
    pattern = re.compile(r"network\s+error.*click\s+to\s+reload", re.I)
    for root in _page_roots(page):
        try:
            error = root.get_by_text(pattern, exact=False)
            if not await error.count() or not await error.first.is_visible():
                continue
            if await _click_login_reload_control(page, root, error.first):
                return True
        except Exception:
            # Short-lived ad frames can disappear during inspection.
            continue
    return False


async def _click_login_reload_control(page: Page, root, error) -> bool:
    """Click the reload glyph below the network-error message, not the text."""
    error_box = await error.bounding_box()
    if error_box is None:
        return False

    selector = (
        'button, [role="button"], [class*="reload" i], [class*="refresh" i], '
        '[class*="retry" i], svg, img'
    )
    ranked: list[tuple[float, object]] = []
    for candidate in await root.locator(selector).all():
        try:
            if not await candidate.is_visible():
                continue
            box = await candidate.bounding_box()
            if box is None:
                continue
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2
            error_cx = error_box["x"] + error_box["width"] / 2
            error_bottom = error_box["y"] + error_box["height"]
            dx = abs(cx - error_cx)
            dy = cy - error_bottom
            # In the observed widget the arrow is centred directly below the
            # message. This excludes the close icon above and QR code below.
            if dx <= 140 and 0 <= dy <= 130:
                ranked.append((dy + dx * 0.25, candidate))
        except Exception:
            continue

    for _, candidate in sorted(ranked, key=lambda item: item[0]):
        with contextlib.suppress(Exception):
            await candidate.click(timeout=5_000)
            return True

    # CSS-only fallback: click the visual arrow position relative to the
    # detected message. Bounding boxes are reported in top-page coordinates
    # even when the text belongs to a child frame.
    with contextlib.suppress(Exception):
        await page.mouse.click(
            error_box["x"] + error_box["width"] / 2,
            error_box["y"] + error_box["height"] + 48,
        )
        return True
    return False


async def _switch_from_registration(page: Page) -> bool:
    """Handle AliExpress's A/B variant that opens passkey registration first."""
    registration = page.get_by_text(re.compile(r"register by passkey", re.I), exact=True)
    if not await registration.count() or not await registration.first.is_visible():
        return False
    existing_account = page.get_by_text(
        re.compile(r"already have an account\??", re.I), exact=True,
    )
    if not await existing_account.count() or not await existing_account.first.is_visible():
        return False
    with contextlib.suppress(Exception):
        await existing_account.first.click(timeout=8_000)
        await page.wait_for_timeout(500)
        return True
    return False


async def _dismiss_promotions(page: Page) -> bool:
    # New-user promotions are rendered above the already loaded login dialog.
    # Scope the selector to their own modal so the login dialog's close button
    # is never touched.
    for selector in (
        '.image-poplayer-modal .pop-close-btn:visible',
        '.image-poplayer-modal [class*="close"]:visible',
    ):
        close = page.locator(selector)
        if await close.count():
            with contextlib.suppress(Exception):
                await close.first.click(timeout=5_000)
                await page.wait_for_timeout(300)
            return True
    return False


async def _click_named_button(page: Page, name: str) -> bool:
    pattern = re.compile(name, re.I)
    for root in _page_roots(page):
        candidates = [
            root.get_by_role("button", name=pattern),
            root.locator('button[type="submit"]:visible'),
            root.get_by_text(pattern, exact=True),
        ]
        for candidate in candidates:
            if await candidate.count() and await candidate.first.is_visible():
                try:
                    # A current AliExpress telemetry bug calls
                    # performance.measure() before creating this mark and
                    # destroys the login modal through its error boundary.
                    with contextlib.suppress(Exception):
                        await page.evaluate("performance.mark('exp_start')")
                    await candidate.first.click(timeout=8_000)
                    return True
                except Exception:
                    continue
    return False


async def _wait_for_password(page: Page, session_id: str | None = None):
    selectors = ['#fm-login-password', 'input[name="password"]', 'input[type="password"]']
    for _ in range(40):
        _raise_if_manual_requested(session_id)
        password = await _first_visible(page, selectors)
        if password is not None:
            return password
        await page.wait_for_timeout(500)
    return None


def _login_field_value(login: str) -> str:
    value = re.sub(r"[\s()\-]", "", login.strip())
    if "@" in value:
        return value
    # The account UI currently defaults to Ukraine and displays +380 in a
    # dedicated selector (confirmed against the current AliExpress flow).
    if value.startswith("+380"):
        return value[4:].lstrip("0")
    if value.startswith("380") and len(value) >= 12:
        return value[3:].lstrip("0")
    return value


async def _otp_input(page: Page):
    return await _first_visible(page, [
        'input[autocomplete="one-time-code"]', 'input[name*="code" i]',
        'input[id*="code" i]', 'input[inputmode="numeric"]',
    ])


async def _captcha(page: Page) -> bool:
    for root in _page_roots(page):
        with contextlib.suppress(Exception):
            text = (await root.locator("body").inner_text(timeout=5_000)).lower()
            if any(word in text for word in ("captcha", "verify you are human", "проверка безопасности")):
                return True
    return False


async def _is_logged_in(page: Page) -> bool:
    if any(part in page.url.lower() for part in ("login", "signin")):
        return False
    if await _first_visible(page, ['input[type="password"]']) is not None:
        return False
    if "/order/" not in page.url.lower():
        with contextlib.suppress(Exception):
            await page.goto(ORDERS_URL, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(1_500)
    return "/order/" in page.url.lower() and not any(
        part in page.url.lower() for part in ("login", "signin")
    )


async def _login_error(page: Page) -> str | None:
    for root in _page_roots(page):
        for selector in ('.fm-error-tip', '[role="alert"]', '.error-message'):
            locator = root.locator(selector)
            if await locator.count() and await locator.first.is_visible():
                text = (await locator.first.inner_text()).strip()
                if text:
                    return f"AliExpress: {text[:300]}"
    return None


async def _state(context: BrowserContext) -> str:
    return json.dumps(await context.storage_state(), separators=(",", ":"))
