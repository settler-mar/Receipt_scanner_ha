"""Shopping list: receipt auto-tick and store suggestion (ROADMAP.md Крок 2).

  * "Логіка автовідмітки" — on every saved receipt, flip any matching pending
    list row to `bought` as a candidate (barcode match, else normalized-name
    exact / token-overlap). Deterministic, no AI.
  * "Логіка підбору магазину" — rank chains for the current pending list by
    coverage and approximate basket cost. `suggest_stores` uses receipt history
    only; `suggest_stores_live` queries each store's catalogue right now.
  * "Роль AI" — the *only* AI touch point here: `resolve_query` turns a free
    phrase ("молоко") into a per-store catalogue search query via
    `ai.normalize_query`, cached forever in `product_aliases`. Falls back to the
    raw phrase (uncached) when no AI backend is configured.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import statistics
import hashlib
import re
from collections import defaultdict

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.core.time import as_utc
from app.models.models import Account, Category, ProductCategory, Receipt, ReceiptItem, Store
from app.models.shopping import (
    ProductAlias, ShoppingListItem, ShoppingListStatus, ShoppingTrip,
    ShoppingTripItem, ShoppingTripItemStatus, ShoppingTripStatus, ShoppingRoutine,
    ShoppingRoutineCategory,
    ShoppingProductAlias, ShoppingMatchCandidate, ShoppingMatchStatus,
)
from app.services.text import normalize_name

logger = logging.getLogger(__name__)

# Jaccard threshold for "these two normalized names are the same product" when
# neither is a clean subset of the other.
_OVERLAP_THRESHOLD = 0.6
# How far back `rescan` / a freshly added item looks through receipt history.
_RESCAN_DAYS = 60
_TRIP_IDLE_TIMEOUT = dt.timedelta(hours=3)

_STORE_WORDS_RE = re.compile(r"\b(епіцентр\s*к|епицентр|атб|сільпо|силпо|фора|метро|metro|foxy|fozzy)\b", re.I)

# Store catalogues mix Ukrainian/Russian names and use punctuation differently
# for model numbers ("WD-40" vs "WD 40"). These are matching aliases only;
# the stored/displayed product names remain unchanged.
_MATCH_TOKEN_ALIASES = {
    "алюминевая": "алюмінієва",
    "алюминиевая": "алюмінієва",
    "алюмінева": "алюмінієва",
}


def canonical_product(raw: str) -> tuple[str, str]:
    """Return (stable key, short display name) for recommendation grouping.

    This is intentionally conservative. Only known broad product families are
    collapsed; unknown names remain separate instead of being falsely merged.
    """
    text = _STORE_WORDS_RE.sub(" ", normalize_name(raw))
    text = re.sub(r"\b\d+(?:[.,]\d+)?\s*(?:х|x)\s*\d+(?:[.,]\d+)?\s*см\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if re.search(r"\bпакет", text):
        return "пакети", "Пакети"
    if re.search(r"\b(ґрунтов|грунтов)", text):
        if re.search(r"глиб|глубок", text):
            return "ґрунтовка глибокопроникна", "Ґрунтовка глибокопроникна"
        return "ґрунтовка", "Ґрунтовка"
    if re.search(r"\b(піца|пиц|pizza)", text):
        return "піца", "Піца"
    if re.search(r"\b(помідор|помидор|томат)", text):
        return "помідори", "Помідори"
    if re.search(r"\b(яйц|яиц)", text):
        if re.search(r"дерев|декор|сувен", text):
            return "яйця декоративні", "Яйця декоративні"
        if re.search(r"кіндер|киндер|шоколад|сюрприз", text):
            return "яйця шоколадні", "Яйця шоколадні"
        return "яйця курячі", "Яйця курячі"
    if re.search(r"\b(хлеб|хліб|батон)\b", text):
        return "хліб", "Хліб"
    return text, raw.strip() or text


def category_name_ru(category: Category | None) -> str:
    """Human label for the UI; category ids/paths never leak to users.

    The taxonomy nodes already carry reviewed human names, so the node name is
    the label. Only a handful of legacy slugs without a readable name fall back
    to the keyword table below.
    """
    if category is None:
        return "Другое"
    name = (category.name or "").strip()
    if name and not re.fullmatch(r"[a-z0-9-]+", name):
        return name
    text = f"{category.name} {category.path}".casefold()
    for needles, label in (
        (("хліб", "хлеб"), "Хлеб и выпечка"), (("молоч",), "Молочные продукты"),
        (("яйц",), "Яйца"), (("овоч", "зелень"), "Овощи и зелень"),
        (("фрукт", "ягод"), "Фрукты и ягоды"), (("м'яс", "мяс"), "Мясо и птица"),
        (("напої", "напит"), "Напитки"), (("вода",), "Вода"),
        (("продукт",), "Продукты"), (("будів", "строй", "ремонт"), "Стройматериалы"),
        (("подуш", "kylymy-pledy-ta-podushky"), "Подушки, пледы и ковры"),
        (("ізоляц", "изоляц", "гермет", "izoliatsiia-ta-hermetyzatsiia"), "Изоляция и герметизация"),
        (("кріплен", "креплен", "метиз", "kriplennia-ta-metyzy"), "Крепёж и метизы"),
    ):
        if any(needle in text for needle in needles):
            return label
    return name or "Другое"


def category_group_name(db: Session, category: Category | None) -> str | None:
    """Top-level branch label for a category, or None when it *is* the root."""
    if category is None or "/" not in (category.path or ""):
        return None
    root = category.path.split("/", 1)[0]
    row = db.execute(select(Category).where(Category.path == root)).scalar_one_or_none()
    return category_name_ru(row) if row is not None else None


def rebuild_product_dictionary(db: Session) -> None:
    """Precompute aliases from historical list rows and compatible receipts."""
    list_rows = db.execute(select(ShoppingListItem)).scalars().all()
    profiles: dict[str, tuple[str, str | None]] = {}
    for row in list_rows:
        key, display = canonical_product(row.raw_text)
        if key:
            profiles.setdefault(key, (display, row.category_id))
            db.merge(ShoppingProductAlias(
                alias_name=normalize_name(row.raw_text), canonical_name=key,
                display_name=display, category_id=row.category_id, source="list",
                confidence=1,
            ))
    if not profiles:
        db.commit()
        return
    receipt_rows = db.execute(select(ReceiptItem)).scalars().all()
    for item in receipt_rows:
        alias = normalize_name(item.name)
        key, display = canonical_product(item.name)
        if key not in profiles:
            continue
        profile_display, profile_category = profiles[key]
        db.merge(ShoppingProductAlias(
            alias_name=alias, canonical_name=key, display_name=profile_display or display,
            category_id=item.category_id or profile_category, source="rule", confidence=0.95,
        ))
    db.commit()


def active_items(db: Session) -> list[ShoppingListItem]:
    """Return the active list shared by HTTP and Telegram clients."""
    return db.execute(
        select(ShoppingListItem)
        .where(ShoppingListItem.status.in_((ShoppingListStatus.pending, ShoppingListStatus.bought)))
        .order_by(ShoppingListItem.added_at.desc())
    ).scalars().all()


def mark_pending_by_text(db: Session, phrase: str) -> ShoppingListItem | None:
    """Manually mark one pending item by an exact normalized name."""
    key = normalize_name(phrase)
    if not key:
        return None
    row = db.execute(
        select(ShoppingListItem)
        .where(
            ShoppingListItem.status == ShoppingListStatus.pending,
            ShoppingListItem.normalized_name == key,
        )
        .order_by(ShoppingListItem.added_at.desc())
    ).scalars().first()
    if row is None:
        return None
    row.status = ShoppingListStatus.bought
    row.auto_matched = False
    row.bought_at = dt.datetime.now(dt.timezone.utc)
    db.commit()
    db.refresh(row)
    return row


def expire_stale_trips(db: Session, *, now: dt.datetime | None = None) -> int:
    """Complete active trips that have had no action for three hours."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - _TRIP_IDLE_TIMEOUT
    stale = db.execute(select(ShoppingTrip).where(
        ShoppingTrip.status == ShoppingTripStatus.active,
        ShoppingTrip.last_activity_at < cutoff,
    )).scalars().all()
    for trip in stale:
        rows = db.execute(select(ShoppingTripItem).where(
            ShoppingTripItem.trip_id == trip.id
        )).scalars().all()
        for item in rows:
            if item.status != ShoppingTripItemStatus.bought:
                item.status = ShoppingTripItemStatus.skipped
        trip.status = ShoppingTripStatus.completed
        trip.completed_at = now
    if stale:
        db.commit()
    return len(stale)


def create_trip(db: Session, *, store_code: str | None = None,
                owner_name: str = "Неизвестно") -> ShoppingTrip:
    """Create an independent checklist snapshot for one participant."""
    expire_stale_trips(db)
    now = dt.datetime.now(dt.timezone.utc)
    trip = ShoppingTrip(store_code=store_code or None,
                        owner_name=owner_name.strip() or "Неизвестно",
                        last_activity_at=now)
    db.add(trip)
    db.flush()
    existing_ids = set(db.execute(select(ShoppingTripItem.shopping_list_item_id).where(
        ShoppingTripItem.trip_id == trip.id
    )).scalars().all())
    pending = db.execute(
        select(ShoppingListItem).where(or_(
            ShoppingListItem.status == ShoppingListStatus.pending,
            and_(
                ShoppingListItem.status == ShoppingListStatus.bought,
                ShoppingListItem.bought_receipt_item_id.is_(None),
                ShoppingListItem.auto_matched.is_(False),
            ),
        ))
    ).scalars().all()
    for row in pending:
        if row.id in existing_ids:
            continue
        categorize_list_item_from_history(db, row)
        if trip.store_code and row.category_id and not store_has_category_history(
            db, trip.store_code, row.category_id
        ):
            continue
        db.add(ShoppingTripItem(
            trip_id=trip.id, shopping_list_item_id=row.id, raw_text=row.raw_text,
            qty=row.qty, unit=row.unit, note=row.note,
        ))
    db.commit()
    db.refresh(trip)
    return trip


def sync_active_trip(db: Session) -> ShoppingTrip | None:
    """Backward-compatible singular accessor for non-HTTP callers."""
    trips = sync_active_trips(db)
    return trips[0] if trips else None


def sync_active_trips(db: Session) -> list[ShoppingTrip]:
    """Expire idle trips and synchronize every remaining active checklist."""
    expire_stale_trips(db)
    active = db.execute(select(ShoppingTrip).where(
        ShoppingTrip.status == ShoppingTripStatus.active
    ).order_by(ShoppingTrip.created_at.asc())).scalars().all()
    for trip in active:
        _sync_trip_items(db, trip)
    return active


def _sync_trip_items(db: Session, trip: ShoppingTrip) -> None:
    """Add new eligible rows without resetting the trip's inactivity timer."""
    existing_ids = set(db.execute(select(ShoppingTripItem.shopping_list_item_id).where(
        ShoppingTripItem.trip_id == trip.id
    )).scalars().all())
    pending = db.execute(
        select(ShoppingListItem).where(or_(
            ShoppingListItem.status == ShoppingListStatus.pending,
            and_(
                ShoppingListItem.status == ShoppingListStatus.bought,
                ShoppingListItem.bought_receipt_item_id.is_(None),
                ShoppingListItem.auto_matched.is_(False),
            ),
        ))
    ).scalars().all()
    for row in pending:
        if row.id in existing_ids:
            continue
        categorize_list_item_from_history(db, row)
        if trip.store_code and row.category_id and not store_has_category_history(
            db, trip.store_code, row.category_id
        ):
            continue
        db.add(ShoppingTripItem(
            trip_id=trip.id, shopping_list_item_id=row.id, raw_text=row.raw_text,
            qty=row.qty, unit=row.unit, note=row.note,
        ))
    db.commit()
    db.refresh(trip)


def touch_trip(db: Session, trip: ShoppingTrip) -> ShoppingTrip:
    """Reset the inactivity timeout after an action in the checklist."""
    trip.last_activity_at = dt.datetime.now(dt.timezone.utc)
    db.commit()
    db.refresh(trip)
    return trip


def update_trip_item(db: Session, item: ShoppingTripItem,
                     status: ShoppingTripItemStatus) -> ShoppingTripItem:
    item.status = status
    # A trip is only a temporary checklist. The durable shopping-list row is
    # changed exclusively by receipt matching, so ticking a Telegram/web item
    # never claims that the purchase actually happened.
    trip = db.get(ShoppingTrip, item.trip_id)
    if trip is not None and trip.status == ShoppingTripStatus.active:
        trip.last_activity_at = dt.datetime.now(dt.timezone.utc)
    db.commit()
    db.refresh(item)
    return item


def complete_trip(db: Session, trip: ShoppingTrip) -> ShoppingTrip:
    """Finish a trip without changing the durable shopping list."""
    rows = db.execute(select(ShoppingTripItem).where(ShoppingTripItem.trip_id == trip.id)).scalars().all()
    for item in rows:
        if item.status != ShoppingTripItemStatus.bought:
            item.status = ShoppingTripItemStatus.skipped
    trip.status = ShoppingTripStatus.completed
    trip.completed_at = dt.datetime.now(dt.timezone.utc)
    db.commit()
    db.refresh(trip)
    return trip


def mark_trip_by_text(db: Session, phrase: str) -> ShoppingTripItem | None:
    """Tick an exact item in the active trip; receipts remain source of truth."""
    key = normalize_name(phrase)
    if not key:
        return None
    trip = db.execute(select(ShoppingTrip).where(
        ShoppingTrip.status == ShoppingTripStatus.active
    )).scalars().first()
    if trip is None:
        return None
    item = db.execute(select(ShoppingTripItem).where(
        ShoppingTripItem.trip_id == trip.id,
        ShoppingTripItem.status != ShoppingTripItemStatus.bought,
    )).scalars().all()
    hit = next((row for row in item if normalize_name(row.raw_text) == key), None)
    if hit is None:
        return None
    return update_trip_item(db, hit, ShoppingTripItemStatus.bought)


def routine_suggestions(db: Session) -> list[dict]:
    """Find overdue groups that originate from the durable shopping list."""
    rebuild_product_dictionary(db)
    aliases = {
        row.alias_name: row for row in db.execute(select(ShoppingProductAlias)).scalars().all()
    }
    rows = db.execute(
        select(ReceiptItem.name, ReceiptItem.quantity, ReceiptItem.unit,
               Receipt.purchased_at, ReceiptItem.category_id)
        .join(Receipt, Receipt.id == ReceiptItem.receipt_id)
        .where(Receipt.purchased_at.is_not(None))
        .order_by(Receipt.purchased_at.asc())
    ).all()
    by_name: dict[str, list[tuple[dt.datetime, float, str | None]]] = defaultdict(list)
    display: dict[str, str] = {}
    category_by_name: dict[str, tuple[str | None, str | None]] = {}
    for name, quantity, unit, purchased_at, category_id in rows:
        alias = aliases.get(normalize_name(name))
        if alias and purchased_at:
            key = alias.canonical_name
            by_name[key].append((as_utc(purchased_at), float(quantity or 1), unit))
            display.setdefault(key, alias.display_name)
            category = db.get(Category, category_id or alias.category_id) if (category_id or alias.category_id) else None
            category_by_name.setdefault(key, (category.id if category else None,
                                              category.path if category else None))
    ignored_category_paths = [
        category.path for category in db.execute(
            select(Category).join(ShoppingRoutineCategory,
                                  ShoppingRoutineCategory.category_id == Category.id)
            .where(ShoppingRoutineCategory.ignored.is_(True))
        ).scalars().all()
    ]
    active = {li.normalized_name for li in db.execute(
        select(ShoppingListItem).where(ShoppingListItem.status.in_((ShoppingListStatus.pending, ShoppingListStatus.bought)))
    ).scalars().all()}
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for key, purchases in by_name.items():
        dates = [row[0] for row in purchases]
        if len(dates) < 3 or any(canonical_product(item.raw_text)[0] == key for item in db.execute(
            select(ShoppingListItem).where(ShoppingListItem.status.in_((ShoppingListStatus.pending, ShoppingListStatus.bought)))
        ).scalars().all()):
            continue
        intervals = [(b - a).total_seconds() / 86400 for a, b in zip(dates, dates[1:])]
        interval = statistics.median(intervals)
        if interval < 3 or interval > 180:
            continue
        routine = db.get(ShoppingRoutine, key)
        if routine and routine.ignored:
            continue
        if routine and routine.postponed_until and as_utc(routine.postponed_until) > now:
            continue
        category_id, category_path = category_by_name.get(key, (None, None))
        if category_path and any(
            category_path == ignored or category_path.startswith(ignored + "/")
            for ignored in ignored_category_paths
        ):
            continue
        last = dates[-1]
        median_qty = statistics.median(row[1] for row in purchases)
        units = [row[2] for row in purchases if row[2]]
        effective = float(routine.interval_days_override) if routine and routine.interval_days_override else interval
        due = last + dt.timedelta(days=effective)
        if due > now:
            continue
        out.append({"normalized_name": key, "display_name": display[key],
                    "purchase_count": len(dates), "interval_days": round(effective, 1),
                    "median_qty": round(float(median_qty), 3),
                    "unit": max(set(units), key=units.count) if units else None,
                    "last_purchased_at": last, "due_at": due,
                    "days_overdue": max(0, (now.date() - due.date()).days),
                    "ignored": False,
                    "postponed_until": routine.postponed_until if routine else None,
                    "category_id": category_id, "category_path": category_path})
    return sorted(out, key=lambda row: (-row["days_overdue"], row["display_name"]))


def routine_category_action(db: Session, category_id: str, action: str) -> None:
    category = db.get(Category, category_id)
    if category is None:
        return
    row = db.get(ShoppingRoutineCategory, category_id)
    if row is None:
        row = ShoppingRoutineCategory(category_id=category_id)
        db.add(row)
    row.ignored = action == "ignore"
    db.commit()


def routine_token(normalized_name: str) -> str:
    return hashlib.sha1(normalized_name.encode("utf-8")).hexdigest()[:12]


def routine_name_by_token(db: Session, token: str) -> str | None:
    for row in routine_suggestions(db):
        if routine_token(row["normalized_name"]) == token:
            return row["normalized_name"]
    return None


def routine_action(db: Session, normalized_name: str, action: str, *, postpone_days: int = 7) -> dict | None:
    key = normalize_name(normalized_name)
    if not key:
        return None
    routine = db.get(ShoppingRoutine, key) or ShoppingRoutine(normalized_name=key)
    if routine not in db:
        db.add(routine)
    if action == "ignore":
        routine.ignored = True
        routine.postponed_until = None
    elif action == "unignore":
        routine.ignored = False
    elif action == "postpone":
        routine.postponed_until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=postpone_days)
    elif action == "add":
        if not db.execute(select(ShoppingListItem).where(
            ShoppingListItem.normalized_name == key,
            ShoppingListItem.status.in_((ShoppingListStatus.pending, ShoppingListStatus.bought)),
        )).scalars().first():
            db.add(ShoppingListItem(raw_text=normalized_name, normalized_name=key,
                                    added_method="recommendation", added_by="system"))
        routine.postponed_until = None
    else:
        return None
    db.commit()
    return next((row for row in routine_suggestions(db) if row["normalized_name"] == key), None)


def names_match(list_norm: str, item_norm: str) -> bool:
    if not list_norm or not item_norm:
        return False
    # A shopping entry can be typed in Russian while the store receipt uses
    # Ukrainian ("сыр"/"сир", "нагетсы"/"нагетси").  These are spelling
    # variants, not different products, so fold the small set of shared
    # Russian/Ukrainian orthographic differences before token comparison.
    variants = str.maketrans({"ы": "и", "э": "е", "ё": "е", "ъ": "ь"})
    def match_tokens(value: str) -> list[str]:
        value = value.translate(variants)
        # Treat model-number punctuation and attached units as token
        # separators: "WD-40" == "WD 40", "20мм" == "20 мм".
        value = re.sub(r"[-_/.,]+", " ", value)
        value = re.sub(r"(?<=\d)(?=[a-zа-яіїєґ])|(?<=[a-zа-яіїєґ])(?=\d)", " ", value)
        return [_MATCH_TOKEN_ALIASES.get(token, token) for token in value.split()]

    list_tokens = match_tokens(list_norm)
    item_tokens = match_tokens(item_norm)
    list_norm = " ".join(list_tokens)
    item_norm = " ".join(item_tokens)
    if list_norm == item_norm:
        return True
    a = set(list_tokens)
    b = set(item_tokens)
    inter = a & b
    if not inter:
        return False
    if a <= b:  # every word of the list entry appears in the receipt line
        return True
    if len(inter) / len(a | b) >= _OVERLAP_THRESHOLD:
        return True
    # Construction-store names often translate the product noun differently
    # ("алюминиевая пластина" / "смуга алюмінієва"). Material + a shared
    # numeric dimension is a sufficiently specific fallback for that shape.
    has_material = "алюмінієва" in inter or "алюміній" in inter
    shared_dimension = any(token.isdigit() and token in b for token in a)
    return has_material and shared_dimension


def _list_key(li: ShoppingListItem) -> str:
    return li.normalized_name or normalize_name(li.raw_text)


def known_category_id(db: Session, text: str) -> str | None:
    """Best-effort category for a freshly typed list phrase — reuses the Крок 1
    `product_category` alias cache, no AI. Returns None when the exact
    normalized phrase has never been classified."""
    norm = normalize_name(text)
    if not norm:
        return None
    row = db.get(ProductCategory, norm)
    return row.category_id if row is not None else None


def categorize_list_item_from_history(db: Session, row: ShoppingListItem) -> str | None:
    """Resolve an uncategorized list row from the existing dictionaries."""
    if row.category_id:
        return row.category_id
    norm = normalize_name(row.raw_text)
    known = db.get(ProductCategory, norm) if norm else None
    if known and known.category_id:
        row.category_id = known.category_id
        return row.category_id
    aliases = db.execute(select(ShoppingProductAlias).where(
        ShoppingProductAlias.canonical_name == canonical_product(row.raw_text)[0],
        ShoppingProductAlias.category_id.is_not(None),
    )).scalars().all()
    if aliases:
        row.category_id = aliases[0].category_id
        return row.category_id
    # Short free-text list entries often have no exact history alias. Use the
    # taxonomy itself as a deterministic fallback before asking AI.
    rules = (
        (("шуруп", "дюбел", "гвозд", "саморез"), "kriplennia-ta-metyzy"),
        (("силикон", "герметик", "клей"), "izoliatsiia-ta-hermetyzatsiia"),
        (("подуш",), "kylymy-pledy-ta-podushky"),
        (("йогурт", "кефир", "сметан"), "kyslomolochni-produkty"),
        (("молок", "сливк"), "moloko-ta-vershky"),
        (("хлеб", "хліб", "батон"), "khlib-ta-vypichka"),
        (("яйц", "яиц"), "iaitsia"),
    )
    text = norm or ""
    for words, fragment in rules:
        if any(word in text for word in words):
            category = db.execute(select(Category).where(Category.path.like(f"%{fragment}%"))
                .order_by(Category.path.desc())).scalars().first()
            if category:
                row.category_id = category.id
                return row.category_id
    return None


def uncategorized_pending(db: Session) -> list[ShoppingListItem]:
    """Pending list rows that still have no category."""
    return db.execute(select(ShoppingListItem).where(
        ShoppingListItem.status == ShoppingListStatus.pending,
        ShoppingListItem.category_id.is_(None),
    ).order_by(ShoppingListItem.added_at.desc())).scalars().all()


async def categorize_pending(db: Session, *, use_ai: bool = True,
                             limit: int = 50) -> dict:
    """Categorize pending rows: dictionaries first, then AI for the rest.

    The local dictionaries only know phrases seen before, so a fresh list stays
    empty without the AI pass. Guesses are cached in `product_category` through
    the shared `categorize.apply_guesses`, which makes the next identical phrase
    (web, Telegram or MCP) free and keeps receipts consistent with the list.
    """
    from app.services import categorize as categorize_svc

    rows = uncategorized_pending(db)
    by_dictionary = sum(1 for row in rows if categorize_list_item_from_history(db, row))
    db.commit()

    rest = [row for row in rows if row.category_id is None][:limit]
    by_ai, ai_error = 0, None
    if rest and use_ai:
        names, seen = [], set()
        for row in rest:
            norm = normalize_name(row.raw_text)
            if norm and norm not in seen:
                seen.add(norm)
                names.append(norm)
        taxonomy = categorize_svc.taxonomy_paths(db)
        if names and taxonomy:
            try:
                guesses = await categorize_svc.classify_hierarchical(db, names, taxonomy)
                categorize_svc.apply_guesses(db, guesses, backend="shopping-list")
            except Exception as exc:  # no AI backend configured, quota, network…
                logger.warning("shopping list AI categorization failed: %s", exc)
                ai_error = str(exc)
            else:
                for row in rest:
                    if categorize_list_item_from_history(db, row):
                        by_ai += 1
                db.commit()
    return {"checked": len(rows), "categorized": by_dictionary + by_ai,
            "by_dictionary": by_dictionary, "by_ai": by_ai, "ai_error": ai_error}


async def run_auto_categorize(db: Session) -> dict | None:
    """Scheduled pass: classify the list once it has been quiet long enough.

    "Quiet" is measured from the most recently added pending row, so a burst of
    additions is classified once, `delay_minutes` after the last of them. The
    handled addition time is remembered, which keeps an idle list from being
    re-checked every minute. Returns None when there was nothing to do.
    """
    from app.services import event_log, shopping_settings

    config = shopping_settings.load(db)
    if not config["auto_categorize"]:
        return None
    rows = uncategorized_pending(db)
    if not rows:
        return None
    pending_rows = db.execute(select(ShoppingListItem).where(
        ShoppingListItem.status == ShoppingListStatus.pending,
    )).scalars().all()
    newest = max((as_utc(row.added_at) for row in pending_rows if row.added_at), default=None)
    if newest is None:
        return None
    delay = dt.timedelta(minutes=config["auto_categorize_delay_minutes"])
    if dt.datetime.now(dt.timezone.utc) - newest < delay:
        return None
    marker = shopping_settings.last_auto_run_marker(db)
    if marker and marker >= newest.isoformat():
        return None  # this batch of additions has already been classified

    result = await categorize_pending(db)
    shopping_settings.set_auto_run_marker(db, newest)
    event_log.record(
        db,
        source="shopping",
        level=event_log.WARNING if result["ai_error"] else event_log.INFO,
        title="Автоматична класифікація списку покупок",
        message=(f"Перевірено {result['checked']}, розкладено {result['categorized']} "
                 f"(словники {result['by_dictionary']}, AI {result['by_ai']})"
                 + (f"; AI недоступний: {result['ai_error']}" if result["ai_error"] else "")),
        details={**result, "delay_minutes": config["auto_categorize_delay_minutes"],
                 "quiet_since": newest.isoformat()},
    )
    return result


async def categorize_item_later(item_id: str) -> None:
    """Background pass for one freshly added row (used by the web/API create).

    Runs in its own session so the request can return before the AI answers.
    """
    from app.db import SessionLocal

    with SessionLocal() as db:
        row = db.get(ShoppingListItem, item_id)
        if row is None or row.category_id is not None:
            return
        await categorize_pending(db)


def store_has_category_history(db: Session, store_code: str, category_id: str) -> bool:
    """Check store availability by the category's top-level branch only."""
    category = db.get(Category, category_id)
    if category is None:
        return True
    root = category.path.split("/", 1)[0]
    store = db.execute(select(Store).where(Store.code == store_code)).scalar_one_or_none()
    # Grocery chains share most of the assortment. Use their combined history
    # for the top-level food branch, instead of penalising a chain with fewer
    # receipts in the local database.
    if store and store.network_type == "supermarket" and root != "produkty":
        return False
    if store and store.network_type == "supermarket" and root == "produkty":
        peer_stores = db.execute(select(Store.code).where(Store.network_type == "supermarket")).scalars().all()
        peer_receipts = db.execute(
            select(Receipt.id).join(ReceiptItem, ReceiptItem.receipt_id == Receipt.id)
            .join(Category, Category.id == ReceiptItem.category_id)
            .where(Receipt.store_code.in_(peer_stores),
                   (Category.path == root) | Category.path.like(f"{root}/%"))
        ).scalars().all()
        return len(set(peer_receipts)) >= 2
    evidence = db.execute(
        select(Receipt.id).join(ReceiptItem, ReceiptItem.receipt_id == Receipt.id)
        .join(Category, Category.id == ReceiptItem.category_id)
        .where(Receipt.store_code == store_code)
    ).scalars().all()
    if not evidence:
        return False
    receipt_ids = db.execute(
        select(Receipt.id).join(ReceiptItem, ReceiptItem.receipt_id == Receipt.id)
        .join(Category, Category.id == ReceiptItem.category_id)
        .where(Receipt.store_code == store_code,
               (Category.path == root) | Category.path.like(f"{root}/%"))
    ).scalars().all()
    return len(set(receipt_ids)) >= 2


def store_pending_item_count(db: Session, store_code: str) -> int:
    """Count current pending rows that belong in a store-level checklist."""
    rows = db.execute(select(ShoppingListItem).where(
        ShoppingListItem.status == ShoppingListStatus.pending
    )).scalars().all()
    count = 0
    for row in rows:
        categorize_list_item_from_history(db, row)
        if row.category_id and store_has_category_history(db, store_code, row.category_id):
            count += 1
    return count


def _unit_price(row) -> float:
    qty = float(row.quantity) if row.quantity else 1.0
    if row.unit_price is not None:
        return float(row.unit_price)
    return float(row.total_price) / qty if qty else float(row.total_price)


async def _batch_ai_matches(db: Session, list_rows: list[ShoppingListItem], receipt_items: list[ReceiptItem]) -> list[dict]:
    """Ask AI once for all unresolved rows of one receipt."""
    from app.services import ai

    cfg = ai.load_config(db)
    system = (
        f"{ai.prompt_catalog.get(db, 'json_response_rules')}\n\n"
        f"{ai.prompt_catalog.get(db, 'language_variants')}\n\n"
        f"{ai.prompt_catalog.get(db, 'shopping_match_rules')}\n\n"
        "Ты сопоставляешь список покупок с позициями одного чека. "
        "Верни только JSON без markdown: {\"matches\":[{\"list_id\":\"...\","
        "\"receipt_id\":\"...\",\"confidence\":0.0,\"reason\":\"...\"}]}. "
        "Не объединяй разные категории. Деревянные, шоколадные и куриные яйца — разные товары. "
        "Не создавай совпадений, если уверенность ниже 0.55. Каждая строка используется максимум один раз."
    )
    user = json.dumps({
        "shopping_list": [{"id": row.id, "name": row.raw_text, "category_id": row.category_id, "qty": float(row.qty or 1)} for row in list_rows],
        "receipt": [{"id": row.id, "name": row.name, "category_id": row.category_id, "qty": float(row.quantity or 1)} for row in receipt_items],
    }, ensure_ascii=False)
    raw = await ai.run_text(db, cfg, "normalize", system=system, user=user,
                            max_tokens=1200, prompt_preview="сопоставление списка с чеком")
    if isinstance(raw, dict):
        return raw.get("matches", []) if isinstance(raw.get("matches", []), list) else []
    try:
        return json.loads(raw).get("matches", [])
    except (TypeError, json.JSONDecodeError):
        return []


# --------------------------------------------------------------------------- #
# Auto-tick
# --------------------------------------------------------------------------- #
def match_receipt(db: Session, receipt: Receipt, *, use_ai: bool = True) -> list[ShoppingListItem]:
    """Match one receipt, auto-confirming strong links and storing weak ones.

    Historical repair jobs can disable the optional AI pass and remain fully
    deterministic when running offline.
    """
    pending = db.execute(
        select(ShoppingListItem).where(ShoppingListItem.status == ShoppingListStatus.pending)
    ).scalars().all()
    purchase_time = as_utc(receipt.purchased_at)
    pending = [row for row in pending if not row.added_at or not purchase_time
               or as_utc(row.added_at) <= purchase_time]
    if not pending:
        return []

    items = list(receipt.items)
    if any(it.id is None for it in items):
        db.flush()  # need real ids to record bought_receipt_item_id

    matched: list[ShoppingListItem] = []
    used_receipt_item_ids: set[str] = set()
    for li in pending:
        if li.added_at and receipt.purchased_at and as_utc(receipt.purchased_at) < as_utc(li.added_at):
            continue
        hit = None
        if li.barcode:
            hit = next(
                    (it for it in items if it.id not in used_receipt_item_ids
                     and it.barcode and it.barcode == li.barcode
                     and it.id != li.rejected_receipt_item_id),
                None,
            )
        method = "barcode" if hit is not None else None
        confidence = 1.0 if hit is not None else 0.0
        explanation = "Совпадает штрихкод"
        if hit is None:
            key = _list_key(li)
            hit = next(
                (
                    it for it in items
                    if it.id not in used_receipt_item_ids
                    and it.id != li.rejected_receipt_item_id
                    and names_match(key, normalize_name(it.name))
                ),
                None,
            )
            if hit is not None:
                method, confidence, explanation = "text", 0.96, "Совпадает название"
        if hit is None:
            list_key = canonical_product(li.raw_text)[0]
            hit = next((it for it in items if it.id not in used_receipt_item_ids
                        and it.id != li.rejected_receipt_item_id
                        and canonical_product(it.name)[0] == list_key), None)
            if hit is not None:
                method, confidence, explanation = "canonical", 0.91, "Совпадает товарная группа"
        if hit is None:
            continue
        existing = db.execute(select(ShoppingMatchCandidate).where(
            ShoppingMatchCandidate.shopping_list_item_id == li.id,
            ShoppingMatchCandidate.receipt_item_id == hit.id,
        )).scalar_one_or_none()
        if existing is None:
            candidate = ShoppingMatchCandidate(
                shopping_list_item_id=li.id, receipt_item_id=hit.id, receipt_id=receipt.id,
                confidence=confidence, method=method or "text", explanation=explanation,
                status=ShoppingMatchStatus.confirmed if confidence >= 0.9 else ShoppingMatchStatus.pending,
                resolved_at=dt.datetime.now(dt.timezone.utc) if confidence >= 0.9 else None,
            )
            db.add(candidate)
        if confidence >= 0.9:
            li.status = ShoppingListStatus.bought
            li.auto_matched = False
            li.bought_receipt_item_id = hit.id
            li.bought_at = as_utc(receipt.purchased_at) or dt.datetime.now(dt.timezone.utc)
        used_receipt_item_ids.add(hit.id)
        if confidence >= 0.9:
            matched.append(li)
    # One batched AI call handles all pairs the local matcher could not settle.
    if not use_ai:
        return matched
    unresolved_lists = [row for row in pending if row.status == ShoppingListStatus.pending]
    unresolved_receipts = [row for row in items if row.id not in used_receipt_item_ids]
    if unresolved_lists and unresolved_receipts:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            try:
                ai_matches = asyncio.run(asyncio.wait_for(
                    _batch_ai_matches(db, unresolved_lists, unresolved_receipts), timeout=8
                ))
            except Exception:  # noqa: BLE001 — receipt processing remains available without AI
                logger.warning("batch shopping match failed", exc_info=True)
                ai_matches = []
            used_ai_receipt_ids: set[str] = set()
            for match in ai_matches:
                list_id = str(match.get("list_id", ""))
                receipt_id = str(match.get("receipt_id", ""))
                confidence = float(match.get("confidence", 0) or 0)
                li = next((row for row in unresolved_lists if row.id == list_id), None)
                hit = next((row for row in unresolved_receipts if row.id == receipt_id), None)
                if not li or not hit or hit.id in used_ai_receipt_ids or confidence < 0.55:
                    continue
                if li.category_id and hit.category_id and li.category_id != hit.category_id:
                    continue
                exists = db.execute(select(ShoppingMatchCandidate).where(
                    ShoppingMatchCandidate.shopping_list_item_id == li.id,
                    ShoppingMatchCandidate.receipt_item_id == hit.id,
                )).scalar_one_or_none()
                if exists is not None:
                    continue
                candidate = ShoppingMatchCandidate(
                    shopping_list_item_id=li.id, receipt_item_id=hit.id, receipt_id=receipt.id,
                    confidence=min(confidence, 1), method="ai",
                    explanation=str(match.get("reason") or "AI сопоставил позиции чека и списка"),
                    status=ShoppingMatchStatus.confirmed if confidence >= 0.9 else ShoppingMatchStatus.pending,
                    resolved_at=dt.datetime.now(dt.timezone.utc) if confidence >= 0.9 else None,
                )
                db.add(candidate)
                used_ai_receipt_ids.add(hit.id)
                if confidence >= 0.9:
                    li.status = ShoppingListStatus.bought
                    li.bought_receipt_item_id = hit.id
                    li.bought_at = as_utc(receipt.purchased_at) or dt.datetime.now(dt.timezone.utc)
                    matched.append(li)
    return matched


def pending_match_candidates(db: Session) -> list[dict]:
    rows = db.execute(select(ShoppingMatchCandidate).where(
        ShoppingMatchCandidate.status == ShoppingMatchStatus.pending
    ).order_by(ShoppingMatchCandidate.confidence.desc(), ShoppingMatchCandidate.created_at.desc())).scalars().all()
    out = []
    for row in rows:
        list_item = db.get(ShoppingListItem, row.shopping_list_item_id)
        receipt_item = db.get(ReceiptItem, row.receipt_item_id)
        if not list_item or not receipt_item:
            continue
        out.append({"id": row.id, "shopping_list_item_id": row.shopping_list_item_id,
                    "receipt_item_id": row.receipt_item_id, "receipt_id": row.receipt_id,
                    "list_name": list_item.raw_text, "receipt_item_name": receipt_item.name,
                    "confidence": row.confidence, "method": row.method,
                    "explanation": row.explanation, "status": row.status,
                    "created_at": row.created_at})
    return out


def resolve_match_candidate(db: Session, candidate: ShoppingMatchCandidate, *, accept: bool) -> ShoppingListItem:
    list_item = db.get(ShoppingListItem, candidate.shopping_list_item_id)
    receipt_item = db.get(ReceiptItem, candidate.receipt_item_id)
    if not list_item or not receipt_item:
        raise ValueError("Кандидат більше не доступний")
    candidate.status = ShoppingMatchStatus.confirmed if accept else ShoppingMatchStatus.rejected
    candidate.resolved_at = dt.datetime.now(dt.timezone.utc)
    if accept:
        list_item.status = ShoppingListStatus.bought
        list_item.auto_matched = False
        list_item.bought_receipt_item_id = receipt_item.id
        receipt = db.get(Receipt, receipt_item.receipt_id)
        list_item.bought_at = as_utc(receipt.purchased_at) if receipt else dt.datetime.now(dt.timezone.utc)
    db.commit()
    db.refresh(list_item)
    return list_item


def rescan_pending(db: Session, *, days: int = _RESCAN_DAYS) -> list[ShoppingListItem]:
    """Re-run the matcher for pending rows and manual purchases without a
    receipt against recent receipts. A later receipt can therefore complete a
    manual move to «Куплено» and replace its transfer time with purchase time."""
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    receipts = db.execute(
        select(Receipt).where(Receipt.purchased_at >= since).order_by(Receipt.purchased_at.asc())
    ).scalars().all()
    matched: list[ShoppingListItem] = []
    for r in receipts:
        matched.extend(match_receipt(db, r))
        # SessionLocal is configured with autoflush disabled; make status changes
        # visible before the next receipt is matched, so one list row cannot be
        # counted repeatedly across several receipts in the same rescan.
        db.flush()
    if matched:
        db.commit()
    return matched


# --------------------------------------------------------------------------- #
# Store suggestion (from receipt history)
# --------------------------------------------------------------------------- #
class StoreCoverage:
    __slots__ = ("store_code", "store_name", "total_items", "covered_items",
                 "approx_basket", "last_purchase_at", "items")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}


def suggest_stores(db: Session) -> list[dict]:
    """Rank store chains for the current pending list. One row per chain that has
    covered at least one list item in the past, sorted by coverage desc then
    cheapest approximate basket."""
    pending = db.execute(
        select(ShoppingListItem).where(ShoppingListItem.status == ShoppingListStatus.pending)
    ).scalars().all()
    wanted = [(li, _list_key(li)) for li in pending]
    wanted = [(li, key) for li, key in wanted if key]
    if not wanted:
        return []

    rows = db.execute(
        select(
            ReceiptItem.name,
            ReceiptItem.unit,
            ReceiptItem.unit_price,
            ReceiptItem.total_price,
            ReceiptItem.quantity,
            Receipt.store_code,
            Receipt.purchased_at,
        ).join(Receipt, Receipt.id == ReceiptItem.receipt_id)
    ).all()

    by_store: dict[str, list[tuple[str, object, dt.datetime]]] = defaultdict(list)
    for r in rows:
        by_store[r.store_code].append((normalize_name(r.name), r, as_utc(r.purchased_at)))

    store_names = {s.code: s.name for s in db.execute(select(Store)).scalars().all()}

    out: list[dict] = []
    for code, entries in by_store.items():
        covered = 0
        basket = 0.0
        last_seen: dt.datetime | None = None
        item_details: list[dict] = []
        for li, key in wanted:
            best: tuple[dt.datetime, float, str | None] | None = None
            for en_norm, r, d in entries:
                if names_match(key, en_norm):
                    if best is None or (d is not None and d > best[0]):
                        best = (d, _unit_price(r), r.unit)
            if best is not None:
                covered += 1
                qty = float(li.qty) if li.qty else 1.0
                basket += best[1] * qty
                if last_seen is None or (best[0] is not None and best[0] > last_seen):
                    last_seen = best[0]
                item_details.append({
                    "raw_text": li.raw_text,
                    "last_price": round(best[1], 2),
                    "unit": best[2],
                    "last_seen_at": best[0],
                })
            else:
                item_details.append({
                    "raw_text": li.raw_text,
                    "last_price": None,
                    "unit": None,
                    "last_seen_at": None,
                })
        if covered == 0:
            continue
        out.append(StoreCoverage(
            store_code=code,
            store_name=store_names.get(code, code),
            total_items=len(wanted),
            covered_items=covered,
            approx_basket=round(basket, 2),
            last_purchase_at=last_seen,
            items=item_details,
        ).as_dict())

    out.sort(key=lambda s: (-s["covered_items"], s["approx_basket"]))
    return out


# --------------------------------------------------------------------------- #
# AI: free phrase → per-store catalogue query (cached in product_aliases)
# --------------------------------------------------------------------------- #
async def resolve_query(db: Session, phrase: str, store_code: str) -> tuple[str, str]:
    """Return (query, source) where source is "cache" | "ai" | "fallback".

    A (normalized_phrase, store_code) pair is resolved through the AI backend at
    most once, ever — the result is cached in `product_aliases`. When no AI
    backend is usable the raw phrase is returned and *not* cached, so it will be
    retried once AI is switched on.
    """
    phrase = phrase.strip()
    norm = normalize_name(phrase)
    if not norm:
        return phrase, "fallback"

    row = db.get(ProductAlias, (norm, store_code))
    if row is not None:
        return row.query, "cache"

    from app.services import ai  # local import — avoids a service import cycle

    try:
        query = (await ai.normalize_query(db, phrase, store_code) or phrase).strip()
    except ai.AiConfigError:
        return phrase, "fallback"
    except Exception:  # noqa: BLE001 — never let a model hiccup break suggestions
        logger.warning("normalize_query failed for %r @ %s", phrase, store_code, exc_info=True)
        return phrase, "fallback"

    db.add(ProductAlias(normalized_phrase=norm, store_code=store_code, query=query))
    db.commit()
    return query, "ai"


# --------------------------------------------------------------------------- #
# Store suggestion — live catalogue (ROADMAP.md "Уточнення наживо")
# --------------------------------------------------------------------------- #
async def suggest_stores_live(db: Session) -> list[dict]:
    """Like `suggest_stores` but prices the current pending list against each
    store's live catalogue search (one account per catalog-search-capable
    store), resolving each phrase to a store-specific query via `resolve_query`.
    """
    from app.adapters.registry import get_adapter
    from app.api.products import _search_one_account

    pending = db.execute(
        select(ShoppingListItem).where(ShoppingListItem.status == ShoppingListStatus.pending)
    ).scalars().all()
    wanted = [li for li in pending if _list_key(li)]
    if not wanted:
        return []

    one_per_store: dict[str, Account] = {}
    for acc in db.execute(select(Account)).scalars().all():
        if get_adapter(acc.store.code).capabilities.get("catalog_search"):
            one_per_store.setdefault(acc.store.code, acc)

    async def _for_store(code: str, acc: Account) -> dict | None:
        covered = 0
        basket = 0.0
        details: list[dict] = []
        for li in wanted:
            query, source = await resolve_query(db, li.raw_text, code)
            try:
                hits = await _search_one_account(db, acc, query)
            except Exception:  # noqa: BLE001
                hits = []
            best = min(
                (h for h in hits if h.in_stock is not False),
                key=lambda h: h.price,
                default=None,
            )
            if best is not None:
                covered += 1
                basket += float(best.price) * (float(li.qty) if li.qty else 1.0)
            details.append({
                "raw_text": li.raw_text,
                "query": query,
                "query_source": source,
                "matched_name": best.name if best else None,
                "last_price": round(float(best.price), 2) if best else None,
                "product_url": best.product_url if best else None,
            })
        if covered == 0:
            return None
        return {
            "store_code": code,
            "store_name": acc.store.name,
            "total_items": len(wanted),
            "covered_items": covered,
            "approx_basket": round(basket, 2),
            "items": details,
        }

    results = await asyncio.gather(*[_for_store(c, a) for c, a in one_per_store.items()])
    out = [r for r in results if r is not None]
    out.sort(key=lambda s: (-s["covered_items"], s["approx_basket"]))
    return out
