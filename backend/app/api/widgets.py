"""Dashboard widgets — compact, self-describing JSON snapshots of each dashboard
block, meant for embedding a single block elsewhere (a Home Assistant `rest`
sensor, an `iframe` Lovelace card pointing at `/#/w/<key>`, …).

Every widget reuses the existing `/api/dashboard/*` query functions — this module
only reshapes their output into a flat `summary` (numbers/strings a HA
`value_template` can read directly) plus an optional detailed `items` array. The
frontend widget components fetch `/api/dashboard/*` themselves; the iframe page
does not depend on this endpoint.
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import dashboard as dash
from app.db import get_db
from app.models.models import Account, Receipt
from app.services.receipt_visibility import visible_receipt_clause

router = APIRouter(prefix="/api/widgets", tags=["widgets"])


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None


# --------------------------------------------------------------------------- #
# Builders — (db, date_from, date_to, include_discount) -> {"summary": ..., "items": ...}
# --------------------------------------------------------------------------- #
def _stat_cards(db, date_from, date_to, include_discount=True):
    s = dash.get_stats(db=db, date_from=date_from, date_to=date_to, include_discount=include_discount)
    return {
        "summary": {
            "total_receipts": s.total_receipts,
            "total_spent": round(s.total_spent, 2),
            "total_discount": round(s.total_discount, 2),
            "total_bonuses": round(s.total_bonuses, 2),
            "total_bonuses_used": round(s.total_bonuses_used, 2),
            "accounts_active": s.accounts_active,
            "accounts_needs_attention": s.accounts_needs_attention,
            "last_run_at": _iso(s.last_run_at),
        },
        "items": [],
    }


def _spend_trend(db, date_from, date_to, include_discount=True):
    points = dash.timeseries(db=db, date_from=date_from, date_to=date_to, include_discount=include_discount)
    return {
        "summary": {
            "total_spent": round(sum(p.total_spent for p in points), 2),
            "buckets": len(points),
            "first_period": points[0].period if points else None,
            "last_period": points[-1].period if points else None,
        },
        "items": [
            {"period": p.period, "total_spent": round(p.total_spent, 2), "receipt_count": p.receipt_count}
            for p in points
        ],
    }


def _by_store(db, date_from, date_to, include_discount=True):
    rows = dash.by_store(db=db, date_from=date_from, date_to=date_to, include_discount=include_discount)
    top = rows[0] if rows else None
    return {
        "summary": {
            "stores": len(rows),
            "total_spent": round(sum(r.total_spent for r in rows), 2),
            "top_store": top.store_name if top else None,
            "top_store_spent": round(top.total_spent, 2) if top else None,
        },
        "items": [
            {
                "store_code": r.store_code,
                "store_name": r.store_name,
                "receipt_count": r.receipt_count,
                "total_spent": round(r.total_spent, 2),
                "total_discount": round(r.total_discount, 2),
            }
            for r in rows
        ],
    }


def _by_channel(db, date_from, date_to, include_discount=True):
    rows = dash.by_channel(db=db, date_from=date_from, date_to=date_to, include_discount=include_discount)
    by = {r.channel: r for r in rows}
    online = by["online"].total_spent if "online" in by else 0.0
    offline = by["offline"].total_spent if "offline" in by else 0.0
    total = online + offline or 1.0
    return {
        "summary": {
            "online_spent": round(online, 2),
            "offline_spent": round(offline, 2),
            "online_pct": round(online / total * 100),
        },
        "items": [
            {"channel": r.channel, "receipt_count": r.receipt_count, "total_spent": round(r.total_spent, 2)}
            for r in rows
        ],
    }


def _by_category(db, date_from, date_to, include_discount=True):
    rows = dash.by_category(db=db, date_from=date_from, date_to=date_to, include_discount=include_discount)
    roots = [r for r in rows if r.depth == 0 and r.rollup_total > 0]
    roots.sort(key=lambda r: r.rollup_total, reverse=True)
    top = roots[0] if roots else None
    return {
        "summary": {
            "categories": len(roots),
            "top_category": top.name if top else None,
            "top_category_total": round(top.rollup_total, 2) if top else None,
        },
        "items": [
            {
                "path": r.path,
                "name": r.name,
                "depth": r.depth,
                "direct_total": r.direct_total,
                "rollup_total": r.rollup_total,
                "item_count": r.item_count,
            }
            for r in rows
        ],
    }


def _store_category(db, date_from, date_to, include_discount=True):
    m = dash.store_category(db=db, date_from=date_from, date_to=date_to, include_discount=include_discount)
    roots = sorted(
        (r for r in m.categories if r.depth == 0 and r.total > 0 and r.category_id is not None),
        key=lambda r: r.total, reverse=True,
    )
    top_store = m.stores[0] if m.stores else None
    return {
        "summary": {
            "stores": len(m.stores),
            "categories": len(roots),
            "total_spent": round(sum(s.total_spent for s in m.stores), 2),
            "top_store": top_store.store_name if top_store else None,
            "top_category": roots[0].name if roots else None,
        },
        "items": [
            {
                "path": r.path,
                "name": r.name,
                "depth": r.depth,
                "total": r.total,
                "item_count": r.item_count,
                "per_store": {c.store_code: round(c.total, 2) for c in r.per_store},
            }
            for r in m.categories if r.total > 0
        ],
    }


def _fuel_prices(db, date_from, date_to, include_discount=True):
    accounts = dash.fuel_prices(db=db)
    return {
        "summary": {
            "accounts": len(accounts),
            "checked_at": _iso(max((a.checked_at for a in accounts if a.checked_at), default=None)),
        },
        "items": [
            {
                "account_id": a.account_id,
                "account_label": a.account_label,
                "store_code": a.store_code,
                "store_name": a.store_name,
                "checked_at": _iso(a.checked_at),
                "prices": [
                    {
                        "name": p.name,
                        "price": p.price,
                        "discount": p.discount,
                        "net": round(p.price - (p.discount or 0), 2),
                        "currency": p.currency,
                    }
                    for p in a.prices
                ],
            }
            for a in accounts
        ],
    }


def _recent_receipts(db, date_from, date_to, include_discount=True):
    rows = db.execute(
        select(Receipt).where(visible_receipt_clause()).order_by(Receipt.purchased_at.desc()).limit(8)
    ).scalars().all()
    first = rows[0] if rows else None
    return {
        "summary": {
            "count": len(rows),
            "last_purchased_at": _iso(first.purchased_at) if first else None,
            "last_total": round(first.total_amount, 2) if first else None,
        },
        "items": [
            {
                "id": r.id,
                "purchased_at": _iso(r.purchased_at),
                "store": r.shop_name or r.store_code,
                "total_amount": round(r.total_amount, 2),
                "account_label": r.account.label if r.account else None,
            }
            for r in rows
        ],
    }


# key -> (label, builder, supports_range)
WIDGETS: dict[str, tuple[str, object, bool]] = {
    "stat-cards": ("Загальні показники", _stat_cards, True),
    "spend-trend": ("Витрати за період", _spend_trend, True),
    "by-store": ("Витрати за магазином", _by_store, True),
    "by-channel": ("Онлайн / офлайн", _by_channel, True),
    "by-category": ("Витрати за категоріями", _by_category, True),
    "store-category": ("Магазини × категорії", _store_category, True),
    "fuel-prices": ("Ціни на паливо", _fuel_prices, False),
    "recent-receipts": ("Останні чеки", _recent_receipts, False),
}


@router.get("")
def list_widgets():
    return [
        {"key": key, "label": label, "supports_range": supports_range}
        for key, (label, _builder, supports_range) in WIDGETS.items()
    ]


@router.get("/{key}")
async def get_widget(
    key: str,
    db: Session = Depends(get_db),
    date_from: dt.datetime | None = None,
    date_to: dt.datetime | None = None,
    include_discount: bool = True,
):
    entry = WIDGETS.get(key)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown widget: {key}")
    label, builder, _supports_range = entry

    result = builder(db, date_from, date_to, include_discount)
    if hasattr(result, "__await__"):
        result = await result

    return {
        "widget": key,
        "title": label,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        **result,
    }
