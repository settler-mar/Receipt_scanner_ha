"""Live Prom.ua buyer-login and order-history probe.

Run from ``backend``::

    ./venv/bin/python prom.py

Prom can show an interactive EVO captcha, so this probe deliberately runs with
a visible Chromium window.  Complete the captcha/SMS step in that window and
press Enter in the terminal.  The persistent browser profile keeps the login,
while relevant order/GraphQL traffic is written to ``prom_capture.json``.
"""
from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path
from typing import Any

from patchright.sync_api import Locator, Page, Response, TimeoutError, sync_playwright


PHONE = ""  # Example: 0631234567. Empty means that the script will ask for it.
ORDERS_URL = "https://my.prom.ua/cabinet/user/orders"
PROFILE_DIR = Path("data/browser_profiles/prom-test")
CAPTURE_FILE = Path("prom_capture.json")


def human_type(locator: Locator, text: str) -> None:
    """Type char-by-char to avoid triggering bot checks on the login form."""
    locator.click()
    for char in text:
        locator.press(char)
        time.sleep(random.uniform(0.05, 0.15))


def first_visible(page: Page, selectors: list[str]) -> Locator | None:
    for selector in selectors:
        locator = page.locator(selector)
        for index in range(locator.count()):
            candidate = locator.nth(index)
            if candidate.is_visible():
                return candidate
    return None


def open_login(page: Page) -> None:
    print("Открываю историю заказов Prom.ua...")
    page.goto(ORDERS_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(2_000)

    if "cabinet/user/orders" in page.url:
        return

    cabinet = page.get_by_role("button", name=re.compile(r"Кабінет|Кабинет", re.I))
    try:
        cabinet.first.click(timeout=10_000)
        page.wait_for_timeout(1_500)
    except TimeoutError:
        pass


def enter_phone_if_possible(page: Page, phone: str) -> None:
    phone_input = first_visible(page, [
        'input[type="tel"]',
        'input[autocomplete="tel"]',
        'input[name*="phone" i]',
        'input[placeholder*="телефон" i]',
        'input[placeholder*="номер" i]',
    ])
    if phone_input is None:
        print("Поле телефона не найдено автоматически — введите номер в браузере.")
        return

    print(f"Ввожу телефон: {phone}")
    human_type(phone_input, phone)

    submit = first_visible(page, [
        'button:has-text("Продовжити")',
        'button:has-text("Продолжить")',
        'button:has-text("Отримати код")',
        'button:has-text("Получить код")',
        'button[type="submit"]',
    ])
    if submit is not None:
        print("Отправляю номер телефона...")
        submit.click()


def main() -> None:
    phone = PHONE.strip() or input("Телефон Prom.ua (например 0631234567): ").strip()
    if not phone:
        raise SystemExit("Телефон не введён.")

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    captured: list[dict[str, Any]] = []

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            channel="chrome",
            locale="uk-UA",
            viewport={"width": 1400, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()

        def capture(response: Response) -> None:
            url = response.url
            if not any(part in url.lower() for part in ("graphql", "cabinet", "order", "/api/")):
                return
            try:
                body = response.text()
            except Exception:
                body = "<binary>"
            captured.append({
                "url": url,
                "status": response.status,
                "method": response.request.method,
                "post_data": response.request.post_data,
                "response": body[:100_000],
            })

        page.on("response", capture)
        open_login(page)
        if "cabinet/user/orders" not in page.url:
            enter_phone_if_possible(page, phone)
            input("Пройдите captcha и введите SMS-код в браузере, затем нажмите Enter здесь...")
            page.goto(ORDERS_URL, wait_until="domcontentloaded", timeout=60_000)

        page.wait_for_timeout(5_000)
        print(f"Текущая страница: {page.url}")
        if "cabinet/user/orders" not in page.url:
            print("Вход не подтверждён: Prom снова перенаправил на авторизацию.")
        else:
            print("Вход успешен, страница заказов открыта.")

        CAPTURE_FILE.write_text(
            json.dumps(captured, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Сетевой дамп сохранён: {CAPTURE_FILE.resolve()}")
        print(f"Профиль браузера сохранён: {PROFILE_DIR.resolve()}")
        input("Нажмите Enter для закрытия браузера...")
        context.close()


if __name__ == "__main__":
    main()
