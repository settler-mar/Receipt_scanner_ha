#!/usr/bin/env python3
"""Drives a real browser login to dnipro-m.ua for a dnipro-m account and hands
the resulting session to the backend, instead of doing it by hand and pasting a
storage-state blob into the UI.

Why a script and not an in-app OTP flow (like ATB/Fora):

  * dnipro-m.ua has no mobile API — the personal cabinet is a plain Yii2 site
    (PHP session cookie + CSRF). The only "token" is the logged-in cookie jar.
  * `/phone-verification/` is behind reCAPTCHA, and a *headless* Patchright
    Chromium gets flagged as automation at that step ("Не вдалося підтвердити,
    що запит виконано людиною") — see RESEARCH.md. We deliberately don't try to
    defeat that.
  * A real, headed browser window where the account owner logs in themselves —
    by Google ("Увійти через Google") or phone+SMS, whichever — is not
    automation of anything: a person clicks through their own login. This script
    just opens that window, waits for it to finish, and copies the cookie jar
    Patchright already has into the backend.

The captured cookie jar (Patchright storage-state JSON) is exactly what
`DniproMAdapter._browser_fetch` replays through its own headless Chromium for
`fetch_receipts` / `get_balance` / `get_warranty_document`. Step 2 below relaunches
a *fresh* headless context with the captured state and re-checks the session, so a
"session doesn't survive the handoff" problem surfaces here and not on the first
scheduled collection.

Usage (run from backend/, on a machine with a display):
    ./venv/bin/python3 scripts/dnipro_m_login.py "Дніпро-М Максим"   # by label...
    ./venv/bin/python3 scripts/dnipro_m_login.py <account-id>         # ...or by id

Env:
    RS_API_BASE                backend API base (default: http://localhost:8001)
    DNIPRO_LOGIN_HEADLESS=1    run the login window headless too (NOT recommended
                               — the site flags headless Chromium as a bot; only
                               useful if you've already got a session cookie in
                               the profile and just want to re-capture it)
    DNIPRO_LOGIN_TIMEOUT       seconds to wait for the login to complete (default 600)
"""
from __future__ import annotations

import json
import os
import sys
import time

import httpx
from patchright.sync_api import sync_playwright

API_BASE = os.environ.get("RS_API_BASE", "http://localhost:8001")
LOGIN_HEADLESS = os.environ.get("DNIPRO_LOGIN_HEADLESS") == "1"
LOGIN_TIMEOUT_S = int(os.environ.get("DNIPRO_LOGIN_TIMEOUT", "600"))

BASE_URL = "https://dnipro-m.ua"
LOGIN_URL = f"{BASE_URL}/login/"
# Lightweight "am I logged in?" endpoint — returns {"auth": true, "user_data": {…}}
# for a live session, {"auth": false} otherwise. Cheaper and less ambiguous than
# GET-ing the order history (which 302s to the login page when logged out).
AUTH_CHECK = "/auth/auth/user-info/?user_data[]=name&user_data[]=phone"

_AUTH_CHECK_JS = """async (path) => {
  try {
    const r = await fetch(path, {credentials: 'same-origin',
      headers: {'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest'}});
    const j = await r.json();
    return j && j.auth === true ? (j.user_data || {}) : null;
  } catch (_) { return null; }
}"""


def find_account(client: httpx.Client, needle: str) -> dict:
    for account in client.get("/api/accounts").json():
        if account["id"] == needle or account["label"] == needle:
            if account["store"]["code"] != "dnipro-m":
                raise SystemExit(f"'{needle}' — це {account['store']['code']}, не dnipro-m")
            return account
    raise SystemExit(f"Не знайдено акаунт Дніпро-М '{needle}'")


def wait_for_login(page) -> dict:
    """Block until the browser page reports a live session (the user finished
    logging in), or the timeout elapses."""
    print("→ Увійдіть у вікні браузера (Google або телефон + SMS). Чекаю…")
    deadline = time.monotonic() + LOGIN_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            user_data = page.evaluate(_AUTH_CHECK_JS, AUTH_CHECK)
        except Exception:
            # Page mid-navigation (Google redirect etc.) — context destroyed.
            user_data = None
        if user_data is not None:
            return user_data
        page.wait_for_timeout(2000)
    raise SystemExit("Вхід не завершено за відведений час — спробуйте ще раз")


def verify_state_survives(state: dict) -> dict | None:
    """Relaunch a *fresh* headless context with the captured cookie jar and
    re-check the session — the same thing the backend does on every collection.
    Returns the user_data dict if the session survived, None otherwise."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="chrome")
        context = browser.new_context(storage_state=state, locale="uk-UA")
        page = context.new_page()
        try:
            page.goto(f"{BASE_URL}/ru/", wait_until="domcontentloaded", timeout=30_000)
            return page.evaluate(_AUTH_CHECK_JS, AUTH_CHECK)
        finally:
            context.close()
            browser.close()


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    needle = sys.argv[1]

    with httpx.Client(base_url=API_BASE, timeout=30) as client:
        account = find_account(client, needle)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=LOGIN_HEADLESS, channel="chrome")
            context = browser.new_context(locale="uk-UA")
            page = context.new_page()
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)

            user_data = wait_for_login(page)
            who = " ".join(filter(None, [user_data.get("last_name"), user_data.get("name")]))
            print(f"✓ Сесія активна: {who or user_data.get('phone') or 'невідомо'}")

            state = context.storage_state()
            context.close()
            browser.close()

        survived = verify_state_survives(state)
        if survived is None:
            raise SystemExit(
                "⚠ Захоплена сесія не пережила перенесення у свіжий headless-браузер — "
                "той самий шлях, яким користується збір. Токен НЕ збережено.\n"
                "   (Схоже, dnipro-m.ua прив'язує сесію до конкретного браузера — "
                "тоді лишається лише ручний імпорт, див. RESEARCH.md.)"
            )
        print("✓ Сесія переживає свіжий headless-браузер — збір працюватиме")

        resp = client.post(
            f"/api/accounts/{account['id']}/token",
            json={"access_token": json.dumps(state, separators=(",", ":"))},
        )
        resp.raise_for_status()
        updated = find_account(client, account["id"])
        if updated["status"] != "active":
            raise SystemExit(f"Статус після збереження: {updated['status']} ({updated.get('last_error')})")
        print(f"✅ {updated['label']}: сесію збережено, акаунт активний")


if __name__ == "__main__":
    main()
