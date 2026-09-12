#!/usr/bin/env python3
"""Browser-assisted Prom.ua buyer-order import.

This is the same flow exposed in the web panel for Prom accounts. The script is
kept as a CLI fallback for debugging.
"""
from __future__ import annotations

import os
import sys

import httpx

from app.services.prom_browser_import import fetch_prom_buyer_orders

API_BASE = os.environ.get("RS_API_BASE", "http://localhost:8001")
LOGIN_TIMEOUT_S = int(os.environ.get("PROM_LOGIN_TIMEOUT", "600"))


def find_account(client: httpx.Client, needle: str) -> dict:
    for account in client.get("/api/accounts").json():
        if account["id"] == needle or account["label"] == needle:
            if account["store"]["code"] != "prom":
                raise SystemExit(f"'{needle}' — це {account['store']['code']}, не prom")
            return account
    raise SystemExit(f"Не знайдено акаунт Prom.ua '{needle}'")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: ./venv/bin/python3 scripts/prom_buyer_import.py <prom-account-label-or-id>")

    with httpx.Client(base_url=API_BASE, timeout=30) as client:
        account = find_account(client, sys.argv[1])
        orders = fetch_prom_buyer_orders(timeout_s=LOGIN_TIMEOUT_S)
        resp = client.post(f"/api/accounts/{account['id']}/import", json={"payload": {"orders": orders}})
        resp.raise_for_status()
        run = resp.json()
        if run["status"] != "success":
            raise SystemExit(f"Імпорт завершився з помилкою: {run.get('error_message')}")
        print(f"✅ Імпортовано Prom.ua: знайдено {run['receipts_found']}, нових {run['receipts_new']}")


if __name__ == "__main__":
    main()
