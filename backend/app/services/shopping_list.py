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
import logging
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import as_utc
from app.models.models import Account, ProductCategory, Receipt, ReceiptItem, Store
from app.models.shopping import ProductAlias, ShoppingListItem, ShoppingListStatus
from app.services.text import normalize_name

logger = logging.getLogger(__name__)

# Jaccard threshold for "these two normalized names are the same product" when
# neither is a clean subset of the other.
_OVERLAP_THRESHOLD = 0.6
# How far back `rescan` / a freshly added item looks through receipt history.
_RESCAN_DAYS = 60


def names_match(list_norm: str, item_norm: str) -> bool:
    if not list_norm or not item_norm:
        return False
    if list_norm == item_norm:
        return True
    a = set(list_norm.split())
    b = set(item_norm.split())
    inter = a & b
    if not inter:
        return False
    if a <= b:  # every word of the list entry appears in the receipt line
        return True
    return len(inter) / len(a | b) >= _OVERLAP_THRESHOLD


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


def _unit_price(row) -> float:
    qty = float(row.quantity) if row.quantity else 1.0
    if row.unit_price is not None:
        return float(row.unit_price)
    return float(row.total_price) / qty if qty else float(row.total_price)


# --------------------------------------------------------------------------- #
# Auto-tick
# --------------------------------------------------------------------------- #
def match_receipt(db: Session, receipt: Receipt) -> list[ShoppingListItem]:
    """Match one just-saved receipt against every pending list row. Returns the
    rows that were flipped to `bought` (candidates). Caller commits."""
    pending = db.execute(
        select(ShoppingListItem).where(ShoppingListItem.status == ShoppingListStatus.pending)
    ).scalars().all()
    if not pending:
        return []

    items = list(receipt.items)
    if any(it.id is None for it in items):
        db.flush()  # need real ids to record bought_receipt_item_id

    matched: list[ShoppingListItem] = []
    for li in pending:
        hit = None
        if li.barcode:
            hit = next(
                (it for it in items if it.barcode and it.barcode == li.barcode and it.id != li.rejected_receipt_item_id),
                None,
            )
        if hit is None:
            key = _list_key(li)
            hit = next(
                (
                    it for it in items
                    if it.id != li.rejected_receipt_item_id
                    and names_match(key, normalize_name(it.name))
                ),
                None,
            )
        if hit is None:
            continue
        li.status = ShoppingListStatus.bought
        li.auto_matched = True
        li.bought_receipt_item_id = hit.id
        li.bought_at = as_utc(receipt.purchased_at) or dt.datetime.now(dt.timezone.utc)
        matched.append(li)
    return matched


def rescan_pending(db: Session, *, days: int = _RESCAN_DAYS) -> list[ShoppingListItem]:
    """Re-run the matcher for all pending rows against recent receipts — for a
    list edited after the shopping trip, or items added once receipts already
    landed. Commits."""
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    receipts = db.execute(
        select(Receipt).where(Receipt.purchased_at >= since).order_by(Receipt.purchased_at.asc())
    ).scalars().all()
    matched: list[ShoppingListItem] = []
    for r in receipts:
        matched.extend(match_receipt(db, r))
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
