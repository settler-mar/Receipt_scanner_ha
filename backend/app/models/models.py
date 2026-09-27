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
    # A pasted personal API token, no login flow at all — supports multiple
    # independent accounts (each with its own token) the same way any other
    # store does, via Account+Credential. First user: Monobank statement API.
    api_token = "api_token"


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


class BankTransactionStatus(str, enum.Enum):
    unmatched = "unmatched"    # no candidate receipt found
    matched = "matched"        # linked to exactly one receipt (auto or manual)
    needs_review = "needs_review"  # more than one plausible receipt candidate
    ignored = "ignored"        # user marked it as never producing a receipt


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
    bank_transactions: Mapped[list["BankTransaction"]] = relationship(
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
    # Coarse step progress for a long-running run (e.g. a deep bank-statement
    # backfill chunked into several throttled requests) — None/None for a run
    # that doesn't report progress, so existing receipt-collection rows are
    # unaffected. Updated (and committed) as the run's own background task
    # proceeds; polled by the account page while status == running.
    progress_current: Mapped[int | None] = mapped_column(nullable=True)
    progress_total: Mapped[int | None] = mapped_column(nullable=True)

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


class AppEvent(Base):
    """One line of the unified operational journal (страница «Журнал»).

    Most subsystems already keep their own history — collection runs, scan jobs,
    duplicate runs, Telegram messages, the AI ring buffer — and `api/logs.py`
    merges those on read. This table is for events that have nowhere else to
    live: scheduled maintenance, automatic shopping-list categorization, and
    anything else a background job wants to leave a trace of.
    """

    __tablename__ = "app_events"
    __table_args__ = (Index("ix_app_events_created_source", "created_at", "source"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )
    source: Mapped[str] = mapped_column(String, index=True)   # shopping | scheduler | ai | ...
    level: Mapped[str] = mapped_column(String, default="info")  # info | warning | error
    title: Mapped[str] = mapped_column(String)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class ReceiptComponent(Base):
    """Signed receipt-level amount from a provider's order breakdown."""

    __tablename__ = "receipt_components"
    __table_args__ = (Index("ix_receipt_components_receipt", "receipt_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    receipt_id: Mapped[str] = mapped_column(ForeignKey("receipts.id"), index=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    code: Mapped[str | None] = mapped_column(String, nullable=True)
    label: Mapped[str] = mapped_column(String)
    amount: Mapped[float] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String, default="UAH")
    source: Mapped[str | None] = mapped_column(String, nullable=True)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)

    receipt: Mapped["Receipt"] = relationship(back_populates="components")


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
    # Signed receipt-level correction for a provider amount that cannot be
    # attributed to a product (for example an unexplained AliExpress order
    # discount or extra charge). This is deliberately not a ReceiptItem field.
    total_adjustment: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    adjustment_reason: Mapped[str | None] = mapped_column(String, nullable=True)
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

    # A user may deliberately hide a known line-total mismatch from the open
    # problems queue. Keep the timestamp so the decision remains auditable.
    reconciliation_ignored_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Denormalized reconciliation snapshot. It is refreshed when receipt data
    # changes so list pages and navigation never aggregate all item rows.
    items_total: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    components_total: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    reconciled_total: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    reconciliation_delta: Mapped[float] = mapped_column(Numeric(12, 2), default=0, index=True)
    reconciliation_status: Mapped[str] = mapped_column(String, default="ok", index=True)
    reconciliation_calculated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    raw: Mapped[dict] = mapped_column(JSON, default=dict)  # original payload, for future re-parsing
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    account: Mapped[Account] = relationship(back_populates="receipts")
    items: Mapped[list["ReceiptItem"]] = relationship(back_populates="receipt", cascade="all, delete-orphan")
    components: Mapped[list[ReceiptComponent]] = relationship(
        back_populates="receipt", cascade="all, delete-orphan", order_by="ReceiptComponent.id"
    )
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
    product_code: Mapped[str | None] = mapped_column(String, nullable=True)
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
    files: Mapped[list["ReceiptItemFile"]] = relationship(
        back_populates="item", cascade="all, delete-orphan", order_by="ReceiptItemFile.created_at"
    )


class ReceiptItemFile(Base):
    """A manually attached image for one line item — a photo of the product or
    of a paper warranty card. This is the non-PDF path for warranties: stores
    that hand out a stamped paper talon (or none at all) leave nothing to
    download, so the scan/photo taken by hand is the only document there is.
    An attachment is either an uploaded file (data/item_files/, same on-disk
    pattern as receipt photos — see api/receipts.py) or just a link to an image
    hosted elsewhere; `filename` and `source_url` are mutually exclusive."""

    __tablename__ = "receipt_item_files"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    item_id: Mapped[str] = mapped_column(
        ForeignKey("receipt_items.id", ondelete="CASCADE"), index=True
    )
    # "warranty" - гарантійний талон / чек на гарантію, "photo" - the product itself.
    kind: Mapped[str] = mapped_column(String, default="warranty")
    # Exactly one of the two is set: an uploaded copy under data/item_files/, or
    # a link to an image that already lives somewhere else (store's product
    # page, Drive, a chat). A link costs nothing to keep and can rot; an upload
    # is ours forever — both are legitimate, so neither is forced.
    filename: Mapped[str | None] = mapped_column(String, nullable=True)
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)
    original_name: Mapped[str | None] = mapped_column(String, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String, nullable=True)
    note: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    item: Mapped[ReceiptItem] = relationship(back_populates="files")


def derive_unit_price(quantity: float, total_price: float) -> float | None:
    """Calculate a two-decimal unit price from a persisted line total."""
    quantity_value = Decimal(str(quantity or 0))
    if quantity_value <= 0:
        return None
    value = Decimal(str(total_price)) / quantity_value
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def derive_quantity(unit_price: float, total_price: float) -> float | None:
    """Calculate a three-decimal quantity (including weight) from line prices."""
    unit_value = Decimal(str(unit_price or 0))
    if unit_value <= 0:
        return None
    value = Decimal(str(total_price or 0)) / unit_value
    return float(value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


def _is_weight_or_volume_unit(unit: str | None) -> bool:
    return (unit or "").strip().lower() in {"кг", "kg", "г", "g", "л", "l", "мл", "ml"}


@event.listens_for(ReceiptItem, "before_insert")
@event.listens_for(ReceiptItem, "before_update")
def _persist_derived_unit_price(_mapper, _connection, item: ReceiptItem) -> None:
    """Keep manually entered line prices and quantity complete for every writer."""
    # A blank quantity is sent as 0 by the manual form. In that case the two
    # entered prices are enough to recover the sold quantity/weight.
    if (
        item.unit_price is not None
        and (item.quantity is not None and item.quantity <= 0 or _is_weight_or_volume_unit(item.unit))
    ):
        derived_quantity = derive_quantity(item.unit_price, item.total_price)
        if derived_quantity is not None:
            item.quantity = derived_quantity
    # Manual forms may leave the line total at its initial zero while a unit
    # price is already entered. Persist the mathematically expected line total.
    if item.total_price is not None and item.total_price <= 0 and item.unit_price is not None and item.quantity:
        item.total_price = float(
            (Decimal(str(item.unit_price)) * Decimal(str(item.quantity))).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        )
    if item.unit_price is None and item.total_price is not None:
        item.unit_price = derive_unit_price(item.quantity, item.total_price)


class BankTransaction(Base):
    """One line of a bank statement (services/monobank.py + future bank
    adapters) — an Account/Credential of a store with AuthType.api_token, same
    as any other store, so more than one bank *and* more than one connection
    per bank (several personal tokens) both fall out for free.

    A separate entity from Receipt on purpose: a single transaction can cover
    several receipts (one card swipe for a shared basket) and a single receipt
    can be paid across several transactions (installments, a tip added after
    the fact) — so v1 keeps a nullable single link for the clean 1:1 case and
    leaves anything ambiguous in `needs_review` for a person to resolve,
    instead of forcing a guess.
    """

    __tablename__ = "bank_transactions"
    __table_args__ = (
        UniqueConstraint("account_id", "external_id", name="uq_bank_transaction_account_external"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    store_code: Mapped[str] = mapped_column(String, index=True)  # denormalized, like Receipt.store_code
    external_id: Mapped[str] = mapped_column(String, index=True)  # the bank's own transaction id

    amount: Mapped[float] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String, default="UAH")
    mcc: Mapped[int | None] = mapped_column(nullable=True)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    # comment, the bank's own receiptId, masked card info, etc — identifiers
    # only, kept verbatim for the review UI and future re-matching.
    counterparty_raw: Mapped[dict] = mapped_column(JSON, default=dict)

    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)

    status: Mapped[BankTransactionStatus] = mapped_column(
        Enum(BankTransactionStatus), default=BankTransactionStatus.unmatched, index=True
    )
    linked_receipt_id: Mapped[str | None] = mapped_column(
        ForeignKey("receipts.id"), nullable=True, index=True
    )
    # Free label for a transaction that never produces a receipt at all
    # ("utilities" / "phone_topup" / "pocket_money" / custom text).
    ignore_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    category_override_id: Mapped[str | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    # Which BankTransactionRule last set ignore_reason/category_override_id
    # here, if any — services/bank_transaction_rules.py's marker for "safe to
    # recompute on the next pass" vs. "a person chose this by hand, leave it".
    auto_rule_id: Mapped[str | None] = mapped_column(
        ForeignKey("bank_transaction_rules.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    account: Mapped["Account"] = relationship(back_populates="bank_transactions")
    linked_receipt: Mapped["Receipt | None"] = relationship()
    category_override: Mapped["Category | None"] = relationship()


class BankTransactionRule(Base):
    """A named auto-categorization rule for bank transactions, matched by a
    counterparty substring (Monobank puts a person's name in `description` for
    peer-to-peer transfers — there's no merchant/MCC to key off of).

    `branches` is an ordered list of {condition, category_id, ignore_reason}
    dicts, evaluated top to bottom — the first branch whose condition holds
    wins. Conditions can see the transaction's amount and its position/count
    among same-rule matches in the same calendar month, which is what makes
    "the first transfer to Женя each month is rent, the rest are utilities,
    and if there's only one that month it's utilities" expressible as one
    rule instead of a hardcoded special case. See
    services/bank_transaction_rules.py for the condition kinds and the
    evaluation/grouping logic.
    """

    __tablename__ = "bank_transaction_rules"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    label: Mapped[str] = mapped_column(String)
    counterparty_match: Mapped[str] = mapped_column(String)
    # Lower runs first; only meaningful for ordering *between* rules — each
    # rule's own branches already have their own evaluation order.
    priority: Mapped[int] = mapped_column(default=0)
    branches: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


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
