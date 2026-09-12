"""Stage 2 — parse a receipt whose retail chain we recognise, using a per-chain
layout spec kept as data (``app/scanner/templates/*.yaml``), not code.

Adding a chain = adding a YAML file. Schema (see templates/_SCHEMA.md):

    code:          short chain id (matches Store.code where one exists)
    store_name:    display name used on the Receipt when the layout matches
    network_type:  supermarket | azs | tech | pharmacy | home | other
    identify:      markers that must appear in the OCR text
      contains:    [list of substrings]  — case-insensitive, ALL must be present
      any:         [list of substrings]  — case-insensitive, at least ONE present
      edrpou:      [list]                 — ЄДРПОУ/ІПН codes, any match
    fields:        {name: [regex, ...]}   — first capturing group wins; keys:
                   total, discount, datetime (2 groups: date, time), payment
    line_item:
      pattern:          regex with named groups name / qty / price / sum
      discount_pattern: regex with named group `discount`, matched on the line(s)
                        right after a product line
    date_formats:   strptime patterns to try for the datetime field's date group

Everything is best-effort: a spec that half-matches still yields a
NormalizedReceipt (possibly with no items), and reconcile.py decides whether the
pipeline trusts it.
"""
from __future__ import annotations

import datetime as dt
import functools
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from app.adapters.base import NormalizedItem, NormalizedReceipt
from app.core.time import store_local_to_utc

TEMPLATES_DIR = Path(__file__).parent / "templates"

_DEFAULT_DATE_FORMATS = ["%d.%m.%Y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%y"]
_NUM_RE = re.compile(r"(-?[\d  ]+[.,]\d{1,2})")


class Identify(BaseModel):
    contains: list[str] = Field(default_factory=list)
    any: list[str] = Field(default_factory=list)
    edrpou: list[str] = Field(default_factory=list)


class LineItemSpec(BaseModel):
    pattern: str
    discount_pattern: str | None = None


class TemplateSpec(BaseModel):
    code: str
    store_name: str
    network_type: str | None = None
    identify: Identify = Field(default_factory=Identify)
    fields: dict[str, list[str]] = Field(default_factory=dict)
    line_item: LineItemSpec | None = None
    date_formats: list[str] = Field(default_factory=lambda: list(_DEFAULT_DATE_FORMATS))

    def matches(self, text: str) -> bool:
        low = text.casefold()
        digits = re.sub(r"\D", "", text)
        if self.identify.contains and not all(m.casefold() in low for m in self.identify.contains):
            return False
        if self.identify.any and not any(m.casefold() in low for m in self.identify.any):
            return False
        if self.identify.edrpou and not any(code in digits for code in self.identify.edrpou):
            return False
        # A spec with no markers at all never auto-matches.
        return bool(self.identify.contains or self.identify.any or self.identify.edrpou)


def _to_float(raw: str) -> float | None:
    raw = raw.replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return round(float(raw), 2)
    except ValueError:
        return None


def _first_group(patterns: list[str], text: str) -> str | None:
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
        if m:
            return m.group(1) if m.groups() else m.group(0)
    return None


def _parse_datetime(spec: TemplateSpec, text: str) -> dt.datetime | None:
    for pat in spec.fields.get("datetime", []):
        m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
        if not m:
            continue
        date_s = m.group(1).strip()
        time_s = (m.group(2).strip() if m.lastindex and m.lastindex >= 2 else "00:00")
        for fmt in spec.date_formats:
            try:
                d = dt.datetime.strptime(date_s, fmt).date()
                break
            except ValueError:
                continue
        else:
            continue
        try:
            parts = [int(p) for p in re.split(r"[:.]", time_s)]
            t = dt.time(*(parts + [0, 0])[:3])
        except (ValueError, TypeError):
            t = dt.time()
        return store_local_to_utc(dt.datetime.combine(d, t))
    return None


def _parse_items(spec: TemplateSpec, lines: list[str]) -> list[NormalizedItem]:
    if spec.line_item is None:
        return []
    item_re = re.compile(spec.line_item.pattern, re.IGNORECASE)
    disc_re = (
        re.compile(spec.line_item.discount_pattern, re.IGNORECASE)
        if spec.line_item.discount_pattern else None
    )
    items: list[NormalizedItem] = []
    for line in lines:
        m = item_re.search(line)
        if m:
            g = m.groupdict()
            name = (g.get("name") or "").strip(" .-·")
            total = _to_float(g.get("sum") or "") if g.get("sum") else None
            price = _to_float(g.get("price") or "") if g.get("price") else None
            qty = _to_float(g.get("qty") or "1") or 1.0
            if total is None and price is not None:
                total = round(price * qty, 2)
            if not name or total is None:
                continue
            items.append(NormalizedItem(
                name=name, total_price=total, quantity=qty, unit_price=price,
            ))
            continue
        if disc_re and items:
            dm = disc_re.search(line)
            if dm:
                d = _to_float(dm.groupdict().get("discount") or "")
                if d:
                    last = items[-1]
                    last.discount = round(last.discount + abs(d), 2)
                    last.total_price = round(last.total_price - abs(d), 2)
    return items


def parse_with_template(spec: TemplateSpec, text: str) -> NormalizedReceipt:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    total = _first_group(spec.fields.get("total", []), text)
    discount = _first_group(spec.fields.get("discount", []), text)
    payment = _first_group(spec.fields.get("payment", []), text)
    items = _parse_items(spec, lines)

    total_amount = _to_float(total) if total else None
    if total_amount is None and items:
        total_amount = round(sum(it.total_price for it in items), 2)

    return NormalizedReceipt(
        external_id="",  # filled in by the pipeline (fiscal key or content hash)
        purchased_at=_parse_datetime(spec, text) or store_local_to_utc(dt.datetime.now()),
        total_amount=total_amount or 0.0,
        shop_name=spec.store_name,
        payment_type=payment,
        total_discount=_to_float(discount) if discount else None,
        items=items,
        raw={"method": f"template:{spec.code}"},
    )


@functools.lru_cache(maxsize=1)
def load_templates() -> list[TemplateSpec]:
    specs: list[TemplateSpec] = []
    if not TEMPLATES_DIR.is_dir():
        return specs
    for path in sorted(TEMPLATES_DIR.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        specs.append(TemplateSpec.model_validate(data))
    return specs


def identify_template(text: str) -> TemplateSpec | None:
    for spec in load_templates():
        if spec.matches(text):
            return spec
    return None
