#!/usr/bin/env python3
"""Validate a monobank Personal API token and inspect statements/receipts.

The token is read from MONOBANK_TOKEN. If the variable is absent, the script
asks for it without echoing the value. Nothing is written to disk.
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
from pathlib import Path
import sys
from typing import Any
from urllib.parse import quote

import httpx

URL = "https://api.monobank.ua/personal/client-info"
STATEMENT_URL = "https://api.monobank.ua/personal/statement/{account}/{from_ts}/{to_ts}"
RECEIPT_URL = "https://api.monobank.ua/receipt/{receipt_id}"


def _mask(value: Any, *, left: int = 4, right: int = 4) -> str:
    text = str(value or "")
    if not text:
        return "—"
    if len(text) <= left + right:
        return "•" * len(text)
    return f"{text[:left]}…{text[-right:]}"


def _mask_name(value: Any) -> str:
    return " ".join(f"{part[:1]}***" for part in str(value or "").split()) or "—"


def fetch_client_info(token: str, *, timeout: float = 15.0,
                      transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    with httpx.Client(timeout=timeout, transport=transport) as client:
        response = client.get(URL, headers={"X-Token": token, "Accept": "application/json"})

    if response.status_code == 403:
        raise RuntimeError("403: токен недійсний, відкликаний або не має доступу")
    if response.status_code == 429:
        raise RuntimeError("429: перевищено ліміт; client-info можна викликати не частіше 1 разу за 60 секунд")
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = response.text.strip()[:300]
        raise RuntimeError(f"HTTP {response.status_code}: {detail or response.reason_phrase}") from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("monobank повернув не JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("неочікуваний формат відповіді")
    return data


def fetch_statement(token: str, *, account: str = "0", days: int = 30,
                    timeout: float = 15.0,
                    transport: httpx.BaseTransport | None = None) -> list[dict[str, Any]]:
    if not 1 <= days <= 31:
        raise ValueError("days має бути від 1 до 31")
    now = dt.datetime.now(dt.timezone.utc)
    from_time = now - dt.timedelta(days=days)
    url = STATEMENT_URL.format(
        account=account,
        from_ts=int(from_time.timestamp()),
        to_ts=int(now.timestamp()),
    )
    with httpx.Client(timeout=timeout, transport=transport) as client:
        response = client.get(url, headers={"X-Token": token, "Accept": "application/json"})

    if response.status_code == 403:
        raise RuntimeError("403: токен недійсний або немає доступу до виписки")
    if response.status_code == 429:
        raise RuntimeError("429: перевищено ліміт; виписку можна запитувати не частіше 1 разу за 60 секунд")
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = response.text.strip()[:300]
        raise RuntimeError(f"HTTP {response.status_code}: {detail or response.reason_phrase}") from exc
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("monobank повернув не JSON для виписки") from exc
    if not isinstance(data, list):
        raise RuntimeError("неочікуваний формат виписки")
    return [row for row in data if isinstance(row, dict)]


def fetch_receipt(token: str, receipt_id: str, *, timeout: float = 15.0,
                  transport: httpx.BaseTransport | None = None) -> httpx.Response:
    """Probe monobank's undocumented receipt endpoint.

    The Personal API documentation exposes receiptId in statement rows but does
    not document a receipt download method. Keep the raw response because Mono
    may return XML, JSON, HTML, an image, or a PDF.
    """
    receipt_id = receipt_id.strip()
    if not receipt_id:
        raise ValueError("receipt-id не може бути порожнім")
    url = RECEIPT_URL.format(receipt_id=quote(receipt_id, safe=""))
    with httpx.Client(timeout=timeout, transport=transport, follow_redirects=True) as client:
        response = client.get(url, headers={"X-Token": token, "Accept": "*/*"})

    if response.status_code == 403:
        raise RuntimeError(
            "403: маршрут чека не прийняв Personal API токен; "
            "ймовірно, він недоступний через публічний API"
        )
    if response.status_code == 404:
        raise RuntimeError("404: чек з таким receiptId не знайдено")
    if response.status_code == 429:
        raise RuntimeError("429: перевищено ліміт запитів monobank")
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = response.text.strip()[:300]
        raise RuntimeError(f"HTTP {response.status_code}: {detail or response.reason_phrase}") from exc
    return response


def save_receipt(response: httpx.Response, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(response.content)
    return destination.resolve()


def print_summary(data: dict[str, Any]) -> None:
    accounts = data.get("accounts") if isinstance(data.get("accounts"), list) else []
    jars = data.get("jars") if isinstance(data.get("jars"), list) else []
    managed = data.get("managedClients") if isinstance(data.get("managedClients"), list) else []

    print("✓ Токен дійсний")
    print(f"Клієнт: {_mask_name(data.get('name'))}")
    print(f"clientId: {_mask(data.get('clientId'))}")
    print(f"permissions: {data.get('permissions') or '—'}")
    print(f"Рахунків: {len(accounts)}; банок: {len(jars)}; керованих клієнтів: {len(managed)}")
    for index, account in enumerate(accounts, 1):
        if not isinstance(account, dict):
            continue
        pans = account.get("maskedPan") if isinstance(account.get("maskedPan"), list) else []
        print(
            f"  {index}. type={account.get('type') or '—'} "
            f"currency={account.get('currencyCode') or '—'} "
            f"id={_mask(account.get('id'))} iban={_mask(account.get('iban'), left=6)} "
            f"cards={','.join(map(str, pans)) or '—'}"
        )


def print_statement(rows: list[dict[str, Any]], *, days: int) -> None:
    print(f"\nВиписка за останні {days} дн. — {len(rows)} операцій")
    if not rows:
        return
    print("Дата              Сума         MCC   Опис / контрагент")
    print("-" * 100)
    for row in sorted(rows, key=lambda item: int(item.get("time") or 0), reverse=True):
        stamp = dt.datetime.fromtimestamp(int(row.get("time") or 0), tz=dt.timezone.utc).astimezone()
        amount = float(row.get("amount") or 0) / 100
        description = str(row.get("description") or row.get("counterName") or "—").replace("\n", " ")
        receipt_id = row.get("receiptId")
        suffix = f"  receiptId={receipt_id}" if receipt_id else ""
        print(f"{stamp:%Y-%m-%d %H:%M}  {amount:>10.2f} {str(row.get('mcc') or '—'):>5}   {description}{suffix}")

def main() -> int:
    parser = argparse.ArgumentParser(description="Перевірити токен monobank Personal API")
    parser.add_argument("--raw", action="store_true", help="показати повний JSON із персональними даними")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--days", type=int, default=30, choices=range(1, 32), metavar="1..31",
                        help="період виписки в днях (типово: 30)")
    parser.add_argument("--account", default="0",
                        help="ID рахунку з client-info; 0 — основний рахунок")
    parser.add_argument("--receipt-id",
                        help="перевірити недокументований маршрут чека за receiptId")
    parser.add_argument("--receipt-output", type=Path,
                        help="файл для сирої відповіді чека (обов'язково з --receipt-id)")
    args = parser.parse_args()

    if args.receipt_output and not args.receipt_id:
        parser.error("--receipt-output потребує --receipt-id")

    token = os.getenv("MONOBANK_TOKEN", "").strip()
    if not token:
        token = getpass.getpass("MONOBANK_TOKEN: ").strip()
    if not token:
        print("Помилка: токен не задано", file=sys.stderr)
        return 2

    try:
        data = fetch_client_info(token, timeout=args.timeout)
        statement = fetch_statement(
            token, account=args.account, days=args.days, timeout=args.timeout,
        )
        receipt = (
            fetch_receipt(token, args.receipt_id, timeout=args.timeout)
            if args.receipt_id else None
        )
    except (RuntimeError, ValueError, httpx.RequestError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1

    if args.raw:
        print(json.dumps({"client_info": data, "statement": statement}, ensure_ascii=False, indent=2))
    else:
        print_summary(data)
        print_statement(statement, days=args.days)
    if receipt is not None:
        content_type = receipt.headers.get("content-type", "application/octet-stream")
        print(f"\n✓ Відповідь чека: HTTP {receipt.status_code}, {content_type}, {len(receipt.content)} байт")
        if args.receipt_output:
            saved_to = save_receipt(receipt, args.receipt_output)
            print(f"Збережено: {saved_to}")
        elif content_type.startswith(("application/json", "text/", "application/xml")):
            print(receipt.text[:4000])
        else:
            print("Бінарну відповідь не виведено. Додайте --receipt-output <файл>.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
