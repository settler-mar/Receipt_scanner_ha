"""Core ORM models.

Domain summary:
  Store       — a receipt source we know how to talk to (atb, fozzy, fora, metro, scanner...).
                Static/seeded reference data, one row per adapter.
  Account     — one "installation" of a store for one person, e.g. "АТБ Максим" vs "АТБ Насті".
                Multiple accounts can point at the same Store.
  Credential  — auth material for an Account (tokens etc.), encrypted at rest, 1:1 with Account.
  Schedule    — how often we auto-collect for an Account.
  CollectionRun — one execution log (manual or scheduled) of pulling receipts for an Account.
  Receipt / ReceiptItem — normalized data, regardless of source store.
"""
from __future__ import annotations

import datetime as dt
import enum
import uuid
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import (
    BigInteger,
    Boolean,
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class AuthType(str, enum.Enum):
    phone_otp = "phone_otp"          # phone + SMS code (ATB, Fozzy, Fora)
    phone_password = "phone_password" # phone/login + password (Vencon)
    phone_call = "phone_call"        # automated verification call (BRSM)
    device_uuid = "device_uuid"      # anonymous device id (METRO)
    manual = "manual"                # no automated login (e.g. scanner/OCR)
    oauth_browser = "oauth_browser"  # OAuth 2.1 + PKCE via a real browser login (Silpo MCP)
    # Just the physical loyalty-card number, no login at all — for stores where
    # the store's own account API is unreachable (BARS' backend is TLS-pinned
    # against our mitmproxy interception, see adapters/bars.py) but the QR/
    # barcode shown at checkout is derivable from the card number alone, same
    # as reading it off the physical card.
    card_number = "card_number"


class AccountStatus(str, enum.Enum):
    pending_otp = "pending_otp"   # OTP sent, awaiting code confirmation
    active = "active"             # has a usable token
    needs_reauth = "needs_reauth" # token expired/invalid, needs a fresh login
    error = "error"


class RunStatus(str, enum.Enum):
    running = "running"
    success = "success"
    partial = "partial"
    error = "error"


class DuplicateStatus(str, enum.Enum):
    pending = "pending"
    auto_merged = "auto_merged"
    needs_review = "needs_review"
    rejected = "rejected"
    rolled_back = "rolled_back"


class DuplicateRunStatus(str, enum.Enum):
    running = "running"
    success = "success"
    error = "error"


class Store(Base):
    __tablename__ = "stores"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    code: Mapped[str] = mapped_column(String, unique=True, index=True)  # atb / fozzy / fora / metro / scanner
    name: Mapped[str] = mapped_column(String)
    auth_type: Mapped[AuthType] = mapped_column(Enum(AuthType))
    enabled: Mapped[bool] = mapped_column(default=True)
    # Coarse chain type ("supermarket" / "azs" / "tech" / "pharmacy" / "home" /
    # "other") — seeded reference data, not classified per store. Used by the
    # shopping-list store picker (ROADMAP.md Крок 2); nullable for stores we
    # haven't tagged yet.
    network_type: Mapped[str | None] = mapped_column(String, nullable=True)

    accounts: Mapped[list["Account"]] = relationship(back_populates="store")


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"))
    label: Mapped[str] = mapped_column(String)  # e.g. "АТБ Максим"
    phone_number: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[AccountStatus] = mapped_column(Enum(AccountStatus), default=AccountStatus.pending_otp)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    # Cached loyalty balance — refreshed by collector.py on each collection run
    # (best-effort, see BaseAdapter.get_balance), not fetched live on page load.
    bonus_balance: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    bonus_balance_updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Current personal discount/cashback rate as a percent, when the store's loyalty
    # program has one separate from the bonus-point wallet above (see dnipro_m.py) —
    # cached the same way and on the same schedule as bonus_balance.
    discount_percent: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    # Last time the background fuel-price job successfully refreshed this
    # account's board prices (app/scheduler.py, 3×/day). Distinct from a price
    # *snapshot* — snapshots are only written on change, this ticks every fetch.
    # The dashboard reads cached snapshots, never fetching live.
    fuel_prices_checked_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Monthly fuel-discount cap state (Ukrnafta), cached alongside the prices —
    # see FuelDiscountAllowance in adapters/base.py.
    fuel_discount_allowance: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    fuel_discount_allowance_unit: Mapped[str | None] = mapped_column(String, nullable=True)
    fuel_discount_allowance_resets_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    store: Mapped[Store] = relationship(back_populates="accounts")
    credential: Mapped["Credential | None"] = relationship(
        back_populates="account", uselist=False, cascade="all, delete-orphan"
    )
    schedule: Mapped["Schedule | None"] = relationship(
        back_populates="account", uselist=False, cascade="all, delete-orphan"
    )
    runs: Mapped[list["CollectionRun"]] = relationship(back_populates="account", cascade="all, delete-orphan")
    receipts: Mapped[list["Receipt"]] = relationship(back_populates="account", cascade="all, delete-orphan")
    fuel_price_snapshots: Mapped[list["FuelPriceSnapshot"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class Credential(Base):
    """Auth material for one Account. Sensitive fields are stored encrypted (see core.security)."""

    __tablename__ = "credentials"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), unique=True)

    access_token_enc: Mapped[str | None] = mapped_column(String, nullable=True)
    refresh_token_enc: Mapped[str | None] = mapped_column(String, nullable=True)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Store-specific extra bits an adapter needs (customerId/deviceId/otp challenge id, etc).
    # Not encrypted — must not contain secrets, only identifiers.
    extra: Mapped[dict] = mapped_column(JSON, default=dict)

    account: Mapped[Account] = relationship(back_populates="credential")


class Schedule(Base):
    __tablename__ = "schedules"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), unique=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    # Standard 5-field cron expression, e.g. "0 */6 * * *" = every 6 hours.
    cron_expression: Mapped[str] = mapped_column(String, default="0 12,19 * * *")
    last_run_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    account: Mapped[Account] = relationship(back_populates="schedule")


class CollectionRun(Base):
    __tablename__ = "collection_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    trigger: Mapped[str] = mapped_column(String, default="manual")  # manual | schedule | login | import
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus), default=RunStatus.running)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    receipts_found: Mapped[int] = mapped_column(default=0)
    receipts_new: Mapped[int] = mapped_column(default=0)
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)

    account: Mapped[Account] = relationship(back_populates="runs")


class FuelPriceSnapshot(Base):
    """A point-in-time fuel price returned by a station chain's API."""

    __tablename__ = "fuel_price_snapshots"
    __table_args__ = (
        Index("ix_fuel_price_account_recorded", "account_id", "recorded_at"),
        Index("ix_fuel_price_store_product", "store_code", "product_code"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    store_code: Mapped[str] = mapped_column(String)
    product_code: Mapped[str | None] = mapped_column(String, nullable=True)
    product_name: Mapped[str] = mapped_column(String)
    price: Mapped[float] = mapped_column(Numeric(12, 2))
    discount: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String, default="UAH")
    recorded_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    account: Mapped[Account] = relationship(back_populates="fuel_price_snapshots")


class Category(Base):
    """A node in the nested product-category tree (ROADMAP.md Крок 1).

    `path` is a materialised slug path ("produkty/ovochi") kept in sync by
    api/categories.py on every create/rename/move — it makes "this branch and
    everything under it" a cheap `path LIKE 'produkty/%'` query for the
    by-category dashboard rollup.
    """

    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("path", name="uq_category_path"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String)
    slug: Mapped[str] = mapped_column(String)
    path: Mapped[str] = mapped_column(String, index=True)
    sort_order: Mapped[int] = mapped_column(default=0)

    parent: Mapped["Category | None"] = relationship(
        back_populates="children", remote_side="Category.id"
    )
    children: Mapped[list["Category"]] = relationship(back_populates="parent")


class ProductCategorySource(str, enum.Enum):
    ai = "ai"        # assigned by app.services.ai.classify_products
    user = "user"    # set by hand in the UI — sticky, AI never overwrites it
    store = "store"  # derived from the store's own category string (future use)


class ProductCategory(Base):
    """Alias cache: one row per unique normalised product name ever seen, so a
    name is classified at most once (ROADMAP.md Крок 1 "product_category").

    A row exists even when classification produced no usable category
    (`category_id` NULL) — that's what stops a name being re-queued forever.
    """

    __tablename__ = "product_category"

    normalized_name: Mapped[str] = mapped_column(String, primary_key=True)
    category_id: Mapped[str | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source: Mapped[ProductCategorySource] = mapped_column(
        Enum(ProductCategorySource), default=ProductCategorySource.ai
    )
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), default=0)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class AppSetting(Base):
    """Generic key/value store for user-tweakable app/UI settings that don't
    warrant a table of their own — e.g. which fuel marks and stations the
    dashboard fuel block shows. `value` is arbitrary JSON, shape owned by
    whichever frontend feature reads the key."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class Receipt(Base):
    __tablename__ = "receipts"
    __table_args__ = (UniqueConstraint("account_id", "external_id", name="uq_receipt_account_external"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    store_code: Mapped[str] = mapped_column(String, index=True)
    external_id: Mapped[str] = mapped_column(String, index=True)  # store's own receipt/order id

    purchased_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    total_amount: Mapped[float] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String, default="UAH")
    exchange_rate_to_uah: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    shop_name: Mapped[str | None] = mapped_column(String, nullable=True)
    shop_address: Mapped[str | None] = mapped_column(String, nullable=True)
    payment_type: Mapped[str | None] = mapped_column(String, nullable=True)
    channel: Mapped[str] = mapped_column(String, default="offline")  # "offline" | "online"
    total_discount: Mapped[float] = mapped_column(Numeric(12, 2), default=0)  # sum of item discounts
    bonuses: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)  # loyalty points/cashback earned, when the store's API reports it
    bonuses_used: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)  # loyalty points/cashback spent as payment, when reported
    receipt_url: Mapped[str | None] = mapped_column(String, nullable=True)  # e.g. an SMS/email fiscal-receipt link (vencon.ua/receipt/... and similar)
    order_status: Mapped[str | None] = mapped_column(String, nullable=True)
    tracking_numbers: Mapped[list] = mapped_column(JSON, default=list)
    photo_filename: Mapped[str | None] = mapped_column(String, nullable=True)  # relative filename under data/photos/, see api/receipts.py
    # Whether the store's own API says this purchase has a warranty document
    # (see BaseAdapter.get_warranty_document, GET /api/receipts/{id}/warranty) —
    # None when the store doesn't report warranty status at all.
    has_warranty: Mapped[bool | None] = mapped_column(nullable=True)
    # Parsed out of an uploaded warranty PDF (see api/receipts.py's
    # _extract_warranty_expiry) or set manually — best-effort, may stay unset
    # even when has_warranty is true if the PDF text didn't match the expected
    # phrasing, or no document has been uploaded at all.
    warranty_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Manually uploaded warranty document (see POST /{id}/warranty) — relative
    # filename under data/warranty/, same pattern as photo_filename. Only stores
    # whose session can't be proxied live need this (currently Дніпро-М, see
    # RESEARCH.md); GET /{id}/warranty prefers this file when present, falling
    # back to a live adapter fetch otherwise.
    warranty_pdf_filename: Mapped[str | None] = mapped_column(String, nullable=True)

    raw: Mapped[dict] = mapped_column(JSON, default=dict)  # original payload, for future re-parsing
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    account: Mapped[Account] = relationship(back_populates="receipts")
    items: Mapped[list["ReceiptItem"]] = relationship(back_populates="receipt", cascade="all, delete-orphan")
    tags: Mapped[list["ReceiptTag"]] = relationship(
        back_populates="receipt", cascade="all, delete-orphan",
        order_by="ReceiptTag.tag",
    )


class DuplicateRun(Base):
    __tablename__ = "duplicate_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    trigger: Mapped[str] = mapped_column(String, default="manual")
    status: Mapped[DuplicateRunStatus] = mapped_column(
        Enum(DuplicateRunStatus), default=DuplicateRunStatus.running, index=True
    )
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scanned_receipts: Mapped[int] = mapped_column(default=0)
    candidates_found: Mapped[int] = mapped_column(default=0)
    candidates_new: Mapped[int] = mapped_column(default=0)
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)


class DuplicateGroup(Base):
    __tablename__ = "duplicate_groups"
    __table_args__ = (
        Index("ix_duplicate_groups_status_score", "status", "score"),
        UniqueConstraint("fingerprint", name="uq_duplicate_group_fingerprint"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    fingerprint: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[DuplicateStatus] = mapped_column(Enum(DuplicateStatus), default=DuplicateStatus.pending)
    score: Mapped[int] = mapped_column(default=0)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    receipt_ids: Mapped[list] = mapped_column(JSON, default=list)
    canonical_receipt_id: Mapped[str | None] = mapped_column(ForeignKey("receipts.id"), nullable=True)
    created_by_run_id: Mapped[str | None] = mapped_column(ForeignKey("duplicate_runs.id"), nullable=True)
    last_seen_run_id: Mapped[str | None] = mapped_column(ForeignKey("duplicate_runs.id"), nullable=True)
    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    merged_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rolled_back_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    merge_log: Mapped[list] = mapped_column(JSON, default=list)
    rollback_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    candidates: Mapped[list["DuplicateCandidate"]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )
    canonical_receipt: Mapped["Receipt | None"] = relationship()


class DuplicateCandidate(Base):
    __tablename__ = "duplicate_candidates"
    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_duplicate_candidate_fingerprint"),
        Index("ix_duplicate_candidates_status_score", "status", "score"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    group_id: Mapped[str] = mapped_column(ForeignKey("duplicate_groups.id"), index=True)
    fingerprint: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[DuplicateStatus] = mapped_column(Enum(DuplicateStatus), default=DuplicateStatus.pending)
    score: Mapped[int] = mapped_column(default=0)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    receipt_ids: Mapped[list] = mapped_column(JSON, default=list)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by_run_id: Mapped[str | None] = mapped_column(ForeignKey("duplicate_runs.id"), nullable=True)
    last_seen_run_id: Mapped[str | None] = mapped_column(ForeignKey("duplicate_runs.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    group: Mapped[DuplicateGroup] = relationship(back_populates="candidates")


class ReceiptTag(Base):
    """A free-form label on a receipt — e.g. "поездка_львов_лето_2026". Lets the
    user group spend across receipts that don't share a store or a date range
    (a trip, a renovation, a gift budget). Stored normalised, without the '#'."""

    __tablename__ = "receipt_tags"
    __table_args__ = (
        UniqueConstraint("receipt_id", "tag", name="uq_receipt_tag"),
        Index("ix_receipt_tags_tag", "tag"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    receipt_id: Mapped[str] = mapped_column(ForeignKey("receipts.id"), index=True)
    tag: Mapped[str] = mapped_column(String)

    receipt: Mapped["Receipt"] = relationship(back_populates="tags")


class ReceiptItem(Base):
    __tablename__ = "receipt_items"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    receipt_id: Mapped[str] = mapped_column(ForeignKey("receipts.id"))

    name: Mapped[str] = mapped_column(String)
    barcode: Mapped[str | None] = mapped_column(String, nullable=True)
    # Raw category string as the store's own API labelled it (mostly NULL) —
    # kept verbatim; the resolved tree category is category_id below.
    category_raw: Mapped[str | None] = mapped_column(String, nullable=True)
    category_id: Mapped[str | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    quantity: Mapped[float] = mapped_column(Numeric(12, 3), default=1)
    unit: Mapped[str | None] = mapped_column(String, nullable=True)
    unit_price: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    total_price: Mapped[float] = mapped_column(Numeric(12, 2))
    discount: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    product_url: Mapped[str | None] = mapped_column(String, nullable=True)
    product_image_url: Mapped[str | None] = mapped_column(String, nullable=True)
    product_image_source_url: Mapped[str | None] = mapped_column(String, nullable=True)
    merchant_name: Mapped[str | None] = mapped_column(String, nullable=True)
    product_image_urls: Mapped[list] = mapped_column(JSON, default=list)
    has_warranty: Mapped[bool | None] = mapped_column(nullable=True)
    warranty_text: Mapped[str | None] = mapped_column(String, nullable=True)
    warranty_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    warranty_checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    receipt: Mapped[Receipt] = relationship(back_populates="items")
    category: Mapped["Category | None"] = relationship()


def derive_unit_price(quantity: float, total_price: float) -> float | None:
    """Calculate a two-decimal unit price from a persisted line total."""
    quantity_value = Decimal(str(quantity or 0))
    if quantity_value <= 0:
        return None
    value = Decimal(str(total_price)) / quantity_value
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@event.listens_for(ReceiptItem, "before_insert")
@event.listens_for(ReceiptItem, "before_update")
def _persist_derived_unit_price(_mapper, _connection, item: ReceiptItem) -> None:
    """Keep unit_price complete for every writer, including scanners/importers."""
    if item.unit_price is None and item.total_price is not None:
        item.unit_price = derive_unit_price(item.quantity, item.total_price)


class TelegramMessageLog(Base):
    """Auditable inbound Telegram message with topic/reply-chain identity."""

    __tablename__ = "telegram_message_log"
    __table_args__ = (
        UniqueConstraint("telegram_update_id", name="uq_telegram_message_log_update"),
        Index("ix_telegram_message_log_conversation", "conversation_key", "received_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    telegram_update_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_type: Mapped[str | None] = mapped_column(String, nullable=True)
    chat_title: Mapped[str | None] = mapped_column(String, nullable=True)
    sender_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    sender_username: Mapped[str | None] = mapped_column(String, nullable=True)
    sender_display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    sender_is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    message_thread_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    reply_to_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    root_message_id: Mapped[int] = mapped_column(BigInteger)
    conversation_key: Mapped[str] = mapped_column(String, index=True)
    content_type: Mapped[str] = mapped_column(String)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_meta: Mapped[dict] = mapped_column(JSON, default=dict)
    authorized: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String, default="received", index=True)
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
