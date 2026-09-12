#!/usr/bin/env python3
"""Standalone diagnostic client for the Ukrainian DPS fiscal receipt API.

Uses only the Python standard library and does not import anything from this
project. The token is read from --token, DPS_API_TOKEN, or a hidden prompt.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path


API_URL = "https://cabinet.tax.gov.ua/ws/api_public/rro/chkAll"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check a DPS API token and download a fiscal receipt.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "receipt_url",
        nargs="?",
        help="Full https://cabinet.tax.gov.ua/cashregs/check?... URL",
    )
    parser.add_argument("--token", help="API token (prefer DPS_API_TOKEN or the hidden prompt)")
    parser.add_argument("--id", dest="receipt_id", help="Fiscal document/check ID")
    parser.add_argument("--fn", help="Fiscal RRO/PRRO number")
    parser.add_argument("--date", help="YYYYMMDD, YYYY-MM-DD, or YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--time", help="HHMM or HHMMSS")
    parser.add_argument("--type", choices=("1", "2", "3"), default="1",
                        help="1=original XML, 2=signed XML, 3=printable text")
    parser.add_argument("--timeout", type=float, default=30.0, help="Request timeout in seconds")
    parser.add_argument("--output-dir", default="dps_debug_output",
                        help="Directory for raw JSON and decoded fields")
    parser.add_argument("--print-content", action="store_true",
                        help="Print decoded receipt content (may contain personal data)")
    parser.add_argument("--insecure", action="store_true",
                        help="Disable TLS certificate verification for diagnostics only")
    return parser.parse_args()


def receipt_params(args: argparse.Namespace) -> dict[str, str]:
    values: dict[str, str] = {}
    if args.receipt_url:
        parsed = urllib.parse.urlparse(args.receipt_url)
        if parsed.scheme != "https" or parsed.hostname != "cabinet.tax.gov.ua":
            raise ValueError("receipt_url must be an HTTPS cabinet.tax.gov.ua URL")
        values.update({k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()})

    receipt_id = args.receipt_id or values.get("id")
    fn = args.fn or values.get("fn")
    raw_date = args.date or values.get("date")
    raw_time = args.time or values.get("time")
    if not receipt_id or not fn:
        raise ValueError("receipt id and fn are required (in URL or via --id/--fn)")

    result = {"id": receipt_id, "fn": fn, "type": args.type}
    if raw_date:
        result["date"] = normalize_date(raw_date, raw_time)
    return result


def normalize_date(raw_date: str, raw_time: str | None) -> str:
    date_text = raw_date.strip()
    time_text = (raw_time or "").strip().replace(":", "")
    if " " in date_text:
        return datetime.strptime(date_text, "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")
    date_digits = date_text.replace("-", "").replace(".", "")
    if len(date_digits) != 8 or not date_digits.isdigit():
        raise ValueError("date must be YYYYMMDD, YYYY-MM-DD, or YYYY-MM-DD HH:MM:SS")
    if not time_text:
        time_text = "000000"
    elif len(time_text) == 4:
        time_text += "00"
    if len(time_text) != 6 or not time_text.isdigit():
        raise ValueError("time must be HHMM or HHMMSS")
    return datetime.strptime(date_digits + time_text, "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")


def decode_field(value: object) -> bytes | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return base64.b64decode(value, validate=True)
    except Exception:
        return None


def readable_text(data: bytes) -> tuple[str, str]:
    for encoding in ("utf-8", "cp1251"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", "replace"), "utf-8 with replacement"


def main() -> int:
    args = parse_args()
    try:
        params = receipt_params(args)
    except ValueError as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        return 2

    token = (args.token or os.environ.get("DPS_API_TOKEN") or "").strip()
    if not token:
        token = getpass.getpass("DPS API token (hidden): ").strip()
    if not token:
        print("Input error: token is empty", file=sys.stderr)
        return 2

    request_params = {**params, "token": token}
    request_url = API_URL + "?" + urllib.parse.urlencode(request_params)
    safe_url = API_URL + "?" + urllib.parse.urlencode({**params, "token": "<redacted>"})
    request = urllib.request.Request(
        request_url,
        headers={"Accept": "application/json", "User-Agent": "DPS-API-Debug/1.0"},
        method="GET",
    )
    context = ssl._create_unverified_context() if args.insecure else ssl.create_default_context()

    print(f"Endpoint: {safe_url}")
    print(f"Token: configured, length={len(token)}, suffix=...{token[-4:] if len(token) >= 4 else '****'}")
    try:
        with urllib.request.urlopen(request, timeout=args.timeout, context=context) as response:
            status = response.status
            content_type = response.headers.get("Content-Type", "")
            body = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        content_type = exc.headers.get("Content-Type", "") if exc.headers else ""
        body = exc.read()
    except urllib.error.URLError as exc:
        print(f"Network error: {exc.reason}", file=sys.stderr)
        return 3
    except TimeoutError:
        print(f"Network error: request timed out after {args.timeout:g}s", file=sys.stderr)
        return 3

    print(f"HTTP status: {status}")
    print(f"Content-Type: {content_type or '<missing>'}")
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        preview = body[:1000].decode("utf-8", "replace")
        print("Response is not valid UTF-8 JSON.")
        print(f"Body preview:\n{preview}")
        return 4

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = output_dir / "response.json"
    raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"resultCode: {payload.get('resultCode')!r}")
    print(f"resultText: {payload.get('resultText')!r}")
    if payload.get("error") or payload.get("error_description"):
        print(f"error: {payload.get('error')!r}")
        print(f"error_description: {payload.get('error_description')!r}")

    decoded_count = 0
    for field in ("check", "checkXml", "checkP7s"):
        encoded = payload.get(field)
        data = decode_field(encoded)
        if data is None:
            print(f"{field}: absent or not valid base64")
            continue
        decoded_count += 1
        binary_path = output_dir / f"{field}.bin"
        binary_path.write_bytes(data)
        text, encoding = readable_text(data)
        text_path = output_dir / f"{field}.txt"
        text_path.write_text(text, encoding="utf-8")
        print(f"{field}: decoded {len(data)} bytes; text encoding={encoding}; saved to {text_path}")
        if args.print_content:
            print(f"\n--- {field} ---\n{text}\n--- end {field} ---")

    print(f"Raw response saved to {raw_path}")
    if status >= 400:
        return 5
    if decoded_count == 0:
        print("API responded successfully, but no receipt content was decoded.")
        return 6
    print("API call and receipt decoding succeeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
