#!/usr/bin/env python3
"""Extract the itemized receipt currently open in the monobank Android app.

This is a fallback for monobank's private e-receipt API. It reads Android's
accessibility hierarchy through ADB and never reads application storage or
authentication tokens.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass


MONEY_RE = re.compile(r"^([+−\-]?\d[\d\s\u00a0]*(?:[.,]\d{1,2})?)\s*₴$")
QTY_RE = re.compile(r"^[xх×]\s*(\d+(?:[.,]\d+)?)$", re.IGNORECASE)
STOP_TEXTS = {
    "Витратили",
    "Переглянути PDF-квитанцію",
    "Поставити запитання",
}


@dataclass(frozen=True)
class ReceiptItem:
    name: str
    quantity: float
    amount: float | None


def adb(*args: str, timeout: float = 20.0) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["adb", *args], check=True, capture_output=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("adb не знайдено") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("adb не відповів вчасно") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or "помилка adb") from exc


def dump_texts() -> list[str]:
    # Some MIUI builds print a harmless stack trace but still create the dump.
    subprocess.run(
        ["adb", "shell", "uiautomator", "dump", "/sdcard/mono-receipt.xml"],
        capture_output=True,
        timeout=20,
        check=False,
    )
    xml_data = adb("exec-out", "cat", "/sdcard/mono-receipt.xml").stdout
    if not xml_data.strip():
        raise RuntimeError("Android не повернув дерево інтерфейсу")
    root = ET.fromstring(xml_data)
    return [
        text.strip()
        for node in root.iter("node")
        if (text := node.attrib.get("text", "").strip())
    ]


def receipt_segment(texts: list[str]) -> list[str]:
    try:
        start = texts.index("Чек") + 1
    except ValueError:
        start = 0
    result: list[str] = []
    for text in texts[start:]:
        if text in STOP_TEXTS or text.startswith("Витратили "):
            break
        result.append(text)
    return result


def parse_items(texts: list[str]) -> list[ReceiptItem]:
    items: list[ReceiptItem] = []
    for index, text in enumerate(texts):
        qty_match = QTY_RE.match(text)
        if not qty_match or index == 0:
            continue
        name = texts[index - 1]
        if name in {"Чек", "Знижка", "Скидка"} or MONEY_RE.match(name):
            continue
        amount: float | None = None
        if index + 1 < len(texts) and (money_match := MONEY_RE.match(texts[index + 1])):
            normalized = money_match.group(1).replace("\u00a0", "").replace(" ", "").replace(",", ".").replace("−", "-")
            amount = float(normalized)
        quantity = float(qty_match.group(1).replace(",", "."))
        items.append(ReceiptItem(name=name, quantity=quantity, amount=amount))
    return items


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Зібрати позиції чека, відкритого у застосунку monobank",
    )
    parser.add_argument("--scrolls", type=int, default=0,
                        help="кількість автоматичних прокруток довгого чека")
    parser.add_argument("--raw", action="store_true",
                        help="додати до JSON видимий текст для діагностики")
    args = parser.parse_args()
    if not 0 <= args.scrolls <= 50:
        parser.error("--scrolls має бути від 0 до 50")

    try:
        merged_texts: list[str] = []
        seen_texts: set[str] = set()
        unique_items: dict[tuple[str, float, float | None], ReceiptItem] = {}
        for step in range(args.scrolls + 1):
            texts = receipt_segment(dump_texts())
            for text in texts:
                if text not in seen_texts:
                    merged_texts.append(text)
                    seen_texts.add(text)
            for item in parse_items(texts):
                unique_items[(item.name, item.quantity, item.amount)] = item
            if step < args.scrolls:
                adb("shell", "input", "swipe", "540", "1850", "540", "800", "450")
                time.sleep(0.8)
    except (RuntimeError, ET.ParseError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1

    result: dict[str, object] = {
        "source": "monobank_android_accessibility",
        "items": [asdict(item) for item in unique_items.values()],
    }
    if args.raw:
        result["visible_text"] = merged_texts
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if unique_items else 2


if __name__ == "__main__":
    raise SystemExit(main())
