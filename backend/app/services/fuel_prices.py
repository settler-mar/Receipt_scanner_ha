from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.base import AdapterError, FuelPrice, NotImplementedYet
from app.models.models import Account, AccountStatus, FuelPriceSnapshot
from app.schemas.schemas import AccountFuelPricesOut, FuelPriceOut

logger = logging.getLogger(__name__)


def persist_fuel_prices(
    db: Session,
    account: Account,
    prices: list[FuelPrice],
    recorded_at: dt.datetime | None = None,
) -> int:
    """Store only price changes compared with the latest saved product state."""
    observed_at = recorded_at or dt.datetime.now(dt.timezone.utc)
    inserted = 0

    for price in prices:
        identity = str(price.code) if price.code is not None else price.name.casefold().strip()
        latest = db.execute(
            select(FuelPriceSnapshot)
            .where(
                FuelPriceSnapshot.account_id == account.id,
                (
                    FuelPriceSnapshot.product_code == identity
                    if price.code is not None
                    else FuelPriceSnapshot.product_code.is_(None)
                ),
                *(
                    []
                    if price.code is not None
                    else [func.lower(FuelPriceSnapshot.product_name) == identity]
                ),
            )
            .order_by(FuelPriceSnapshot.recorded_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if latest is not None and (
            float(latest.price) == price.price
            and (float(latest.discount) if latest.discount is not None else None) == price.discount
            and latest.currency == price.currency
        ):
            continue

        db.add(
            FuelPriceSnapshot(
                account_id=account.id,
                store_code=account.store.code,
                product_code=str(price.code) if price.code is not None else None,
                product_name=price.name,
                price=price.price,
                discount=price.discount,
                currency=price.currency,
                recorded_at=observed_at,
            )
        )
        inserted += 1

    return inserted


# --------------------------------------------------------------------------- #
# Live refresh (background job) vs. cached read (dashboard)
# --------------------------------------------------------------------------- #
async def refresh_account_fuel_prices(db: Session, account: Account) -> bool:
    """Live-fetch one account's board prices + monthly discount allowance, write
    any price-change snapshots, and cache `fuel_prices_checked_at` / allowance on
    the account. Best-effort: returns False (state untouched) on any adapter
    error or when the account has no usable credential. Commits on success."""
    # Imported here to keep this module import-safe from the scheduler.
    from app.adapters.registry import get_adapter
    from app.services.collector import ensure_fresh_token

    if account.credential is None or not account.credential.access_token_enc:
        return False

    adapter = get_adapter(account.store.code)
    extra = dict(account.credential.extra or {})
    try:
        access_token = await ensure_fresh_token(db, account, adapter)
        prices = await adapter.get_fuel_prices(access_token, extra)
    except (NotImplementedYet, AdapterError):
        return False

    persist_fuel_prices(db, account, prices)
    account.fuel_prices_checked_at = dt.datetime.now(dt.timezone.utc)

    try:
        allowance = await adapter.get_fuel_discount_allowance(access_token, extra)
        account.fuel_discount_allowance = allowance.remaining
        account.fuel_discount_allowance_unit = allowance.unit
        account.fuel_discount_allowance_resets_at = allowance.resets_at
    except (NotImplementedYet, AdapterError):
        pass

    db.commit()
    return True


async def refresh_all_fuel_prices(db: Session, min_age: dt.timedelta | None = None) -> int:
    """Refresh every active account whose adapter reports fuel-price support.
    `min_age` skips accounts already refreshed within that window (used by the
    startup catch-up so a redeploy doesn't re-hit every station API)."""
    from app.adapters.registry import get_adapter
    from app.core.time import as_utc

    now = dt.datetime.now(dt.timezone.utc)
    accounts = db.execute(
        select(Account).where(Account.status == AccountStatus.active)
    ).scalars().all()

    refreshed = 0
    for account in accounts:
        if not get_adapter(account.store.code).capabilities.get("fuel_prices"):
            continue
        if min_age is not None:
            checked = as_utc(account.fuel_prices_checked_at)
            if checked is not None and now - checked < min_age:
                continue
        try:
            if await refresh_account_fuel_prices(db, account):
                refreshed += 1
        except Exception:  # noqa: BLE001 — never let one account kill the batch
            logger.warning("Fuel-price refresh failed for account %s", account.label, exc_info=True)
            db.rollback()

    return refreshed


def read_fuel_prices(db: Session) -> list[AccountFuelPricesOut]:
    """Build the dashboard fuel-price list purely from stored snapshots — the
    latest snapshot per (account, product) plus each account's cached allowance.
    No network, no writes. The background job (app/scheduler.py) keeps the
    snapshots fresh."""
    accounts = db.execute(
        select(Account).where(Account.status == AccountStatus.active)
    ).scalars().all()

    out: list[AccountFuelPricesOut] = []
    for account in accounts:
        snaps = db.execute(
            select(FuelPriceSnapshot)
            .where(FuelPriceSnapshot.account_id == account.id)
            .order_by(FuelPriceSnapshot.recorded_at.desc())
        ).scalars().all()
        if not snaps:
            continue

        current: dict[str, FuelPriceSnapshot] = {}
        previous: dict[str, float] = {}
        for snap in snaps:
            identity = snap.product_code or snap.product_name.casefold().strip()
            if identity not in current:
                current[identity] = snap
            elif identity not in previous and float(snap.price) != float(current[identity].price):
                previous[identity] = float(snap.price)

        prices = [
            FuelPriceOut(
                name=snap.product_name,
                price=float(snap.price),
                currency=snap.currency,
                discount=float(snap.discount) if snap.discount is not None else None,
                code=snap.product_code,
                previous_price=previous.get(identity),
            )
            for identity, snap in sorted(current.items(), key=lambda kv: kv[1].product_name)
        ]

        out.append(
            AccountFuelPricesOut(
                account_id=account.id,
                account_label=account.label,
                store_code=account.store.code,
                store_name=account.store.name,
                checked_at=account.fuel_prices_checked_at,
                bonus_balance=float(account.bonus_balance) if account.bonus_balance is not None else None,
                bonus_balance_updated_at=account.bonus_balance_updated_at,
                discount_allowance=(
                    float(account.fuel_discount_allowance)
                    if account.fuel_discount_allowance is not None
                    else None
                ),
                discount_allowance_unit=account.fuel_discount_allowance_unit,
                discount_allowance_resets_at=account.fuel_discount_allowance_resets_at,
                prices=prices,
            )
        )
    return out
