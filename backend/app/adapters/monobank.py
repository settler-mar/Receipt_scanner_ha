"""Monobank personal statement — a bank-transaction feed, not a store.

No line items, no login flow: `fetch_receipts` stays a no-op (like
scanner.py) and the real work is `fetch_transactions`, gated by
`transactions_supported` instead of `receipts_supported`. Auth is a single
pasted personal token (AuthType.api_token) via the same generic
`POST /api/accounts/{id}/token` escape hatch every other store's manual-token
form already uses — see accounts.py's bootstrap_token.

Multiple independent Monobank connections (several personal tokens) are just
several Accounts of this one Store, same as any chain with more than one
shopper login — see models.py's Account/Credential docstring.
"""
from __future__ import annotations

import datetime as dt
from typing import Callable

from app.adapters.base import BaseAdapter, NormalizedBankTransaction, NormalizedReceipt
from app.services import monobank as mono_client
from app.services.monobank import MonobankError


class MonobankAdapter(BaseAdapter):
    store_code = "monobank"
    receipts_supported = False
    transactions_supported = True

    async def fetch_receipts(
        self, access_token: str, extra: dict, since: dt.datetime | None = None
    ) -> list[NormalizedReceipt]:
        return []

    async def fetch_transactions(
        self,
        access_token: str,
        extra: dict,
        since: dt.datetime | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[NormalizedBankTransaction]:
        since = since or (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24))
        items = await mono_client.fetch_statement(access_token, since=since, on_progress=on_progress)
        return [
            NormalizedBankTransaction(
                external_id=item["id"],
                occurred_at=dt.datetime.fromtimestamp(item["time"], tz=dt.timezone.utc),
                amount=item["amount"] / 100,
                mcc=item.get("mcc"),
                description=item.get("description"),
                counterparty={
                    "comment": item.get("comment"),
                    "receiptId": item.get("receiptId"),
                    "counterName": item.get("counterName"),
                    "counterEdrpou": item.get("counterEdrpou"),
                },
                raw=item,
            )
            for item in items
        ]

    async def test_token(self, access_token: str) -> dict:
        try:
            info = await mono_client.fetch_client_info(access_token)
        except MonobankError as exc:
            return {"ok": False, "detail": str(exc)}
        name = info.get("name") or "?"
        accounts = len(info.get("accounts") or [])
        return {"ok": True, "detail": f"{name} · рахунків: {accounts}"}
