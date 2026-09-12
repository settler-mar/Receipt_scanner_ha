"""Recording a fuel purchase from a station's own price list.

"був на барс і заправився газом на 1000грн" carries no litres and no price — the
station's latest saved price turns it into a proper receipt line. This lives in a
service because two callers need exactly the same rules: the MCP tool
`add_fuel_purchase` and the free-phrase capture used by the Telegram bot.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.models import Account, AccountStatus, FuelPriceSnapshot, Store

logger = logging.getLogger(__name__)

# keyword -> substrings that identify the fuel product in FuelPriceSnapshot.product_name
FUEL_SYNONYMS: dict[str, tuple[str, ...]] = {
    "газ": ("газ", "скраплен", "свг", "lpg", "пропан"),
    "дизель": ("дп ", "дизел", "diesel", "євро", "euro"),
    "дт": ("дп ", "дизел", "diesel"),
    "бензин": ("бензин", "а-9", "а9", "а-95", "а-92", "а-98", "super", "premium"),
}


def resolve_store(db: Session, query: str) -> Store:
    """Find one Store by code (exact, case-insensitive) or by a substring of its
    name/code. Raises ValueError if nothing or more than one matches."""
    q = (query or "").strip().casefold()
    if not q:
        raise ValueError("не вказано магазин")
    stores = db.execute(select(Store)).scalars().all()
    exact = [s for s in stores if s.code.casefold() == q]
    if len(exact) == 1:
        return exact[0]
    hits = [s for s in stores if q in s.code.casefold() or q in s.name.casefold()]
    if not hits:
        raise ValueError(f"магазин {query!r} не знайдено")
    if len(hits) > 1:
        raise ValueError(
            f"{query!r} підходить кільком магазинам: "
            + ", ".join(f"{s.name} ({s.code})" for s in hits)
        )
    return hits[0]


def store_account(db: Session, store: Store) -> Account:
    accs = db.execute(
        select(Account).where(Account.store_id == store.id)
    ).scalars().all()
    if not accs:
        raise ValueError(
            f"для «{store.name}» немає жодного акаунта — чек нема до чого прив'язати"
        )
    return next((a for a in accs if a.status == AccountStatus.active), accs[0])


def latest_fuel_price(db: Session, store_code: str, fuel: str) -> FuelPriceSnapshot:
    """Newest FuelPriceSnapshot for `store_code` whose product_name matches the
    `fuel` keyword (exact-ish contains, or a small synonym set)."""
    rows = db.execute(
        select(FuelPriceSnapshot)
        .where(FuelPriceSnapshot.store_code == store_code)
        .order_by(FuelPriceSnapshot.recorded_at.desc())
    ).scalars().all()
    if not rows:
        raise ValueError(
            f"немає збережених цін на паливо для {store_code!r} — "
            "передайте price_per_liter явно, або спершу оновіть ціни на дашборді"
        )
    key = fuel.strip().casefold()
    needles = FUEL_SYNONYMS.get(key, (key,))
    for snap in rows:  # rows are newest-first
        name = snap.product_name.casefold()
        if any(n in name for n in needles):
            return snap
    available = sorted({r.product_name for r in rows})
    raise ValueError(
        f"не знайшов паливо «{fuel}» серед цін {store_code!r}. Доступні: "
        + ", ".join(available)
    )


def record_fuel_purchase(
    db: Session,
    *,
    store: str,
    fuel: str = "газ",
    amount: float | None = None,
    liters: float | None = None,
    price_per_liter: float | None = None,
    purchased_at: dt.datetime | None = None,
    source: str = "mcp",
) -> dict[str, Any]:
    """Save one fuel receipt under the station's own account.

    Either `amount` (UAH paid) or `liters` is enough — the other follows from the
    station's latest price, which `price_per_liter` overrides. The receipt is a
    single line (fuel name, litres, price per litre) and goes through the same
    persist path as every other receipt.
    """
    from app.adapters.base import NormalizedItem, NormalizedReceipt
    from app.api.inbox import _persist_receipt

    if amount is None and liters is None:
        raise ValueError("вкажіть суму (amount) або обʼєм (liters)")
    when = purchased_at or dt.datetime.now(dt.timezone.utc)

    store_row = resolve_store(db, store)
    account = store_account(db, store_row)

    if price_per_liter is not None:
        net_price = float(price_per_liter)
        product_name = fuel.strip() or "Паливо"
        price_source = "передано явно"
    else:
        snap = latest_fuel_price(db, store_row.code, fuel)
        net_price = float(snap.price) - float(snap.discount or 0)
        product_name = snap.product_name
        price_source = f"ціна станції від {snap.recorded_at:%Y-%m-%d}"
    if net_price <= 0:
        raise ValueError("ціна за літр вийшла ≤ 0")

    if amount is None:
        amount = round(float(liters) * net_price, 2)
    elif liters is None:
        liters = round(float(amount) / net_price, 3)
    amount = float(amount)
    liters = float(liters)

    nr = NormalizedReceipt(
        external_id=f"mcp-fuel:{when.timestamp():.0f}:{abs(hash((store_row.code, round(amount, 2), round(liters, 2)))) % 10**8}",
        purchased_at=when,
        total_amount=amount,
        currency="UAH",
        shop_name=store_row.name,
        channel="offline",
        items=[NormalizedItem(
            name=product_name,
            total_price=amount,
            quantity=liters,
            unit="л",
            unit_price=net_price,
        )],
        raw={"source": source, "kind": "fuel", "fuel": product_name,
             "liters": liters, "price_per_liter": net_price},
    )
    try:
        receipt = _persist_receipt(db, account, nr)
    except Exception as exc:  # HTTPException etc.
        raise ValueError(f"не вдалося зберегти чек: {getattr(exc, 'detail', exc)}") from exc

    return {
        "id": receipt.id,
        "store_code": receipt.store_code,
        "store_name": store_row.name,
        "account_label": account.label,
        "purchased_at": receipt.purchased_at.isoformat() if receipt.purchased_at else None,
        "fuel": product_name,
        "liters": round(liters, 3),
        "price_per_liter": round(net_price, 4),
        "total_amount": round(amount, 2),
        "price_source": price_source,
    }
