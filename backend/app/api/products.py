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
from app.db import SessionLocal, get_db
from app.models.models import Account, AccountStatus, Receipt, ReceiptItem, Store
from app.schemas.schemas import LiveProductOut, ProductPriceOut, ProductReceiptOut
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
            ReceiptItem.product_image_url,
            Receipt.store_code,
            Receipt.purchased_at,
            Receipt.id,
            Receipt.total_amount,
            Receipt.currency,
            Receipt.exchange_rate_to_uah,
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
    buckets: dict[
        tuple[str, str, str],
        list[tuple[dt.datetime, float, str | None, str | None, str, float, str, float | None]],
    ] = defaultdict(list)
    for r in rows:
        qty = float(r.quantity) if r.quantity else 1.0
        price = float(r.unit_price) if r.unit_price is not None else float(r.total_price) / qty
        currency = r.currency or "UAH"
        buckets[(r.name, r.store_code, currency)].append(
            (
                as_utc(r.purchased_at),
                price,
                r.unit,
                r.product_image_url,
                r.id,
                float(r.total_amount),
                currency,
                float(r.exchange_rate_to_uah) if r.exchange_rate_to_uah is not None else None,
            )
        )

    out: list[ProductPriceOut] = []
    for (name, store_code, currency), entries in buckets.items():
        entries.sort(key=lambda e: e[0])
        prices = [p for _, p, _, _, _, _, _, _ in entries]
        last_at, last_price, last_unit, _, last_receipt_id, _, _, last_rate = entries[-1]
        image_url = next((image for _, _, _, image, _, _, _, _ in reversed(entries) if image), None)
        prices_uah = [
            price * (rate if rate is not None else 1.0)
            for _, price, _, _, _, _, row_currency, rate in entries
            if row_currency == "UAH" or rate is not None
        ]
        receipt_rows: list[ProductReceiptOut] = []
        seen_receipts: set[str] = set()
        for purchased_at, _, _, _, receipt_id, total_amount, receipt_currency, rate in reversed(entries):
            if receipt_id in seen_receipts:
                continue
            seen_receipts.add(receipt_id)
            receipt_rows.append(
                ProductReceiptOut(
                    receipt_id=receipt_id,
                    purchased_at=purchased_at,
                    total_amount=total_amount,
                    currency=receipt_currency or "UAH",
                    total_amount_uah=(total_amount * (rate if rate is not None else 1.0))
                    if receipt_currency == "UAH" or rate is not None
                    else None,
                )
            )
        out.append(
            ProductPriceOut(
                name=name,
                store_code=store_code,
                store_name=store_names.get(store_code, store_code),
                unit=last_unit,
                currency=currency,
                image_url=image_url,
                last_price=last_price,
                last_purchased_at=last_at,
                min_price=min(prices),
                max_price=max(prices),
                avg_price=sum(prices) / len(prices),
                times_bought=len(entries),
                last_receipt_id=last_receipt_id,
                receipts=receipt_rows,
                last_price_uah=(last_price * (last_rate if last_rate is not None else 1.0))
                if currency == "UAH" or last_rate is not None
                else None,
                min_price_uah=min(prices_uah) if prices_uah else None,
                max_price_uah=max(prices_uah) if prices_uah else None,
                avg_price_uah=sum(prices_uah) / len(prices_uah) if prices_uah else None,
                exchange_rate_to_uah=last_rate,
            )
        )

    out.sort(key=lambda p: (p.name, p.last_price))
    return out


async def _search_one_account(account_id: str, q: str) -> list[LiveProductOut]:
    """Search one account with a session owned by this coroutine.

    ``live_search`` fans out to several stores.  A SQLAlchemy Session is not
    safe to share between those coroutines, especially while refreshing tokens.
    """
    with SessionLocal() as db:
        account = db.get(Account, account_id)
        if account is None:
            return []
        adapter = get_adapter(account.store.code)
        store_code = account.store.code
        store_name = account.store.name
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
    # Do not keep a database session/connection open while waiting on a store.
    try:
        products = await adapter.search_catalog(access_token, extra, q)
    except (NotImplementedYet, AdapterError):
        return []
    return [
        LiveProductOut(
            store_code=store_code,
            store_name=store_name,
            name=p.name,
            price=p.price,
            currency=p.currency,
            unit=p.unit,
            brand=p.brand,
            image_url=p.image_url,
            product_url=p.product_url,
            in_stock=p.in_stock,
            seller_name=p.seller_name,
            seller_phone=p.seller_phone,
        )
        for p in products
    ]


async def _search_public_store(store: Store, q: str) -> list[LiveProductOut]:
    """Search a catalog that deliberately does not require an account."""
    adapter = get_adapter(store.code)
    try:
        products = await adapter.search_catalog(None, {}, q)
    except (NotImplementedYet, AdapterError):
        return []
    return [
        LiveProductOut(
            store_code=store.code,
            store_name=store.name,
            name=p.name,
            price=p.price,
            currency=p.currency,
            unit=p.unit,
            brand=p.brand,
            image_url=p.image_url,
            product_url=p.product_url,
            in_stock=p.in_stock,
            seller_name=p.seller_name,
            seller_phone=p.seller_phone,
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

    public_stores = [
        store for store in db.execute(select(Store)).scalars().all()
        if store.code not in one_per_store
        and (wanted is None or store.code in wanted)
        and not get_adapter(store.code).catalog_search_requires_auth
    ]
    results = await asyncio.gather(
        *[_search_one_account(a.id, q) for a in one_per_store.values()],
        *[_search_public_store(store, q) for store in public_stores],
    )
    out: list[LiveProductOut] = [item for sublist in results for item in sublist]
    out.sort(key=lambda p: p.price)
    return out
