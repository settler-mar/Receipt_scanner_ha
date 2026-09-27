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
    target_store_code: str | None = None
    tag: str | None = None
    added_method: str = "web"
    added_by: str | None = None


class ShoppingItemUpdate(BaseModel):
    """PATCH — only fields present in the body are applied."""

    raw_text: str | None = Field(default=None, min_length=1)
    qty: float | None = None
    unit: str | None = None
    note: str | None = None
    barcode: str | None = None
    category_id: str | None = None
    target_store_code: str | None = None
    tag: str | None = None
    status: str | None = None  # pending | bought | cancelled


class ShoppingItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    raw_text: str
    normalized_name: str
    category_id: str | None
    category_name: str | None = None
    category_group: str | None = None
    store_class: str | None = None
    store_class_name: str | None = None
    target_store_code: str | None
    tag: str | None
    barcode: str | None
    qty: float
    unit: str | None
    status: str
    note: str | None
    auto_matched: bool
    bought_receipt_item_id: str | None
    added_at: AwareUTC
    added_method: str
    added_by: str | None
    bought_at: AwareUTC | None
    bought_store_name: str | None = None
    bought_receipt_id: str | None = None
    bought_receipt_url: str | None = None
    bought_purchased_at: AwareUTC | None = None


class ShoppingSettingsOut(BaseModel):
    auto_categorize: bool
    auto_categorize_delay_minutes: int


class ShoppingSettingsUpdate(BaseModel):
    auto_categorize: bool | None = None
    auto_categorize_delay_minutes: int | None = Field(default=None, ge=1, le=1440)


class ShoppingArchiveItemOut(BaseModel):
    shopping_item_id: str
    list_name: str
    qty: float
    unit: str | None
    receipt_item_name: str | None
    receipt_item_total: float | None


class ShoppingArchiveReceiptOut(BaseModel):
    receipt_id: str | None
    purchased_at: AwareUTC | None
    shop_name: str | None
    store_code: str | None
    total_amount: float | None
    currency: str | None
    receipt_url: str | None
    items: list[ShoppingArchiveItemOut]


class ShoppingMatchCandidateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    shopping_list_item_id: str
    receipt_item_id: str
    receipt_id: str
    list_name: str
    receipt_item_name: str
    confidence: float
    method: str
    explanation: str | None
    status: str
    created_at: AwareUTC


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


class ShoppingTripCreate(BaseModel):
    store_code: str | None = None
    owner_name: str = Field(default="Неизвестно", min_length=1, max_length=80)


class ShoppingTripItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    trip_id: str
    shopping_list_item_id: str
    raw_text: str
    qty: float
    unit: str | None
    note: str | None
    status: str
    created_at: AwareUTC


class ShoppingTripOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    store_code: str | None
    owner_name: str
    status: str
    created_at: AwareUTC
    last_activity_at: AwareUTC
    expires_at: AwareUTC
    completed_at: AwareUTC | None
    items: list[ShoppingTripItemOut] = Field(default_factory=list)


class ShoppingTripItemUpdate(BaseModel):
    status: str = Field(pattern="^(planned|in_cart|bought|skipped)$")


class ShoppingRoutineOut(BaseModel):
    normalized_name: str
    display_name: str
    purchase_count: int
    interval_days: float
    last_purchased_at: AwareUTC
    due_at: AwareUTC
    days_overdue: int
    ignored: bool = False
    postponed_until: AwareUTC | None = None
    category_id: str | None = None
    category_path: str | None = None
    median_qty: float
    unit: str | None = None


class ShoppingRoutineAction(BaseModel):
    action: str = Field(pattern="^(add|postpone|ignore|unignore)$")
    postpone_days: int = Field(default=7, ge=1, le=365)


class ShoppingRoutineCategoryAction(BaseModel):
    action: str = Field(pattern="^(ignore|unignore)$")


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
