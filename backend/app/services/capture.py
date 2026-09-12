"""Free-phrase capture: one spoken or typed sentence → a receipt draft or a
shopping list (CAPTURE_PLAN.md, крок 1).

The user writes the way they would tell a person ("додай чек покупка у бабули
возле форы. помидоры 2кг - 160грн. огурцы 0,7кг по 50грн") and the app has to
decide what was meant and split it into priced lines. Only the *reading* is left
to the model — every sum is recomputed here, because a model that quietly
mis-multiplies 0.7 × 50 would file a wrong receipt with no visible error.

The result is a draft, never a saved receipt: the caller shows it and asks for
confirmation (Telegram reuses the scan-review keyboard). Refuelling is the one
exception — station, fuel and price all come from the user's own data, so it is
booked straight away and the answer offers to undo it.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services import ai

logger = logging.getLogger(__name__)

Intent = Literal["receipt", "fuel", "shopping", "unknown"]

# Below this the reading is treated as a misunderstanding rather than a receipt —
# the bot then answers with its usual help text instead of inventing a purchase.
MIN_CONFIDENCE = 0.35

# How many previously used shop names are offered to the model as spelling hints.
_SHOP_HINTS = 40

# Unit spellings the model may echo back, mapped onto what the rest of the app
# stores. Anything unknown is kept as the user said it.
_UNITS = {
    "шт": "шт", "штук": "шт", "штуки": "шт", "штука": "шт", "pcs": "шт",
    "кг": "кг", "килограмм": "кг", "кілограм": "кг", "kg": "кг",
    "г": "г", "гр": "г", "грам": "г", "грамм": "г",
    "л": "л", "литр": "л", "літр": "л",
    "мл": "мл",
    "уп": "уп", "упаковка": "уп", "пачка": "уп",
    "десяток": "десяток", "десятка": "десяток", "дес": "десяток",
}


class CaptureItem(BaseModel):
    """One priced line of a spoken receipt."""

    name: str
    quantity: float = 1
    unit: str | None = None
    unit_price: float | None = None
    total_price: float | None = None


class CaptureReceipt(BaseModel):
    shop_name: str | None = None
    shop_address: str | None = None
    purchased_at: str | None = None
    items: list[CaptureItem] = Field(default_factory=list)
    total_amount: float | None = None


class CaptureFuel(BaseModel):
    """A refuelling told without the numbers that make a receipt: the station's
    saved price fills in whichever of litres/amount was not said."""

    store: str
    fuel: str = "газ"
    amount: float | None = None          # UAH paid
    liters: float | None = None
    price_per_liter: float | None = None
    purchased_at: str | None = None


class CaptureShoppingItem(BaseModel):
    text: str
    qty: float = 1
    unit: str | None = None
    note: str | None = None


class CaptureDraft(BaseModel):
    intent: Intent = "unknown"
    confidence: float = 0.0
    receipt: CaptureReceipt | None = None
    fuel: CaptureFuel | None = None
    shopping: list[CaptureShoppingItem] = Field(default_factory=list)
    source_text: str = ""

    def draft_dict(self) -> dict[str, Any]:
        """The receipt shaped the way `scan_jobs.normalized_from_draft` and
        `telegram.format_draft_message` already expect a draft."""
        r = self.receipt
        if r is None:
            return {}
        return {
            "shop_name": r.shop_name,
            "shop_address": r.shop_address,
            "purchased_at": r.purchased_at,
            "total_amount": r.total_amount,
            "items": [
                {
                    "name": i.name,
                    "quantity": i.quantity,
                    "unit": i.unit,
                    "unit_price": i.unit_price,
                    "total_price": i.total_price,
                }
                for i in r.items
            ],
        }


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #
_SYSTEM = (
    "You read one short message from a household expense app. The user reports a "
    "purchase they already made (a receipt), a refuelling at a filling station, "
    "or dictates things to buy (a shopping list). Reply with a single valid JSON "
    "object and nothing else — no prose, no code fences."
)

_RULES = """\
Decide the intent:
  "receipt"  — a purchase that already happened, usually with prices
               ("додай чек ...", "купив ...", a place plus amounts).
  "fuel"     — refuelling a car at a filling station ("був на барс і заправився
               газом на 1000грн", "залив 15л газу"). Even with no price said:
               the app looks up the station's own price.
  "shopping" — things still to buy ("додай в список покупок ...", "треба купити ...").
  "unknown"  — anything else; do not guess a purchase out of small talk.

Reading prices (Ukrainian/Russian, mixed spelling and typos are normal):
  * "назва - 600", "назва — 600", "назва 600грн" → that number is the line TOTAL
    (total_price); leave unit_price null.
  * "по 50", "по 50грн" → that number is the price per unit (unit_price);
    leave total_price null, the app multiplies it by the quantity itself.
  * "2кг", "0,7кг", "3десятка", "20шт" → quantity + unit. A decimal comma means
    a decimal point. Quantity defaults to 1.
  * Never compute sums or totals yourself; never invent a price that was not said.

Shop:
  * shop_name — as the user named the place ("Парикмахерская Виктория",
    "покупка у бабули"); do not substitute a retail chain and do not invent a name.
  * shop_address — a street or landmark said about the place ("на индустриальной"
    → "Индустриальная", "возле форы" → "возле Фори").
  * purchased_at — only if a date/time was actually said, ISO-8601; otherwise null.

Refuelling ("fuel"):
  * store — the station as named ("барс", "укрнафта", "БРСМ"); it is matched
    against the user's own stations, so keep the name and nothing else.
  * fuel — "газ" / "дизель" / "бензин", or the exact product if one was named.
    Default to "газ" only when the message says nothing about the fuel.
  * "на 1000грн", "на 1000" → amount (UAH paid); "15л", "залив 15 літрів" →
    liters. Fill in only what was said and leave the other null — the litres or
    the sum are computed from the station's price, never guessed here.
  * price_per_liter — only if the user actually named a price per litre.

Shopping list:
  * One entry per thing to buy. A modifier that covers a listing applies to every
    thing in it: "болты и гайки с шайбами M8 по 20шт" → three entries
    (болты M8 / гайки M8 / шайбы M8), each qty 20, unit "шт".
  * "для <person>" is a note, not part of the name ("молоко для Мілани" →
    text "молоко", note "для Мілани"). But when the phrase says what the thing is
    for rather than who it is for ("крепеж для телевизора"), keep it in the name.
  * Keep the user's own wording and language for names.

Reply exactly as:
{"intent": "receipt|fuel|shopping|unknown",
 "confidence": <0..1>,
 "receipt": {"shop_name": <string|null>, "shop_address": <string|null>,
             "purchased_at": <string|null>,
             "items": [{"name": <string>, "quantity": <number>,
                        "unit": <string|null>, "unit_price": <number|null>,
                        "total_price": <number|null>}]},
 "fuel": {"store": <string>, "fuel": <string>, "amount": <number|null>,
          "liters": <number|null>, "price_per_liter": <number|null>,
          "purchased_at": <string|null>},
 "shopping": [{"text": <string>, "qty": <number>, "unit": <string|null>,
               "note": <string|null>}]}
Use null for "receipt"/"fuel" when the intent is not that one, and [] for
"shopping" when the intent is not "shopping"."""


def build_prompt(text: str, known_shops: list[str] | None = None) -> tuple[str, str]:
    """(system, user) for the capture call. Split out so tests can assert on the
    rules without reaching a model."""
    hint = ""
    if known_shops:
        hint = (
            "\n\nShop names this user already used — reuse the exact spelling when "
            "the message means one of them:\n"
            f"{json.dumps(known_shops, ensure_ascii=False)}"
        )
    return _SYSTEM, f"{_RULES}{hint}\n\nMessage:\n{text.strip()}"


def known_shop_names(db: Session, limit: int = _SHOP_HINTS) -> list[str]:
    """Shop names already filed by hand, newest first — spelling hints so the
    same corner shop doesn't end up under three variants."""
    from app.models.models import Receipt

    rows = db.execute(
        select(Receipt.shop_name)
        .where(Receipt.store_code == "scanner", Receipt.shop_name.isnot(None))
        .order_by(Receipt.purchased_at.desc())
        .limit(limit * 5)
    ).scalars()
    seen: list[str] = []
    for name in rows:
        name = (name or "").strip()
        if name and name not in seen:
            seen.append(name)
        if len(seen) >= limit:
            break
    return seen


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _num(value: Any) -> float | None:
    """A number the way a model may hand it over: 160, "160", "160,5 грн"."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:[.,]\d+)?", str(value))
    return float(match.group(0).replace(",", ".")) if match else None


def _unit(value: Any) -> str | None:
    text = str(value or "").strip().lower().rstrip(".")
    if not text:
        return None
    return _UNITS.get(text, text)


def _clean_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def parse_payload(data: Any, source_text: str = "") -> CaptureDraft:
    """Model output → a validated, arithmetically consistent draft.

    Separate from the model call so the whole reading can be tested offline.
    """
    if not isinstance(data, dict):
        return CaptureDraft(source_text=source_text)

    intent = str(data.get("intent") or "unknown").lower()
    if intent not in ("receipt", "fuel", "shopping"):
        intent = "unknown"
    confidence = _num(data.get("confidence")) or 0.0

    receipt = None
    raw_receipt = data.get("receipt")
    if intent == "receipt" and isinstance(raw_receipt, dict):
        items: list[CaptureItem] = []
        for row in raw_receipt.get("items") or []:
            if not isinstance(row, dict):
                continue
            name = _clean_str(row.get("name"))
            if not name:
                continue
            quantity = _num(row.get("quantity"))
            if quantity is None or quantity <= 0:
                quantity = 1.0
            unit_price = _num(row.get("unit_price"))
            total_price = _num(row.get("total_price"))
            # "по 50грн" only gives the per-unit price — the line total is ours
            # to compute, and the same holds the other way round.
            if total_price is None and unit_price is not None:
                total_price = round(quantity * unit_price, 2)
            elif total_price is not None and unit_price is None and quantity != 1:
                unit_price = round(total_price / quantity, 2)
            items.append(CaptureItem(
                name=name, quantity=quantity, unit=_unit(row.get("unit")),
                unit_price=unit_price, total_price=total_price,
            ))
        receipt = CaptureReceipt(
            shop_name=_clean_str(raw_receipt.get("shop_name")),
            shop_address=_clean_str(raw_receipt.get("shop_address")),
            purchased_at=_clean_str(raw_receipt.get("purchased_at")),
            items=items,
            # The receipt total is always the lines, never the model's addition.
            total_amount=round(sum(i.total_price or 0 for i in items), 2) if items else None,
        )

    fuel = None
    raw_fuel = data.get("fuel")
    if intent == "fuel" and isinstance(raw_fuel, dict):
        store = _clean_str(raw_fuel.get("store"))
        if store:
            fuel = CaptureFuel(
                store=store,
                fuel=_clean_str(raw_fuel.get("fuel")) or "газ",
                amount=_num(raw_fuel.get("amount")),
                liters=_num(raw_fuel.get("liters")),
                price_per_liter=_num(raw_fuel.get("price_per_liter")),
                purchased_at=_clean_str(raw_fuel.get("purchased_at")),
            )

    shopping: list[CaptureShoppingItem] = []
    if intent == "shopping":
        for row in data.get("shopping") or []:
            if not isinstance(row, dict):
                continue
            text = _clean_str(row.get("text"))
            if not text:
                continue
            qty = _num(row.get("qty"))
            if qty is None or qty <= 0:
                qty = 1.0
            shopping.append(CaptureShoppingItem(
                text=text, qty=qty, unit=_unit(row.get("unit")),
                note=_clean_str(row.get("note")),
            ))

    # A reading with nothing in it is not an instruction, whatever the model
    # called it; the caller falls back to its normal answer.
    if intent == "receipt" and (receipt is None or not receipt.items):
        intent, receipt = "unknown", None
    if intent == "shopping" and not shopping:
        intent = "unknown"
    # A refuelling with neither a sum nor a volume leaves nothing to compute:
    # the station price alone cannot say how much was bought.
    if intent == "fuel" and (fuel is None or (fuel.amount is None and fuel.liters is None)):
        intent, fuel = "unknown", None
    if intent != "unknown" and confidence and confidence < MIN_CONFIDENCE:
        intent, receipt, fuel, shopping = "unknown", None, None, []

    return CaptureDraft(
        intent=intent, confidence=confidence, receipt=receipt, fuel=fuel,
        shopping=shopping, source_text=source_text,
    )


async def parse_capture(db: Session, text: str) -> CaptureDraft:
    """Read one free phrase. Raises `ai.AiConfigError` when no AI backend is
    usable — capture is an AI-only feature, there is no regex fallback."""
    text = (text or "").strip()
    if not text:
        return CaptureDraft(source_text="")

    cfg = ai.load_config(db)
    system, user = build_prompt(text, known_shop_names(db))
    raw = await ai.run_text(db, cfg, "intent", system=system, user=user, max_tokens=1500,
                            prompt_preview=text[:300])
    data = raw if isinstance(raw, dict) else ai._extract_json(raw)
    return parse_payload(data, source_text=text)


# --------------------------------------------------------------------------- #
# Applying a draft
# --------------------------------------------------------------------------- #
def create_receipt_job(db: Session, draft: CaptureDraft, *, method: str = "text"):
    """File the read receipt as a `needs_review` scan job.

    Nothing is booked yet: the user confirms it with the same review buttons a
    photographed receipt gets, and `scan_jobs.save_draft()` does the saving.
    """
    from app.models.scanner import ScanJob, ScanStatus

    if draft.receipt is None:
        raise ValueError("у чернетці немає чека")
    job = ScanJob(
        source_filename=None,
        status=ScanStatus.needs_review,
        method=method,
        confidence="low",
        raw_ocr={"draft": draft.draft_dict(), "source_text": draft.source_text},
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def record_fuel(db: Session, draft: CaptureDraft) -> dict[str, Any]:
    """Book the refuelling under the station's own account.

    Unlike a spoken receipt this needs no draft step: the station, the fuel and
    the price all come from the user's own data, so there is nothing for a review
    screen to fix that the message didn't already say. `ValueError` carries a
    message meant for the user (unknown station, no saved price).
    """
    from app.core.time import as_utc
    from app.services import fuel_entry

    if draft.fuel is None:
        raise ValueError("у чернетці немає заправки")
    when = None
    if draft.fuel.purchased_at:
        try:
            when = as_utc(dt.datetime.fromisoformat(draft.fuel.purchased_at))
        except ValueError:
            when = None
    return fuel_entry.record_fuel_purchase(
        db, store=draft.fuel.store, fuel=draft.fuel.fuel, amount=draft.fuel.amount,
        liters=draft.fuel.liters, price_per_liter=draft.fuel.price_per_liter,
        purchased_at=when, source="capture",
    )


def add_shopping_items(db: Session, draft: CaptureDraft) -> list[Any]:
    """Add every read entry to the shopping list through the same handler the UI
    uses, so normalization, category reuse and auto-tick stay identical."""
    from app.api.shopping_list import create_item
    from app.schemas.shopping import ShoppingItemCreate

    rows = []
    for item in draft.shopping:
        rows.append(create_item(
            ShoppingItemCreate(raw_text=item.text, qty=item.qty, unit=item.unit,
                               note=item.note),
            db=db,
        ))
    return rows
