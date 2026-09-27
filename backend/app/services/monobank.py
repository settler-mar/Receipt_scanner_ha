"""Monobank personal-statement API client.

Pure HTTP client, no database access — mirrors how store adapters keep
API-shape knowledge separate from persistence (app/services/collector.py owns
that side for receipts; app/services/bank_transactions.py owns it for
statements). Used by adapters/monobank.py, which maps the raw dicts here onto
NormalizedBankTransaction.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import math
from typing import Callable

import httpx

API_BASE = "https://api.monobank.ua"
# Monobank throttles the statement endpoint to one request per token per this
# many seconds, and caps each request to a 31-day window.
STATEMENT_MIN_INTERVAL = dt.timedelta(seconds=60)
STATEMENT_MAX_WINDOW = dt.timedelta(days=31)


class MonobankError(Exception):
    pass


def chunk_count(since: dt.datetime, until: dt.datetime | None = None) -> int:
    """How many throttled requests fetch_statement will need for this range —
    used to size a progress bar before the first request goes out."""
    until = until or dt.datetime.now(dt.timezone.utc)
    span = (until - since).total_seconds()
    return max(1, math.ceil(span / STATEMENT_MAX_WINDOW.total_seconds()))


async def fetch_statement(
    token: str,
    *,
    since: dt.datetime,
    until: dt.datetime | None = None,
    account: str = "0",
    on_progress: Callable[[int, int], None] | None = None,
) -> list[dict]:
    """Fetch every statement item in [since, until), chunked to Monobank's
    31-day-per-request limit and throttled to its 1-request/60s rate limit.
    Never logs the token. `on_progress(done, total)` fires after each chunk,
    in request order, so a caller can persist/display how far along a long
    backfill is."""
    until = until or dt.datetime.now(dt.timezone.utc)
    total = chunk_count(since, until)
    items: list[dict] = []
    window_start = since
    done = 0
    first_request = True
    async with httpx.AsyncClient(timeout=20.0) as client:
        while window_start < until:
            if not first_request:
                await asyncio.sleep(STATEMENT_MIN_INTERVAL.total_seconds())
            first_request = False
            window_end = min(window_start + STATEMENT_MAX_WINDOW, until)
            url = (
                f"{API_BASE}/personal/statement/{account}/"
                f"{int(window_start.timestamp())}/{int(window_end.timestamp())}"
            )
            try:
                response = await client.get(url, headers={"X-Token": token})
            except httpx.HTTPError as exc:
                raise MonobankError(f"Monobank request failed: {exc}") from exc
            if response.status_code != 200:
                raise MonobankError(
                    f"Monobank returned {response.status_code} for statement request"
                )
            items.extend(response.json())
            window_start = window_end
            done += 1
            if on_progress is not None:
                on_progress(done, total)
    return items


async def fetch_client_info(token: str) -> dict:
    """GET /personal/client-info — the cheapest authenticated call Monobank
    offers, used purely to prove a token is valid (see BaseAdapter.test_token)."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(f"{API_BASE}/personal/client-info", headers={"X-Token": token})
        except httpx.HTTPError as exc:
            raise MonobankError(f"Monobank request failed: {exc}") from exc
        if response.status_code != 200:
            raise MonobankError(f"Monobank returned {response.status_code} for client-info")
        return response.json()
