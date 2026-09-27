from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, PlainSerializer, field_validator


def _to_utc_iso(value: dt.datetime) -> str:
    """SQLite drops tzinfo, so datetimes read back from the DB are naive — but by
    project convention they are always UTC (see app/core/time.py). Emit them with
    an explicit "+00:00" so the frontend's `new Date(...)` parses them as UTC and
    can render them in Europe/Kyiv, instead of silently treating the string as
    the browser's local time."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).isoformat()


# Drop-in for `dt.datetime` on response-model fields.
AwareUTC = Annotated[dt.datetime, PlainSerializer(_to_utc_iso, return_type=str, when_used="json")]


class StoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    code: str
    name: str
    auth_type: str
    enabled: bool
    network_type: str | None = None
    # What this store's adapter actually implements (receipts/loyalty_code/balance/
    # fuel_prices/...) — see BaseAdapter.capabilities. Not a DB column; filled in
    # by api/stores.py from the adapter registry, so the frontend can show/hide
    # UI per store without hardcoding store codes.
    capabilities: dict[str, bool] = {}
    # Whether this store logs in by phone, email or either (see
    # BaseAdapter.login_field) — what the account's `phone_number` really holds.
    login_field: str = "phone"
    # User-facing class of the chain (see services/store_classes.py), filled in
    # by api/stores.py — the UI groups and filters by it.
    store_class: str | None = None
    store_class_name: str | None = None


class StoreClassOut(BaseModel):
    code: str
    name: str


class ScheduleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    enabled: bool
    cron_expression: str
    last_run_at: AwareUTC | None
    next_run_at: AwareUTC | None


class ScheduleUpdate(BaseModel):
    enabled: bool | None = None
    cron_expression: str | None = None


class AccountCreate(BaseModel):
    store_code: str
    label: str
    phone_number: str | None = None


class AccountUpdate(BaseModel):
    """PATCH semantics — only fields actually present in the request body are
    applied (see accounts.py, model_dump(exclude_unset=True))."""

    label: str | None = None
    phone_number: str | None = None


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    label: str
    phone_number: str | None
    status: str
    last_error: str | None
    created_at: AwareUTC
    store: StoreOut
    schedule: ScheduleOut | None = None
    receipt_count: int = 0
    last_collected_at: AwareUTC | None = None
    bonus_balance: float | None = None
    bonus_balance_updated_at: AwareUTC | None = None
    discount_percent: float | None = None
    # Health of the recent scheduled runs. `last_error` is wiped by the next
    # successful collection, so a chain that fails every night and works when
    # the user presses "Зібрати" used to look perfectly fine; these three do not
    # disappear until the failures stop.
    failed_runs_24h: int = 0
    last_failed_at: AwareUTC | None = None
    last_failed_error: str | None = None
    # True while the *most recent* run is a failure — the same rule the sidebar
    # badge and the problem list use, so every screen agrees on what is broken.
    last_run_failed: bool = False


class ImportPayload(BaseModel):
    """Raw API response body pasted from a mitmproxy capture (e.g. АТБ getReceipts),
    for stores where live automated collection is blocked. See adapters/*.py
    parse_import() for the exact expected shape per store."""

    payload: dict


class TokenBootstrap(BaseModel):
    """Manual token entry — bridges the gap until real OTP flows are automated
    per store (see RESEARCH.md). Paste a token captured via mitmproxy."""

    access_token: str
    refresh_token: str | None = None
    expires_at: AwareUTC | None = None
    extra: dict = {}


class LoginStart(BaseModel):
    phone_number: str


class LoginConfirm(BaseModel):
    code: str


class PasswordLogin(BaseModel):
    password: str


class CardLogin(BaseModel):
    card_number: str


class DeviceUuidLogin(BaseModel):
    device_uuid: str


class DeviceRegistrationLogin(BaseModel):
    cart_barcode: str
    card_barcode: str


class LoyaltyCodeOut(BaseModel):
    value: str
    display: str  # "qr" | "barcode" | "pdf417"
    expires_at: AwareUTC | None = None


class FuelPriceOut(BaseModel):
    name: str
    price: float
    currency: str = "UAH"
    discount: float | None = None
    code: str | None = None
    # Price at the previous recorded change, so the UI can show a ↑/↓ arrow.
    # None when there's no earlier snapshot to compare against.
    previous_price: float | None = None


class AccountFuelPricesOut(BaseModel):
    account_id: str
    account_label: str
    store_code: str
    store_name: str
    prices: list[FuelPriceOut]
    # When the board prices were last refreshed for this account.
    checked_at: AwareUTC | None = None
    # Loyalty wallet balance at this chain, cached from the last collection run
    # (see Account.bonus_balance) — None when the store has no wallet or it was
    # never fetched.
    bonus_balance: float | None = None
    bonus_balance_updated_at: AwareUTC | None = None
    # Monthly fuel-discount cap state (Ukrnafta) — see FuelDiscountAllowance.
    discount_allowance: float | None = None
    discount_allowance_unit: str | None = None
    discount_allowance_resets_at: AwareUTC | None = None


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    trigger: str
    status: str
    started_at: AwareUTC
    finished_at: AwareUTC | None
    receipts_found: int
    receipts_new: int
    error_message: str | None
    progress_current: int | None = None
    progress_total: int | None = None


class ExchangeRateBackfillOut(BaseModel):
    checked: int
    updated: int
    missing: int
    queued: bool = False


class DuplicateRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    trigger: str
    status: str
    started_at: AwareUTC
    finished_at: AwareUTC | None
    scanned_receipts: int
    candidates_found: int
    candidates_new: int
    error_message: str | None = None


class DuplicateReceiptItemOut(BaseModel):
    name: str
    quantity: float
    unit: str | None = None
    unit_price: float | None = None
    total_price: float
    discount: float = 0
    barcode: str | None = None
    product_code: str | None = None
    category_id: str | None = None
    product_url: str | None = None
    product_image_url: str | None = None
    merchant_name: str | None = None


class DuplicateReceiptOut(BaseModel):
    id: str
    account_id: str
    account_label: str = ""
    store_code: str
    external_id: str
    purchased_at: AwareUTC
    total_amount: float
    currency: str
    exchange_rate_to_uah: float | None = None
    shop_name: str | None = None
    shop_address: str | None = None
    payment_type: str | None = None
    channel: str
    receipt_url: str | None = None
    tags: list[str] = []
    items: list[DuplicateReceiptItemOut] = []
    raw: dict = {}


class DuplicateGroupOut(BaseModel):
    id: str
    status: str
    score: int
    reasons: list[str] = []
    receipt_ids: list[str] = []
    canonical_receipt_id: str | None = None
    first_seen_at: AwareUTC
    last_seen_at: AwareUTC
    merged_at: AwareUTC | None = None
    rolled_back_at: AwareUTC | None = None
    merge_log: list[dict] = []
    merge_plan: dict | None = None
    receipts: list[DuplicateReceiptOut] = []


class DuplicateMergeChoices(BaseModel):
    receipt_fields: dict[str, str] = {}
    item_name: dict[str, str] = {}
    item_barcode: dict[str, str] = {}
    item_price: dict[str, str] = {}
    item_discount: dict[str, str] = {}
    item_identity: dict[str, str] = {}
    item_money: dict[str, str] = {}


class PromLoginStart(BaseModel):
    session_id: str | None = None


class AliLoginStart(BaseModel):
    password: str
    session_id: str | None = None


class AliLoginOtp(BaseModel):
    session_id: str
    code: str


class AliLoginClose(BaseModel):
    session_id: str


class AliBrowserClick(BaseModel):
    x: float
    y: float


class AliBrowserPointer(BaseModel):
    action: Literal["down", "move", "up"]
    x: float
    y: float


class AliBrowserKey(BaseModel):
    key: str
    alt: bool = False
    ctrl: bool = False
    meta: bool = False
    shift: bool = False


class AliBrowserText(BaseModel):
    value: str


class AliLoginOut(BaseModel):
    session_id: str | None = None
    step: str
    message: str | None = None
    run: RunOut | None = None


class ReceiptItemFileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    kind: str
    note: str | None = None
    created_at: AwareUTC
    # Where the image was pasted from, when it was attached by URL — the file
    # itself is always our own copy (see api/receipts.py link_item_file).
    source_url: str | None = None
    url: str = ""  # set manually in receipts.py _out(), not a column


class ReceiptItemFileLink(BaseModel):
    """Attach an image by URL — it is downloaded and kept as our own copy."""
    url: HttpUrl
    kind: str = "warranty"
    note: str | None = Field(default=None, max_length=500)


class ReceiptItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    barcode: str | None
    product_code: str | None = None
    category_raw: str | None = None
    category_id: str | None = None
    category_path: str | None = None  # resolved in receipts.py _out(), not a column
    quantity: float
    unit: str | None
    unit_price: float | None
    total_price: float
    discount: float
    product_url: str | None = None
    product_image_url: str | None = None
    product_image_source_url: str | None = None
    merchant_name: str | None = None
    product_image_urls: list[str] = []
    has_warranty: bool | None = None
    warranty_text: str | None = None
    warranty_expires_at: AwareUTC | None = None
    warranty_checked_at: AwareUTC | None = None
    files: list[ReceiptItemFileOut] = []


class ReceiptComponentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    kind: str
    code: str | None = None
    label: str
    amount: float
    currency: str
    source: str | None = None


class ReceiptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    account_id: str
    store_code: str
    external_id: str
    purchased_at: AwareUTC
    total_amount: float
    currency: str
    exchange_rate_to_uah: float | None = None
    shop_name: str | None
    seller_phone: str | None = None  # optional public seller contact, kept in raw payloads
    shop_address: str | None
    payment_type: str | None
    channel: str
    total_discount: float
    total_adjustment: float
    adjustment_reason: str | None = None
    components: list[ReceiptComponentOut] = []
    bonuses: float | None = None
    bonuses_used: float | None = None
    receipt_url: str | None = None
    order_status: str | None = None
    tracking_numbers: list[str] = []
    receipt_file_url: str | None = None
    photo_url: str | None = None  # set manually in receipts.py's _out(), not a real column
    has_warranty: bool | None = None
    warranty_text: str | None = None
    warranty_expires_at: AwareUTC | None = None
    warranty_pdf_url: str | None = None  # set manually in receipts.py's _out(), not a real column
    items_total: float = 0
    total_delta: float = 0
    has_total_mismatch: bool = False
    reconciliation_ignored_at: AwareUTC | None = None
    account_label: str = ""
    tags: list[str] = []
    items: list[ReceiptItemOut] = []
    duplicate: bool = False

    @field_validator("tags", mode="before")
    @classmethod
    def _tag_names(cls, v):
        return [getattr(t, "tag", t) for t in (v or [])]

    @field_validator("total_discount", "total_adjustment", mode="before")
    @classmethod
    def _default_zero(cls, v):
        return 0 if v is None else v


class ReceiptListOut(BaseModel):
    total: int
    items: list[ReceiptOut]


class ReceiptProblemBulkAction(BaseModel):
    receipt_ids: list[str] = Field(min_length=1)
    action: Literal[
        "recalculate_amount",
        "recalculate_discount",
        "recalculate_both",
        "ignore",
        "unignore",
        "delete",
    ]


class ReceiptUpdate(BaseModel):
    """PATCH semantics — only fields present in the request body are applied
    (see receipts.py, model_dump(exclude_unset=True)). Deliberately excludes
    account_id/store_code/external_id — those are identity, not data to correct;
    move/delete+recreate the receipt if those are genuinely wrong."""

    purchased_at: AwareUTC | None = None
    total_amount: float | None = None
    currency: str | None = None
    exchange_rate_to_uah: float | None = None
    shop_name: str | None = None
    shop_address: str | None = None
    payment_type: str | None = None
    channel: str | None = None
    total_discount: float | None = None
    total_adjustment: float | None = None
    adjustment_reason: str | None = None
    bonuses: float | None = None
    bonuses_used: float | None = None
    receipt_url: str | None = None
    has_warranty: bool | None = None
    warranty_expires_at: AwareUTC | None = None
    # When present, replaces the whole tag set on the receipt (normalised,
    # de-duplicated). Omit to leave tags untouched.
    tags: list[str] | None = None


class ReceiptTagSummary(BaseModel):
    tag: str
    receipt_count: int
    total_amount: float


class TagRename(BaseModel):
    from_: str = Field(alias="from")
    to: str
    model_config = ConfigDict(populate_by_name=True)


class ReceiptItemUpdate(BaseModel):
    name: str | None = None
    barcode: str | None = None
    product_code: str | None = None
    category_raw: str | None = None
    # Setting this by hand is a sticky choice — see receipts.py update_item /
    # categorize.record_user_choice. Pass null to clear the category.
    category_id: str | None = None
    quantity: float | None = None
    unit: str | None = None
    unit_price: float | None = None
    total_price: float | None = None
    discount: float | None = None
    # Manual warranty bookkeeping — the non-PDF path: for a paper/stamped talon
    # there is nothing to download, so the dates and wording are typed in and
    # the talon itself is attached as an image (see the item files endpoints).
    has_warranty: bool | None = None
    warranty_text: str | None = None
    warranty_expires_at: AwareUTC | None = None


class ReceiptItemCategorize(BaseModel):
    clarification: str = Field(min_length=1, max_length=1000)


class ReceiptItemCreate(BaseModel):
    name: str
    barcode: str | None = None
    product_code: str | None = None
    category_raw: str | None = None
    quantity: float = 1
    unit: str | None = None
    unit_price: float | None = None
    total_price: float
    discount: float = 0


class ShopSuggestion(BaseModel):
    """One (shop_name, shop_address) pair actually seen in existing receipts —
    for autocomplete so the same physical store gets typed identically every
    time instead of drifting into near-duplicate spellings. Sorted by how often
    the pair occurs (see receipts.py::list_shops), most-used first."""

    shop_name: str
    shop_address: str | None
    count: int


class ReceiptCreate(BaseModel):
    """Manual entry — a receipt from a store this project doesn't (yet, or ever
    will) automate collection for. `external_id` is generated server-side
    (manual:<uuid>), not user-supplied — nothing to dedupe against."""

    account_id: str
    purchased_at: AwareUTC
    total_amount: float
    currency: str = "UAH"
    exchange_rate_to_uah: float | None = None
    shop_name: str | None = None
    shop_address: str | None = None
    payment_type: str | None = None
    channel: str = "offline"
    total_discount: float = 0
    total_adjustment: float = 0
    adjustment_reason: str | None = None
    receipt_url: str | None = None
    tags: list[str] = []
    items: list[ReceiptItemCreate] = []


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    parent_id: str | None
    name: str
    slug: str
    path: str
    sort_order: int


class CategoryCreate(BaseModel):
    name: str
    parent_id: str | None = None


class CategoryUpdate(BaseModel):
    name: str | None = None
    parent_id: str | None = None
    sort_order: int | None = None


class CategorySpend(BaseModel):
    """One row of GET /api/dashboard/by-category. `category_id` is null for the
    synthetic "Без категорії" bucket. `direct_total` is spend on items tagged
    exactly this node; `rollup_total` includes every descendant."""

    category_id: str | None
    path: str
    name: str
    depth: int
    direct_total: float
    rollup_total: float
    item_count: int


class CategoryStat(BaseModel):
    """Item counts for one category node (GET /api/categories/stats).
    `direct_items` — receipt line-items tagged exactly this node; `rollup_items`
    — plus every descendant."""

    category_id: str
    direct_items: int
    rollup_items: int


class CategoryStatsOut(BaseModel):
    total_items: int            # receipt line-items with any category
    uncategorized_items: int    # receipt line-items still without a category
    classified_names: int       # distinct product names resolved to a category
    per_category: list[CategoryStat]


class CategoryItemName(BaseModel):
    """One distinct product name inside a category browse listing — grouped by
    normalized name so near-duplicate raw spellings collapse into one row the
    user can reassign in one action."""

    normalized_name: str
    display_name: str      # most common raw spelling, for a readable label
    count: int              # receipt line-items with this normalized name here
    source: str | None      # ProductCategorySource of the underlying alias, if any
    example_category_id: str | None = None   # which node it's tagged under (may
    # differ from the browsed node when include_descendants=True)
    # One representative purchase — a photo if any occurrence has one, else the
    # most recent — so the browse row can link to the receipt and show a thumbnail.
    example_receipt_id: str | None = None
    example_item_id: str | None = None
    example_image_url: str | None = None


class CategoryItemsOut(BaseModel):
    category_id: str
    total_names: int
    total_items: int
    names: list[CategoryItemName]


class CategoryItemsReassign(BaseModel):
    category_id: str                  # scope: only items currently under this node
    normalized_names: list[str]
    target_category_id: str
    include_descendants: bool = False


class CategoryItemsReassignResult(BaseModel):
    names: int
    items_moved: int


class BackfillReset(BaseModel):
    aliases_cleared: int
    items_reapplied: int


class BackfillResult(BaseModel):
    names_classified: int
    batches: int
    queued: bool = False
    # Set only for a forced re-split (force=True) — how much was wiped first.
    reset: BackfillReset | None = None


class SettingOut(BaseModel):
    key: str
    value: Any = None


class SettingUpdate(BaseModel):
    value: Any = None


class DashboardStats(BaseModel):
    total_receipts: int
    total_spent: float
    total_discount: float
    total_bonuses: float
    total_bonuses_used: float
    accounts_active: int
    accounts_needs_attention: int
    last_run_at: AwareUTC | None


class StoreBreakdown(BaseModel):
    store_code: str
    store_name: str
    receipt_count: int
    total_spent: float
    total_discount: float
    total_bonuses: float
    total_bonuses_used: float = 0.0


class ChannelBreakdown(BaseModel):
    channel: str
    receipt_count: int
    total_spent: float


class TimeseriesPoint(BaseModel):
    period: str  # "2026-08-24" | "2026-08" | "2026", depending on the bucket used
    total_spent: float
    receipt_count: int
    # Savings/loyalty for the same bucket — plotted as overlay series on the
    # spend chart. Unlike `total_spent` these never depend on `include_discount`:
    # a discount is a discount whether the bars show net or gross spend.
    total_discount: float = 0.0
    total_bonuses: float = 0.0
    total_bonuses_used: float = 0.0


class StoreCategoryCell(BaseModel):
    """Spend on one category branch at one store (rollup — descendants folded in)."""

    store_code: str
    total: float
    item_count: int


class StoreCategoryRow(BaseModel):
    """One category branch of GET /api/dashboard/store-category, with its spend
    split per store. `category_id` is null for the "Без категорії" bucket."""

    category_id: str | None
    path: str
    name: str
    depth: int
    total: float                 # across every store, this branch + descendants
    item_count: int
    per_store: list[StoreCategoryCell]


class StoreCategoryMatrix(BaseModel):
    stores: list[StoreBreakdown]        # column order — sorted by total_spent desc
    categories: list[StoreCategoryRow]  # every category (filter total>0 client-side)


class SavingsReceipt(BaseModel):
    """One receipt behind the savings/bonus totals (GET
    /api/dashboard/savings/receipts) — raw per-receipt discount and loyalty
    numbers, so a suspicious total can be traced to the receipts that make it up.
    Amounts are UAH-converted, exactly as the dashboard aggregates them."""

    receipt_id: str
    purchased_at: AwareUTC
    store_code: str
    store_name: str
    shop_name: str | None
    total_amount: float
    total_discount: float
    bonuses: float
    bonuses_used: float


class StoreCategoryItem(BaseModel):
    """One receipt line behind a store×category cell (GET
    /api/dashboard/store-category/items) — carries `receipt_id`/`item_id` so the
    category can be edited straight from the drill-down."""

    model_config = ConfigDict(from_attributes=True)

    receipt_id: str
    item_id: str
    name: str
    quantity: float
    unit: str | None
    unit_price: float | None
    total_price: float
    discount: float
    net: float
    purchased_at: AwareUTC
    store_code: str
    category_id: str | None
    category_path: str | None


class FuelPriceHistoryOut(BaseModel):
    account_id: str
    account_label: str
    store_code: str
    store_name: str
    product_code: str | None
    product_name: str
    price: float
    discount: float | None
    currency: str
    recorded_at: AwareUTC


class ProductReceiptOut(BaseModel):
    receipt_id: str
    purchased_at: AwareUTC
    total_amount: float
    currency: str
    total_amount_uah: float | None = None


class ProductPriceOut(BaseModel):
    """One (item name, store) group aggregated across every receipt that ever
    contained it — there's no shared product catalog across chains, each names
    things its own way, so this is plain text grouping over ReceiptItem.name.
    Basis for both "who sells X and for how much" search and, longer term, a
    shopping-list-vs-store optimizer (see SERVICE.md)."""

    name: str
    store_code: str
    store_name: str
    unit: str | None
    currency: str
    last_price: float
    last_purchased_at: AwareUTC
    min_price: float
    max_price: float
    avg_price: float
    times_bought: int
    image_url: str | None = None
    last_receipt_id: str | None = None
    receipts: list[ProductReceiptOut] = []
    last_price_uah: float | None = None
    min_price_uah: float | None = None
    max_price_uah: float | None = None
    avg_price_uah: float | None = None
    exchange_rate_to_uah: float | None = None


class LiveProductOut(BaseModel):
    """One hit from a store's own live catalog-search API, queried right now —
    as opposed to ProductPriceOut, which is derived from past receipts. See
    api/products.py's /live-search and BaseAdapter.search_catalog."""

    store_code: str
    store_name: str
    name: str
    price: float
    currency: str = "UAH"
    unit: str | None = None
    brand: str | None = None
    image_url: str | None = None
    product_url: str | None = None
    in_stock: bool | None = None
    seller_name: str | None = None
    seller_phone: str | None = None


class WarrantyProductOut(BaseModel):
    item_id: str
    receipt_id: str
    name: str
    store_code: str
    store_name: str
    account_label: str
    purchased_at: AwareUTC
    purchase_price: float
    warranty_text: str | None = None
    warranty_expires_at: AwareUTC | None = None
    expired: bool
    product_url: str | None = None
    image_url: str | None = None
    # Hand-attached talon/product photos for this line (api/receipts.py item files).
    file_urls: list[str] = []


# --------------------------------------------------------------------------- #
# Bank transactions — Monobank first (services/bank_transactions.py,
# adapters/monobank.py, api/bank_transactions.py). One Account per bank
# connection, same as any other store — see AuthType.api_token.
# --------------------------------------------------------------------------- #
class BankTransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    account_id: str
    account_label: str = ""
    store_code: str
    external_id: str
    amount: float
    currency: str
    mcc: int | None = None
    description: str | None = None
    counterparty_raw: dict = {}
    occurred_at: AwareUTC
    status: str
    linked_receipt_id: str | None = None
    ignore_reason: str | None = None
    category_override_id: str | None = None
    created_at: AwareUTC

    @field_validator("status", mode="before")
    @classmethod
    def _status_value(cls, v):
        return getattr(v, "value", v)


class BankTransactionLink(BaseModel):
    receipt_id: str


class BankTransactionIgnore(BaseModel):
    reason: str


class BankTransactionUpdate(BaseModel):
    category_override_id: str | None = None


class BankTransactionSummaryRow(BaseModel):
    category_id: str | None = None
    category_name: str | None = None
    total_amount: float
    count: int


class TokenTestResult(BaseModel):
    ok: bool
    detail: str


class BankTransactionPollIn(BaseModel):
    # A deep-history backfill: fetch from this date instead of the account's
    # incremental poll cursor. Absent/None = the normal quick incremental poll.
    since: AwareUTC | None = None


class BankTransactionRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    label: str
    counterparty_match: str
    priority: int
    # [{condition: {kind, ...}, category_id, ignore_reason}, ...] — see
    # services/bank_transaction_rules.py for the condition kinds; kept as
    # passthrough dicts rather than a strict nested schema since the frontend
    # both writes and reads this shape directly.
    branches: list[dict] = []
    updated_at: AwareUTC


class BankTransactionRuleCreate(BaseModel):
    label: str
    counterparty_match: str
    priority: int = 0
    branches: list[dict] = []


class BankTransactionRuleUpdate(BaseModel):
    label: str | None = None
    counterparty_match: str | None = None
    priority: int | None = None
    branches: list[dict] | None = None


class BankTransactionRuleApplyResult(BaseModel):
    updated: int
