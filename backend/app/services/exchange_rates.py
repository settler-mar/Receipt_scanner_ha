from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from dataclasses import dataclass
from typing import Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import as_utc
from app.models.models import Receipt

logger = logging.getLogger(__name__)


class ExchangeRateProvider(Protocol):
    def rate_to_uah(self, currency: str, at: dt.datetime) -> Decimal | None:
        ...


class NbuExchangeRateProvider:
    """Fetch official historical hryvnia rates from the NBU open-data API."""

    base_url = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange"

    def __init__(self, *, timeout: float = 8.0) -> None:
        self.timeout = timeout
        self._cache: dict[tuple[str, str], Decimal | None] = {}

    def rate_to_uah(self, currency: str, at: dt.datetime) -> Decimal | None:
        code = (currency or "UAH").upper()
        if code == "UAH":
            return None
        date_key = as_utc(at).strftime("%Y%m%d")
        cache_key = (code, date_key)
        if cache_key in self._cache:
            return self._cache[cache_key]
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(
                    self.base_url,
                    params={"valcode": code, "date": date_key, "json": ""},
                )
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not fetch NBU exchange rate for %s at %s: %s", code, date_key, exc)
            self._cache[cache_key] = None
            return None
        row = payload[0] if isinstance(payload, list) and payload else payload
        try:
            rate = Decimal(str(row["rate"]))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not parse NBU exchange rate for %s at %s: %s", code, date_key, exc)
            rate = None
        self._cache[cache_key] = rate
        return rate


default_provider = NbuExchangeRateProvider()


def rate_to_uah(currency: str, at: dt.datetime, *, provider: ExchangeRateProvider | None = None) -> Decimal | None:
    return (provider or default_provider).rate_to_uah(currency, at)


@dataclass
class ExchangeRateBackfillResult:
    checked: int = 0
    updated: int = 0
    missing: int = 0


def backfill_missing_rates(
    db: Session,
    *,
    provider: ExchangeRateProvider | None = None,
    limit: int | None = None,
) -> ExchangeRateBackfillResult:
    """Fill `exchange_rate_to_uah` for existing non-UAH receipts.

    Missing remote rates are left empty so we can retry later; UAH receipts do
    not need a stored rate because their dashboard multiplier is 1.
    """
    stmt = (
        select(Receipt)
        .where(Receipt.currency != "UAH", Receipt.exchange_rate_to_uah.is_(None))
        .order_by(Receipt.purchased_at.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = db.execute(stmt).scalars().all()
    result = ExchangeRateBackfillResult(checked=len(rows))
    for receipt in rows:
        rate = rate_to_uah(receipt.currency, receipt.purchased_at, provider=provider)
        if rate is None:
            result.missing += 1
            continue
        receipt.exchange_rate_to_uah = rate
        result.updated += 1
    db.commit()
    return result
