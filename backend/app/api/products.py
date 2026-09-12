"""Product price search across all stores, built from ReceiptItem history.

There's no shared product catalog between chains — every store names the same
item its own way — so this is a plain substring search over item names actually
seen on past receipts, grouped by (name, store). Meant to answer "who sells X
and for how much", and to become the data source for a shopping-list-vs-store
optimizer later (see SERVICE.md "Що не зроблено").
"""
from __future__ import annotations

import asyncio
import datetime as dt
from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.base import AdapterError, NotImplementedYet
from app.adapters.registry import get_adapter
from app.core.time import as_utc
from app.db import get_db
from app.models.models import Account, AccountStatus, Receipt, ReceiptItem, Store
from app.schemas.schemas import LiveProductOut, ProductPriceOut
from app.services.collector import ensure_fresh_token

router = APIRouter(prefix="/api/products", tags=["products"])

_MIN_QUERY_LEN = 2
_MAX_ROWS = 5000  # safety cap on raw rows scanned per search — plenty for this dataset's size


@router.get("/search", response_model=list[ProductPriceOut])
def search_products(q: str, db: Session = Depends(get_db)):
    q = q.strip()
    if len(q) < _MIN_QUERY_LEN:
        return []
    pattern = f"%{q}%"

    rows = db.execute(
        select(
            ReceiptItem.name,
            ReceiptItem.unit,
            ReceiptItem.unit_price,
            ReceiptItem.total_price,
            ReceiptItem.quantity,
            Receipt.store_code,
            Receipt.purchased_at,
        )
        .join(Receipt, Receipt.id == ReceiptItem.receipt_id)
        .where(ReceiptItem.name.ilike(pattern))
        .limit(_MAX_ROWS)
    ).all()

    store_names = {s.code: s.name for s in db.execute(select(Store)).scalars().all()}

    # Group in Python rather than SQL: picking the "price at the most recent
    # purchase" per (name, store) is a correlated aggregate SQLite/SQLAlchemy
    # can't express in one clean GROUP BY, and this dataset is small enough
    # (a few thousand rows at most) that it's not worth a subquery dance.
    buckets: dict[tuple[str, str], list[tuple[dt.datetime, float, str | None]]] = defaultdict(list)
    for r in rows:
        qty = float(r.quantity) if r.quantity else 1.0
        price = float(r.unit_price) if r.unit_price is not None else float(r.total_price) / qty
        buckets[(r.name, r.store_code)].append((as_utc(r.purchased_at), price, r.unit))

    out: list[ProductPriceOut] = []
    for (name, store_code), entries in buckets.items():
        entries.sort(key=lambda e: e[0])
        prices = [p for _, p, _ in entries]
        last_at, last_price, last_unit = entries[-1]
        out.append(
            ProductPriceOut(
                name=name,
                store_code=store_code,
                store_name=store_names.get(store_code, store_code),
                unit=last_unit,
                last_price=last_price,
                last_purchased_at=last_at,
                min_price=min(prices),
                max_price=max(prices),
                avg_price=sum(prices) / len(prices),
                times_bought=len(entries),
            )
        )

    out.sort(key=lambda p: (p.name, p.last_price))
    return out


async def _search_one_account(db: Session, account: Account, q: str) -> list[LiveProductOut]:
    adapter = get_adapter(account.store.code)
    access_token: str | None = None
    extra: dict = {}
    if account.credential is not None and account.credential.access_token_enc:
        extra = dict(account.credential.extra or {})
        try:
            access_token = await ensure_fresh_token(db, account, adapter)
        except AdapterError:
            # Couldn't refresh — still worth trying search_catalog with no token,
            # some stores (ATB's multisearch.io widget) don't need one at all.
            access_token = None
    try:
        products = await adapter.search_catalog(access_token, extra, q)
    except (NotImplementedYet, AdapterError):
        return []
    return [
        LiveProductOut(
            store_code=account.store.code,
            store_name=account.store.name,
            name=p.name,
            price=p.price,
            currency=p.currency,
            unit=p.unit,
            brand=p.brand,
            image_url=p.image_url,
            product_url=p.product_url,
            in_stock=p.in_stock,
        )
        for p in products
    ]


@router.get("/live-search", response_model=list[LiveProductOut])
async def live_search(q: str, stores: str | None = None, db: Session = Depends(get_db)):
    """Queries each store's own live catalog-search API right now — not receipt
    history. One account per store (see `one_per_store` below): if you have two
    Fora accounts, only one of them gets asked, since the catalog/price is the
    same regardless of which account you'd actually shop with. Best-effort per
    store (same pattern as get_balance/get_fuel_prices): a store whose adapter
    doesn't implement search_catalog (most, currently — only ATB so far, see
    adapters/atb.py) or whose live call fails is silently skipped rather than
    failing the whole search.

    `stores`, if given, is a comma-separated list of store codes to restrict the
    search to — the frontend's "search in these shops" picker. Unknown or
    ineligible codes are simply ignored; an empty resulting set means no stores
    are queried."""
    q = q.strip()
    if len(q) < _MIN_QUERY_LEN:
        return []

    wanted: set[str] | None = None
    if stores is not None:
        wanted = {c.strip() for c in stores.split(",") if c.strip()}
        if not wanted:
            return []

    # Public catalogs (Vencon) remain searchable even while an account is
    # awaiting login. Authenticated catalogs are queried only for active accounts.
    accounts = db.execute(select(Account)).scalars().all()
    one_per_store: dict[str, Account] = {}
    for a in accounts:
        if wanted is not None and a.store.code not in wanted:
            continue
        if a.status != AccountStatus.active and get_adapter(a.store.code).catalog_search_requires_auth:
            continue
        one_per_store.setdefault(a.store.code, a)

    results = await asyncio.gather(*[_search_one_account(db, a, q) for a in one_per_store.values()])
    out: list[LiveProductOut] = [item for sublist in results for item in sublist]
    out.sort(key=lambda p: p.price)
    return out
