"""Pydantic models for the shopping-list API (ROADMAP.md Крок 2).

Separate module from schemas.py so it could be built alongside Крок 1.
"""
from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.schemas import AwareUTC


class ShoppingItemCreate(BaseModel):
    raw_text: str = Field(min_length=1)
    qty: float = 1
    unit: str | None = None
    note: str | None = None
    barcode: str | None = None
    category_id: str | None = None


class ShoppingItemUpdate(BaseModel):
    """PATCH — only fields present in the body are applied."""

    raw_text: str | None = Field(default=None, min_length=1)
    qty: float | None = None
    unit: str | None = None
    note: str | None = None
    barcode: str | None = None
    category_id: str | None = None
    status: str | None = None  # pending | bought | cancelled


class ShoppingItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    raw_text: str
    normalized_name: str
    category_id: str | None
    barcode: str | None
    qty: float
    unit: str | None
    status: str
    note: str | None
    auto_matched: bool
    bought_receipt_item_id: str | None
    added_at: AwareUTC
    bought_at: AwareUTC | None


class SuggestStoreItem(BaseModel):
    raw_text: str
    last_price: float | None
    unit: str | None
    last_seen_at: AwareUTC | None


class SuggestStoreOut(BaseModel):
    store_code: str
    store_name: str
    total_items: int
    covered_items: int
    approx_basket: float
    last_purchase_at: AwareUTC | None
    items: list[SuggestStoreItem]


class RescanResult(BaseModel):
    matched: list[ShoppingItemOut]


class LiveSuggestItem(BaseModel):
    raw_text: str
    query: str
    query_source: str            # "cache" | "ai" | "fallback"
    matched_name: str | None
    last_price: float | None
    product_url: str | None


class LiveSuggestStoreOut(BaseModel):
    store_code: str
    store_name: str
    total_items: int
    covered_items: int
    approx_basket: float
    items: list[LiveSuggestItem]
