"""Paper receipt OCR — placeholder, per plan to build this out later.

Intended shape: an Account of store_code="scanner" won't have a phone/token; instead
receipts get pushed in (photo upload -> OCR -> NormalizedReceipt) rather than pulled
on a schedule. fetch_receipts stays a no-op so this adapter can sit in the registry
without breaking the scheduler; a dedicated upload endpoint will call the OCR
pipeline directly once it exists.
"""
from __future__ import annotations

import datetime as dt

from app.adapters.base import BaseAdapter, NormalizedReceipt


class ScannerAdapter(BaseAdapter):
    store_code = "scanner"
    receipts_supported = False  # receipts are pushed via photo upload, never pulled
    manual_receipts_supported = True

    async def fetch_receipts(
        self, access_token: str, extra: dict, since: dt.datetime | None = None
    ) -> list[NormalizedReceipt]:
        return []
