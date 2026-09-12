#!/usr/bin/env python3
"""Drives the auth.silpo.ua browser login (phone + SMS code — the exact "Власний
Рахунок" flow the Silpo app itself uses) for a silpo (oauth_browser) account,
instead of doing it by hand in a browser each time a token needs (re-)issuing.

This is NOT a bypass of anything: it types the account owner's own phone number
and the real OTP code they receive by SMS into Silpo's own login form, the same
way a person would — no anti-bot challenge is being defeated, and a human still
has to be at the keyboard to supply the SMS code when prompted. Compare with
adapters/atb.py's parse_import — this exists for the same reason (Silpo's login
happens on a real page in a browser, not an API call we can script directly), not
as a workaround for a blocked automated path.

Usage (run from backend/, with the dev extras installed — see requirements-dev.txt):
    ./venv/bin/python3 scripts/silpo_login.py "Сільпо Максим"   # matches by label...
    ./venv/bin/python3 scripts/silpo_login.py <account-id>       # ...or by id

Runs headless by default (no window pops up) — needed to run unattended and inside
Docker, where there's no display server at all. Only stdin/stdout are interactive
(prompts for the SMS code in the terminal); the browser itself is invisible. This
was verified end-to-end headless, including the Cloudflare Turnstile check behind
the consent button — see RESEARCH.md.

Env:
    RS_API_BASE         backend API to talk to (default: http://localhost:8001)
    SILPO_LOGIN_HEADLESS=0   show the browser window (debugging only — needs a
                             display, won't work in Docker/over SSH without X11)
"""
from __future__ import annotations

import os
import sys
import time

import httpx
from patchright.sync_api import Page, sync_playwright

API_BASE = os.environ.get("RS_API_BASE", "http://localhost:8001")
HEADLESS = os.environ.get("SILPO_LOGIN_HEADLESS") != "0"
TIMEOUT_S = 120


def find_account(client: httpx.Client, needle: str) -> dict:
    accounts = client.get("/api/accounts").json()
    for a in accounts:
        if a["id"] == needle or a["label"] == needle:
            if a["store"]["code"] != "silpo":
                raise SystemExit(f"'{needle}' — це {a['store']['code']}, не silpo")
            return a
    raise SystemExit(f"Не знайдено акаунт Сільпо '{needle}'")


def _body_text(page: Page) -> str:
    try:
        return page.inner_text("body")
    except Exception:
        return ""


def run_login(page: Page, phone: str, callback_prefix: str) -> bool:
    """Polls the page's visible state and reacts — a state machine rather than a
    fixed script, because Silpo may skip straight to consent (session already
    remembered) or straight past consent (already granted before)."""
    otp_filled = False
    # TIMEOUT_S bounds each *automated* step (submit → next screen, click → redirect),
    # not how long a human takes to type something — reset it right after every
    # input() call returns, otherwise a slow SMS-code entry (or, when testing, relaying
    # it through a person one step removed from the terminal) burns the same budget
    # meant for the click-retry loop and reports a false failure even though the
    # actual login goes on to succeed.
    deadline = time.monotonic() + TIMEOUT_S

    while time.monotonic() < deadline:
        if page.url.startswith(callback_prefix):
            return True

        text = _body_text(page).lower()

        if "номер телефону" in text and "sms" not in text:
            box = page.get_by_role("textbox").first
            box.click()
            box.fill(phone)
            page.get_by_role("button", name="Увійти").click()
            page.wait_for_timeout(1500)
            deadline = time.monotonic() + TIMEOUT_S

        elif "код із sms" in text or "код відправлено" in text:
            if not otp_filled:
                code = input("Код із SMS: ").strip()
                deadline = time.monotonic() + TIMEOUT_S
                box = page.get_by_role("textbox").first
                box.click()
                box.fill(code)
                otp_filled = True
                page.wait_for_timeout(500)
            btn = page.get_by_role("button", name="Продовжити")
            if btn.is_visible():
                btn.click()
            page.wait_for_timeout(1500)

        elif "увійдіть до" in text and "продовжити" in text:
            # The consent button sits behind an invisible Cloudflare Turnstile
            # check that isn't always wired up the instant it renders — clicking
            # too early is a silent no-op, so this just keeps retrying.
            btn = page.get_by_role("button", name="Продовжити")
            if btn.is_visible():
                btn.click()
            page.wait_for_timeout(2000)

        else:
            page.wait_for_timeout(1000)

    return page.url.startswith(callback_prefix)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    needle = sys.argv[1]

    with httpx.Client(base_url=API_BASE, timeout=20) as client:
        account = find_account(client, needle)
        phone = account.get("phone_number")
        if not phone:
            phone = input("Номер телефону (+380...): ").strip()

        start = client.get(f"/api/accounts/{account['id']}/oauth/start").json()
        authorize_url = start["authorize_url"]
        # redirect_uri (our own backend's callback, e.g. http://192.168.1.50:8001/...)
        # is embedded in authorize_url — pull settings.base_url out of it rather than
        # importing app.core.config, so this script has no import-time dependency on
        # the app package.
        import urllib.parse

        redirect_uri = urllib.parse.parse_qs(urllib.parse.urlparse(authorize_url).query)["redirect_uri"][0]
        callback_prefix = redirect_uri.rsplit("/api/", 1)[0]

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=HEADLESS, channel="chrome")
            page = browser.new_page()
            page.goto(authorize_url)
            ok = run_login(page, phone, callback_prefix)
            browser.close()

        # The callback is processed server-side by app/api/oauth.py the moment the
        # browser navigates there — that can land a beat before run_login's own poll
        # notices it, so a local "timed out" isn't necessarily a real failure. Check
        # the account itself before giving up.
        updated = find_account(client, account["id"])
        if not ok and updated["status"] != "active":
            raise SystemExit("Не вдалось завершити вхід за відведений час — спробуйте ще раз")

        if updated["status"] == "active":
            print(f"✅ {updated['label']}: вхід виконано")
        else:
            raise SystemExit(f"Статус після спроби: {updated['status']} ({updated.get('last_error')})")


if __name__ == "__main__":
    main()
