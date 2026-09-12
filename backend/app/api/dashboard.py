from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.time import as_utc
from app.db import get_db
from app.models.models import (
    Account,
    AccountStatus,
    Category,
    CollectionRun,
    FuelPriceSnapshot,
    Receipt,
    ReceiptItem,
    Store,
)
from app.schemas.schemas import (
    AccountFuelPricesOut,
    CategorySpend,
    ChannelBreakdown,
    DashboardStats,
    FuelPriceHistoryOut,
    StoreBreakdown,
    StoreCategoryCell,
    StoreCategoryItem,
    StoreCategoryMatrix,
    StoreCategoryRow,
    TimeseriesPoint,
)
from app.services.fuel_prices import read_fuel_prices
from app.services.receipt_visibility import visible_receipt_clause

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def _date_filters(date_from: dt.datetime | None, date_to: dt.datetime | None) -> list:
    clauses = [visible_receipt_clause()]
    if date_from:
        clauses.append(Receipt.purchased_at >= date_from)
    if date_to:
        clauses.append(Receipt.purchased_at <= date_to)
    return clauses


def _uah_rate():
    return case(
        (Receipt.currency == "UAH", 1),
        else_=func.coalesce(Receipt.exchange_rate_to_uah, 0),
    )


def _uah_amount(column):
    return column * _uah_rate()


def _receipt_spend(include_discount: bool):
    """Receipt spend in UAH. `include_discount=True` (default) is what was
    actually paid; `False` adds the discount back, i.e. the pre-discount price."""
    col = Receipt.total_amount
    if not include_discount:
        col = col + func.coalesce(Receipt.total_discount, 0)
    return _uah_amount(col)


def _item_spend(include_discount: bool):
    """Same as `_receipt_spend`, per receipt line (`total_price` is already net)."""
    col = ReceiptItem.total_price
    if not include_discount:
        col = col + func.coalesce(ReceiptItem.discount, 0)
    return _uah_amount(col)


@router.get("", response_model=DashboardStats)
def get_stats(
    db: Session = Depends(get_db),
    date_from: dt.datetime | None = None,
    date_to: dt.datetime | None = None,
    include_discount: bool = True,
):
    clauses = _date_filters(date_from, date_to)

    def agg(col):
        return db.execute(select(func.coalesce(func.sum(col), 0)).where(*clauses)).scalar_one()

    total_receipts = db.execute(select(func.count()).select_from(Receipt).where(*clauses)).scalar_one()
    total_spent = agg(_receipt_spend(include_discount))
    total_discount = agg(_uah_amount(Receipt.total_discount))
    total_bonuses = agg(_uah_amount(Receipt.bonuses))
    total_bonuses_used = agg(_uah_amount(Receipt.bonuses_used))

    # Account status/last-run are "now" facts, not scoped to the selected date range.
    active = db.execute(
        select(func.count()).select_from(Account).where(Account.status == AccountStatus.active)
    ).scalar_one()
    needs_attention = db.execute(
        select(func.count())
        .select_from(Account)
        .where(Account.status.in_([AccountStatus.needs_reauth, AccountStatus.error]))
    ).scalar_one()
    last_run = db.execute(select(func.max(CollectionRun.started_at))).scalar_one()

    return DashboardStats(
        total_receipts=total_receipts,
        total_spent=float(total_spent),
        total_discount=float(total_discount),
        total_bonuses=float(total_bonuses),
        total_bonuses_used=float(total_bonuses_used),
        accounts_active=active,
        accounts_needs_attention=needs_attention,
        last_run_at=last_run,
    )


@router.get("/by-store", response_model=list[StoreBreakdown])
def by_store(
    db: Session = Depends(get_db),
    date_from: dt.datetime | None = None,
    date_to: dt.datetime | None = None,
    include_discount: bool = True,
):
    clauses = _date_filters(date_from, date_to)
    stmt = (
        select(
            Receipt.store_code,
            func.count().label("receipt_count"),
            func.coalesce(func.sum(_receipt_spend(include_discount)), 0).label("total_spent"),
            func.coalesce(func.sum(_uah_amount(Receipt.total_discount)), 0).label("total_discount"),
            func.coalesce(func.sum(_uah_amount(Receipt.bonuses)), 0).label("total_bonuses"),
        )
        .where(*clauses)
        .group_by(Receipt.store_code)
    )
    rows = db.execute(stmt).all()
    store_names = {s.code: s.name for s in db.execute(select(Store)).scalars().all()}

    breakdown = [
        StoreBreakdown(
            store_code=r.store_code,
            store_name=store_names.get(r.store_code, r.store_code),
            receipt_count=r.receipt_count,
            total_spent=float(r.total_spent),
            total_discount=float(r.total_discount),
            total_bonuses=float(r.total_bonuses),
        )
        for r in rows
    ]
    breakdown.sort(key=lambda b: b.total_spent, reverse=True)
    return breakdown


@router.get("/by-channel", response_model=list[ChannelBreakdown])
def by_channel(
    db: Session = Depends(get_db),
    date_from: dt.datetime | None = None,
    date_to: dt.datetime | None = None,
    include_discount: bool = True,
):
    clauses = _date_filters(date_from, date_to)
    stmt = (
        select(
            Receipt.channel,
            func.count().label("receipt_count"),
            func.coalesce(func.sum(_receipt_spend(include_discount)), 0).label("total_spent"),
        )
        .where(*clauses)
        .group_by(Receipt.channel)
    )
    rows = db.execute(stmt).all()
    return [
        ChannelBreakdown(channel=r.channel, receipt_count=r.receipt_count, total_spent=float(r.total_spent))
        for r in rows
    ]


@router.get("/by-category", response_model=list[CategorySpend])
def by_category(
    db: Session = Depends(get_db),
    date_from: dt.datetime | None = None,
    date_to: dt.datetime | None = None,
    include_discount: bool = True,
):
    """Spend per category branch. `direct_total` is items tagged exactly that
    node; `rollup_total` folds in every descendant. Uncategorised items land in
    a synthetic row with `category_id = None`."""
    clauses = _date_filters(date_from, date_to)
    net = _item_spend(include_discount)

    rows = db.execute(
        select(
            ReceiptItem.category_id,
            func.coalesce(func.sum(net), 0).label("total"),
            func.count().label("n"),
        )
        .join(Receipt, Receipt.id == ReceiptItem.receipt_id)
        .where(*clauses)
        .group_by(ReceiptItem.category_id)
    ).all()
    direct = {r.category_id: (float(r.total), r.n) for r in rows}

    cats = db.execute(select(Category).order_by(Category.path)).scalars().all()
    out: list[CategorySpend] = []
    for cat in cats:
        d_total, d_count = direct.get(cat.id, (0.0, 0))
        roll_total, roll_count = d_total, d_count
        for other in cats:
            if other.path.startswith(cat.path + "/"):
                o_total, o_count = direct.get(other.id, (0.0, 0))
                roll_total += o_total
                roll_count += o_count
        out.append(CategorySpend(
            category_id=cat.id,
            path=cat.path,
            name=cat.name,
            depth=cat.path.count("/"),
            direct_total=round(d_total, 2),
            rollup_total=round(roll_total, 2),
            item_count=roll_count,
        ))

    uncat_total, uncat_count = direct.get(None, (0.0, 0))
    if uncat_count:
        out.append(CategorySpend(
            category_id=None, path="", name="Без категорії", depth=0,
            direct_total=round(uncat_total, 2), rollup_total=round(uncat_total, 2),
            item_count=uncat_count,
        ))
    return out


@router.get("/store-category", response_model=StoreCategoryMatrix)
def store_category(
    db: Session = Depends(get_db),
    date_from: dt.datetime | None = None,
    date_to: dt.datetime | None = None,
    include_discount: bool = True,
):
    """Cross-tab of net spend by store × category branch — answers "which stores
    did I buy <category> at" and "what categories did I buy at <store>" at once.
    Each cell is a rollup (the branch plus all its descendants). Uncategorised
    items get a `category_id = None` row."""
    clauses = _date_filters(date_from, date_to)
    net = _item_spend(include_discount)

    rows = db.execute(
        select(
            Receipt.store_code,
            ReceiptItem.category_id,
            func.coalesce(func.sum(net), 0).label("total"),
            func.count().label("n"),
        )
        .join(Receipt, Receipt.id == ReceiptItem.receipt_id)
        .where(*clauses)
        .group_by(Receipt.store_code, ReceiptItem.category_id)
    ).all()

    # direct[(store, category_id)] -> (total, count)
    direct: dict[tuple[str, str | None], tuple[float, int]] = {
        (r.store_code, r.category_id): (float(r.total), r.n) for r in rows
    }
    store_codes = sorted({r.store_code for r in rows})
    store_names = {s.code: s.name for s in db.execute(select(Store)).scalars().all()}

    cats = db.execute(select(Category).order_by(Category.path)).scalars().all()
    children: dict[str, list[str]] = {}
    for cat in cats:
        children.setdefault(cat.id, [])
        for other in cats:
            if other.path.startswith(cat.path + "/"):
                children[cat.id].append(other.id)

    store_totals: dict[str, float] = {sc: 0.0 for sc in store_codes}
    category_rows: list[StoreCategoryRow] = []

    def _build_row(cat_id: str | None, path: str, name: str, depth: int, ids: list[str]):
        cells: list[StoreCategoryCell] = []
        row_total = 0.0
        row_count = 0
        for sc in store_codes:
            c_total = c_count = 0
            for cid in ids:
                t, n = direct.get((sc, cid), (0.0, 0))
                c_total += t
                c_count += n
            if c_count:
                cells.append(StoreCategoryCell(store_code=sc, total=round(c_total, 2), item_count=c_count))
            row_total += c_total
            row_count += c_count
        return StoreCategoryRow(
            category_id=cat_id, path=path, name=name, depth=depth,
            total=round(row_total, 2), item_count=row_count, per_store=cells,
        )

    for cat in cats:
        category_rows.append(_build_row(
            cat.id, cat.path, cat.name, cat.path.count("/"),
            [cat.id, *children[cat.id]],
        ))

    if any(cid is None for (_sc, cid) in direct):
        category_rows.append(_build_row(None, "", "Без категорії", 0, [None]))

    for (sc, _cid), (t, _n) in direct.items():
        store_totals[sc] += t
    br = by_store(db=db, date_from=date_from, date_to=date_to, include_discount=include_discount)
    receipts_by_store = {b.store_code: b.receipt_count for b in br}

    stores = [
        StoreBreakdown(
            store_code=sc,
            store_name=store_names.get(sc, sc),
            receipt_count=receipts_by_store.get(sc, 0),
            total_spent=round(store_totals[sc], 2),
            total_discount=0.0,
            total_bonuses=0.0,
        )
        for sc in store_codes
    ]
    stores.sort(key=lambda s: s.total_spent, reverse=True)

    return StoreCategoryMatrix(stores=stores, categories=category_rows)


@router.get("/store-category/items", response_model=list[StoreCategoryItem])
def store_category_items(
    store_code: str,
    db: Session = Depends(get_db),
    category_id: str | None = None,
    date_from: dt.datetime | None = None,
    date_to: dt.datetime | None = None,
    include_discount: bool = True,
):
    """The receipt lines behind one store×category cell — what exactly was bought
    at `store_code` in `category_id` (that branch plus its descendants) over the
    range. `category_id="none"` → the uncategorised bucket; omitted → every line
    at that store."""
    clauses = _date_filters(date_from, date_to)
    clauses.append(Receipt.store_code == store_code)

    if category_id == "none":
        clauses.append(ReceiptItem.category_id.is_(None))
    elif category_id:
        node = db.get(Category, category_id)
        if node is None:
            raise HTTPException(status_code=404, detail="Категорію не знайдено")
        branch_ids = [category_id, *db.execute(
            select(Category.id).where(Category.path.like(f"{node.path}/%"))
        ).scalars().all()]
        clauses.append(ReceiptItem.category_id.in_(branch_ids))

    rows = db.execute(
        select(ReceiptItem, Receipt.purchased_at, Receipt.store_code, Category.path, _uah_rate().label("rate"))
        .join(Receipt, Receipt.id == ReceiptItem.receipt_id)
        .join(Category, Category.id == ReceiptItem.category_id, isouter=True)
        .where(*clauses)
        .order_by(Receipt.purchased_at.desc())
    ).all()

    def _line_total(item, rate: float) -> float:
        gross_add = 0.0 if include_discount else float(item.discount or 0)
        return round((float(item.total_price) + gross_add) * rate, 2)

    return [
        StoreCategoryItem(
            receipt_id=item.receipt_id,
            item_id=item.id,
            name=item.name,
            quantity=item.quantity,
            unit=item.unit,
            unit_price=round(float(item.unit_price) * float(rate), 2) if item.unit_price is not None else None,
            total_price=_line_total(item, float(rate)),
            discount=round(float(item.discount) * float(rate), 2),
            net=_line_total(item, float(rate)),
            purchased_at=purchased_at,
            store_code=store_code_,
            category_id=item.category_id,
            category_path=path,
        )
        for item, purchased_at, store_code_, path, rate in rows
    ]


@router.get("/timeseries", response_model=list[TimeseriesPoint])
def timeseries(
    db: Session = Depends(get_db),
    date_from: dt.datetime | None = None,
    date_to: dt.datetime | None = None,
    bucket: str = "auto",
    include_discount: bool = True,
):
    """`bucket`: "day" | "month" | "year" | "auto" (picks day/month/year off the
    span between date_from/date_to — falls back to the receipts' own min/max
    when either bound is missing, so "весь час" — 9 years of history in this
    project's own data — doesn't render as ~3000 daily bars)."""
    clauses = _date_filters(date_from, date_to)

    if bucket == "auto":
        # date_from/date_to come in tz-aware (the frontend sends .toISOString());
        # SQLite doesn't persist tzinfo, so anything read back from the DB (the
        # min/max fallback below) comes back naive — as_utc() both sides before
        # subtracting or this throws (see app/core/time.py).
        lo = as_utc(date_from)
        hi = as_utc(date_to)
        if lo is None or hi is None:
            row = db.execute(
                select(func.min(Receipt.purchased_at), func.max(Receipt.purchased_at)).where(visible_receipt_clause())
            ).one()
            lo = lo or as_utc(row[0])
            hi = hi or as_utc(row[1])
        span_days = (hi - lo).days if (lo and hi) else 0
        bucket = "day" if span_days <= 60 else "month" if span_days <= 900 else "year"

    fmt = {"day": "%Y-%m-%d", "month": "%Y-%m", "year": "%Y"}.get(bucket, "%Y-%m")
    period = func.strftime(fmt, Receipt.purchased_at).label("period")

    stmt = (
        select(
            period,
            func.coalesce(func.sum(_receipt_spend(include_discount)), 0).label("total_spent"),
            func.count().label("receipt_count"),
        )
        .where(*clauses)
        .group_by(period)
        .order_by(period)
    )
    rows = db.execute(stmt).all()
    return [
        TimeseriesPoint(period=r.period, total_spent=float(r.total_spent), receipt_count=r.receipt_count)
        for r in rows
    ]


@router.get("/fuel-prices", response_model=list[AccountFuelPricesOut])
def fuel_prices(db: Session = Depends(get_db)):
    """Cached board prices, read straight from stored snapshots — never hits a
    station API. A background job refreshes them 3×/day (app/scheduler.py,
    `refresh_all_fuel_prices`); see services/fuel_prices.py::read_fuel_prices."""
    return read_fuel_prices(db)


@router.get("/fuel-prices/history", response_model=list[FuelPriceHistoryOut])
def fuel_price_history(
    db: Session = Depends(get_db),
    account_id: str | None = None,
    date_from: dt.datetime | None = None,
    date_to: dt.datetime | None = None,
    limit: int = 2000,
):
    """Historical station price samples, newest first, for statistics and charts."""
    clauses = []
    if account_id:
        clauses.append(FuelPriceSnapshot.account_id == account_id)
    if date_from:
        clauses.append(FuelPriceSnapshot.recorded_at >= date_from)
    if date_to:
        clauses.append(FuelPriceSnapshot.recorded_at <= date_to)

    rows = db.execute(
        select(FuelPriceSnapshot, Account, Store)
        .join(Account, FuelPriceSnapshot.account_id == Account.id)
        .join(Store, Account.store_id == Store.id)
        .where(*clauses)
        .order_by(FuelPriceSnapshot.recorded_at.desc())
        .limit(min(max(limit, 1), 10_000))
    ).all()
    return [
        FuelPriceHistoryOut(
            account_id=snapshot.account_id,
            account_label=account.label,
            store_code=snapshot.store_code,
            store_name=store.name,
            product_code=snapshot.product_code,
            product_name=snapshot.product_name,
            price=float(snapshot.price),
            discount=float(snapshot.discount) if snapshot.discount is not None else None,
            currency=snapshot.currency,
            recorded_at=snapshot.recorded_at,
        )
        for snapshot, account, store in rows
    ]
