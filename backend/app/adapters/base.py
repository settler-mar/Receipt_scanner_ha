"""Common contract every store adapter implements.

An adapter knows how to: (1) log an account in via phone+SMS (where applicable),
(2) refresh an expired token, and (3) pull the list of receipts + their line items,
translating the store's own response shape into the normalized dataclasses below.

Nothing here talks to the database — adapters are pure API clients. The collector
service (app/services/collector.py) is what persists NormalizedReceipt -> Receipt rows.
"""
from __future__ import annotations

import abc
import dataclasses
import datetime as dt
from typing import Callable


# Browser-backed adapters can rotate their cookie jar while handling a request.
# The refreshed token travels through this transient in-memory `extra` slot and
# is encrypted by service code; it must never be persisted as plain JSON extra.
ROTATED_ACCESS_TOKEN_KEY = "_rotated_access_token"


@dataclasses.dataclass
class NormalizedItem:
    name: str
    total_price: float
    quantity: float = 1
    unit: str | None = None
    unit_price: float | None = None
    barcode: str | None = None
    # Store/seller product code (article, PLU, internal SKU), separate from
    # the retail barcode when a receipt provides both.
    product_code: str | None = None
    category: str | None = None
    discount: float = 0
    product_url: str | None = None
    product_image_url: str | None = None
    # Original remote image URL. The collector copies the image into local
    # storage and replaces product_image_url with our own /api/product-images/
    # URL, while retaining this value for re-downloads if the local file is
    # ever lost.
    product_image_source_url: str | None = None
    merchant_name: str | None = None
    product_image_urls: list[str] = dataclasses.field(default_factory=list)
    has_warranty: bool | None = None
    warranty_text: str | None = None
    warranty_expires_at: dt.datetime | None = None
    warranty_checked_at: dt.datetime | None = None


@dataclasses.dataclass
class NormalizedReceiptComponent:
    """Signed receipt-level amount, never a product line.

    Positive amounts add to the payable total (shipping, tax, service fee);
    negative amounts reduce it (coupon, discount, coins). The provider-neutral
    code/label pair lets adapters expose new breakdowns without schema changes.
    """

    kind: str
    code: str | None
    label: str
    amount: float
    currency: str = "UAH"
    source: str | None = None
    raw: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class NormalizedReceipt:
    external_id: str
    purchased_at: dt.datetime
    total_amount: float
    currency: str = "UAH"
    exchange_rate_to_uah: float | None = None
    shop_name: str | None = None
    shop_address: str | None = None
    payment_type: str | None = None
    # The retail network this purchase belongs to, when the ingest channel is
    # not the store itself — a ДПС fiscal document resolved to a known network.
    # Left None by store adapters: their receipts take the account's own store.
    store_code: str | None = None
    # "offline" (in-store/till) or "online" (delivery/pickup order). Every adapter
    # defaults to "offline" since that's the only channel most stores expose;
    # Fora is the one store so far with a genuine online branch (see fora.py).
    channel: str = "offline"
    items: list[NormalizedItem] = dataclasses.field(default_factory=list)
    components: list[NormalizedReceiptComponent] = dataclasses.field(default_factory=list)
    raw: dict = dataclasses.field(default_factory=dict)
    # Override for the receipt-level discount total. Leave None to have the
    # collector sum item discounts instead — set this when the store's API gives
    # an authoritative total directly but doesn't break it down per line (e.g.
    # Fozzy's online orders only expose sumActualDiscount at the order level).
    total_discount: float | None = None
    # Signed receipt-level amount that remains unexplained after the provider's
    # known line discounts/fees are considered. Positive means an extra charge,
    # negative means an order-level reduction. Never put this on an item.
    total_adjustment: float | None = None
    adjustment_reason: str | None = None
    # Loyalty points/cashback the store's own API says this purchase earned — only
    # set when the store actually reports it (ATB/Fozzy don't expose this anywhere
    # we've found; see RESEARCH.md).
    bonuses: float | None = None
    # Loyalty points/cashback *spent* as payment on this purchase — separate from
    # `bonuses` (earned). Same availability caveat.
    bonuses_used: float | None = None
    # Whether the store's own API says this purchase has a warranty document
    # attached (see get_warranty_document below) — None when the store doesn't
    # report warranty status at all (most stores; see dnipro_m.py).
    has_warranty: bool | None = None
    warranty_expires_at: dt.datetime | None = None
    receipt_url: str | None = None
    # Non-purchase orders (most importantly cancelled online orders) are still
    # returned so persistence can remove a row that was saved before the order
    # changed status. They are never inserted or included in reports.
    excluded: bool = False
    exclusion_reason: str | None = None
    order_status: str | None = None
    tracking_numbers: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class LoyaltyCode:
    """The store's own customer/loyalty-card code, shown at checkout instead of a
    physical card. `display` says how the store's real app renders it — "qr",
    "barcode" (1D, CODE128), or "pdf417" (2D, stacked — Ukrnafta's AZK card) —
    not a suggestion, every store we've checked is consistent about it."""

    value: str
    display: str  # "qr" | "barcode" | "pdf417"
    expires_at: dt.datetime | None = None  # set when the store issues a rotating code


@dataclasses.dataclass
class LoyaltyBalance:
    """Current bonus/cashback wallet balance, as the store's own app shows it."""

    total: float
    currency: str = "UAH"
    # Bonuses the store has earmarked but not yet released for spending (e.g.
    # Epicentr's separate "notActiveBonuses" bucket) — None when the store
    # doesn't distinguish this.
    pending: float | None = None
    # Current personal discount/cashback *rate*, as a percent (e.g. 2 for "2%") —
    # distinct from the bonus wallet total above; some loyalty programs (see
    # dnipro_m.py) tier a purchase-percentage discount separately from points.
    # None when the store doesn't expose a rate like this.
    discount_percent: float | None = None


@dataclasses.dataclass
class FuelPrice:
    """One fuel/gas grade's current pump price plus this account's personal
    per-liter discount on it, as the store's own app shows on the price board
    (see wog.py). `discount` is what's actually deducted per liter at checkout —
    already netted out of `price` is NOT assumed; callers wanting the discounted
    price should compute price - discount themselves."""

    name: str
    price: float
    currency: str = "UAH"
    discount: float | None = None  # personal discount per liter, None if store doesn't expose one
    code: str | None = None  # store's own product code, for matching against receipt line items


@dataclasses.dataclass
class FuelDiscountAllowance:
    """Ukrnafta caps the personal fuel discount at a monthly allowance that
    resets on a fixed date (see ukrnafta.py). This is the account-level state of
    that cap — not per grade. `unit` is "л" or "грн" depending on how the chain
    denominates the allowance."""

    remaining: float
    unit: str = "л"
    resets_at: dt.datetime | None = None


@dataclasses.dataclass
class CatalogProduct:
    """One item found via a store's live product/catalog search — as opposed to
    NormalizedReceipt.items, which only reflects things already bought. Basis
    for cross-store price comparison (see api/products.py's live-search
    endpoint)."""

    name: str
    price: float
    currency: str = "UAH"
    unit: str | None = None
    brand: str | None = None
    image_url: str | None = None
    product_url: str | None = None
    in_stock: bool | None = None  # None when the store's search doesn't report stock at all
    seller_name: str | None = None
    seller_phone: str | None = None


@dataclasses.dataclass
class NormalizedBankTransaction:
    """One line of a bank statement (services/monobank.py) — deliberately not a
    NormalizedReceipt: a bank transaction has no line items and often no
    matching receipt at all (utilities, a phone top-up, a transfer). The
    transaction<->receipt link is a separate, reviewable step in
    services/bank_transactions.py, not something an adapter decides."""

    external_id: str
    occurred_at: dt.datetime
    # Signed, in the bank's own major currency unit (e.g. hryvnia, not kopecks):
    # negative is money leaving the account (a purchase — what can plausibly
    # match a receipt), positive is money coming in (a refund, a transfer in)
    # and is never something a receipt would be issued for.
    amount: float
    currency: str = "UAH"
    mcc: int | None = None
    description: str | None = None
    counterparty: dict = dataclasses.field(default_factory=dict)
    raw: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class AuthResult:
    """What an adapter hands back after a successful login/refresh."""

    access_token: str | None = None
    refresh_token: str | None = None
    expires_at: dt.datetime | None = None
    extra: dict = dataclasses.field(default_factory=dict)
    # True if the flow needs another step (e.g. waiting for the SMS code) before
    # access_token is usable.
    pending: bool = False


class AdapterError(RuntimeError):
    """Raised by adapters for any request/auth failure; message is shown to the user."""


class NotImplementedYet(AdapterError):
    """Raised by stub adapters (METRO, scanner) that are documented but not wired up."""


class BaseAdapter(abc.ABC):
    store_code: str
    # Public catalogs may opt out so product search still works before login.
    catalog_search_requires_auth: bool = True

    # What an Account's `phone_number` slot actually holds for this store, so the
    # UI can label the field honestly instead of asking every store for a phone:
    # "phone" (default), "email" (Tabletki.ua's cabinet logs in by email only) or
    # "phone_or_email" (AliExpress/Liki accept either). Same idea as
    # `capabilities` above — derived from the adapter, never hardcoded per store
    # code in the frontend.
    login_field: str = "phone"

    # Whether fetch_receipts is meant to work at all for this store — distinct
    # from whether it *currently* succeeds (ATB's is fully implemented but
    # blocked by Cloudflare at runtime; that's still "supported", just failing —
    # see atb.py). Only scanner.py (receipts are pushed via photo upload, never
    # pulled) sets this False.
    receipts_supported: bool = True

    # Whether this store is a hand-entry destination — receipts get typed/photo-
    # uploaded by the user rather than pulled from any API (scanner.py). Distinct
    # from receipts_supported being False for other reasons (bars.py: card-only,
    # no purchase history at all) — only scanner.py sets this True, and it's what
    # the UI uses to show a "Додати чек" button instead of collection controls.
    manual_receipts_supported: bool = False

    # Whether this store is a bank/card-statement feed rather than a retail
    # source — transactions get pulled instead of receipts, and matched to
    # existing receipts as a separate step (see fetch_transactions,
    # services/bank_transactions.py). Only bank adapters (monobank.py) set
    # this True; it and receipts_supported are never both True.
    transactions_supported: bool = False

    @property
    def capabilities(self) -> dict[str, bool]:
        """What this adapter actually implements, derived from which optional
        methods it overrides — instead of hand-maintained per-store flags that
        can silently drift out of sync. Used by the API/frontend to decide what
        buttons/columns make sense for a given store (see api/stores.py)
        rather than hardcoding store codes in the UI."""
        cls = type(self)

        def overrides(name: str) -> bool:
            return getattr(cls, name) is not getattr(BaseAdapter, name)

        return {
            "receipts": self.receipts_supported,
            "manual_receipts": self.manual_receipts_supported,
            "transactions": self.transactions_supported,
            "phone_otp_login": overrides("start_login"),
            "phone_password_login": overrides("login_with_password"),
            "oauth_browser_login": overrides("oauth_authorize_url"),
            "card_number_login": overrides("login_with_card"),
            "device_uuid_login": overrides("login_with_device_uuid"),
            "device_registration_login": overrides("login_with_device_registration"),
            "refresh": overrides("refresh"),
            "manual_import": overrides("parse_import"),
            "loyalty_code": overrides("get_loyalty_code"),
            "balance": overrides("get_balance"),
            "fuel_prices": overrides("get_fuel_prices"),
            "catalog_search": overrides("search_catalog"),
            "warranty_document": overrides("get_warranty_document"),
            # A manually pasted token can be checked against the store's API
            # before relying on it — see the "Токен доступу вручну" card.
            "token_test": overrides("test_token"),
        }

    async def login_with_password(self, phone_number: str, password: str) -> AuthResult:
        """Authenticate with a phone/login and password.  Implementations must
        return a session/token and must never put the password into ``extra``."""
        raise NotImplementedYet(f"{self.store_code}: вхід за паролем не підтримується")

    async def start_login(self, phone_number: str) -> AuthResult:
        """Kick off phone+SMS login: send the OTP. Returns pending=True with any
        challenge id the adapter needs to remember (in AuthResult.extra)."""
        raise NotImplementedYet(f"{self.store_code}: OTP login not implemented yet")

    async def confirm_login(self, extra: dict, code: str) -> AuthResult:
        """Confirm the SMS code and exchange it for real tokens."""
        raise NotImplementedYet(f"{self.store_code}: OTP confirmation not implemented yet")

    async def refresh(self, credential_extra: dict, refresh_token: str | None) -> AuthResult:
        """Refresh an expired access token. Default: not supported, caller must re-login."""
        raise AdapterError(f"{self.store_code}: token refresh not supported, please re-authenticate")

    def oauth_authorize_url(self, state: str, redirect_uri: str, code_challenge: str) -> str:
        """For AuthType.oauth_browser stores (e.g. silpo): build the URL the user's
        browser is sent to for an OAuth 2.1 + PKCE authorization-code login. Default:
        not supported."""
        raise NotImplementedYet(f"{self.store_code}: OAuth browser login not implemented")

    async def oauth_exchange(self, code: str, code_verifier: str, redirect_uri: str) -> AuthResult:
        """Exchange the authorization code (from the oauth_authorize_url redirect)
        for tokens. Default: not supported."""
        raise NotImplementedYet(f"{self.store_code}: OAuth browser login not implemented")

    def login_with_card(self, card_number: str) -> AuthResult:
        """For AuthType.card_number stores: register just the physical loyalty-
        card number, no account login at all — for when the store's real
        account API is unreachable but the checkout QR/barcode is derivable
        from the card number alone (see adapters/bars.py). Synchronous — no
        network call, just validates/normalizes the number. Default: not
        supported."""
        raise NotImplementedYet(f"{self.store_code}: вхід за номером картки не підтримується")

    async def login_with_device_uuid(self, device_uuid: str) -> AuthResult:
        """Restore an anonymous installation-backed session (METRO)."""
        raise NotImplementedYet(f"{self.store_code}: вхід за ID пристрою не підтримується")

    async def login_with_device_registration(self, cart_barcode: str, card_barcode: str) -> AuthResult:
        """Create an installation session using a store's physical onboarding identifiers."""
        raise NotImplementedYet(f"{self.store_code}: реєстрація пристрою не підтримується")

    @abc.abstractmethod
    async def fetch_receipts(
        self, access_token: str, extra: dict, since: dt.datetime | None = None
    ) -> list[NormalizedReceipt]:
        """Return receipts (with line items already filled in where the store's API
        allows it in one call; otherwise items may be empty and filled in lazily)."""
        raise NotImplementedError

    def parse_import(self, payload: dict) -> list[NormalizedReceipt]:
        """Parse a manually-captured raw API response (pasted from mitmproxy, when
        this store blocks server-side automation — see atb.py) into normalized
        receipts, without making any network call. Optional; default: unsupported."""
        raise NotImplementedYet(f"{self.store_code}: manual import not supported")

    async def fetch_transactions(
        self,
        access_token: str,
        extra: dict,
        since: dt.datetime | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[NormalizedBankTransaction]:
        """For AuthType.api_token bank-feed adapters only (monobank.py):
        return statement lines since `since`. `on_progress(done, total)`, when
        given, fires as a multi-request fetch proceeds — adapters that only
        ever need one request may ignore it. Optional; default: unsupported."""
        raise NotImplementedYet(f"{self.store_code}: transaction fetch not supported")

    async def test_token(self, access_token: str) -> dict:
        """Cheap round-trip that proves a manually pasted token still works,
        without a full collection run. Returns {"ok": bool, "detail": str}.
        Optional; default: unsupported."""
        raise NotImplementedYet(f"{self.store_code}: token test not supported")

    async def get_loyalty_code(self, access_token: str, extra: dict) -> LoyaltyCode:
        """Fetch the account's loyalty-card code for on-screen display (see
        LoyaltyCode). Optional; default: unsupported. Called live on every UI
        request rather than cached — some stores issue a short-lived rotating
        code (see atb.py)."""
        raise NotImplementedYet(f"{self.store_code}: код картки для цього магазину поки не підтримується")

    async def get_balance(self, access_token: str, extra: dict) -> LoyaltyBalance:
        """Current bonus/cashback balance (see LoyaltyBalance). Optional; default:
        unsupported. Unlike get_loyalty_code this IS cached — collector.py calls it
        once per collection run and stores the result on Account, since it changes
        slowly and accounts.py's list endpoint needs to return it instantly."""
        raise NotImplementedYet(f"{self.store_code}: баланс бонусів для цього магазину поки не підтримується")

    async def get_fuel_prices(self, access_token: str, extra: dict) -> list[FuelPrice]:
        """Current pump prices plus this account's personal per-grade discount
        (see FuelPrice) — only meaningful for fuel-station chains (wog.py).
        Optional; default: unsupported. Called live, same as get_loyalty_code —
        prices change during the day, not worth caching per collection run."""
        raise NotImplementedYet(f"{self.store_code}: ціни на паливо для цього магазину не підтримуються")

    async def get_fuel_discount_allowance(self, access_token: str, extra: dict) -> "FuelDiscountAllowance":
        """State of the monthly fuel-discount cap for this account (see
        FuelDiscountAllowance). Optional; default: unsupported. Called live
        alongside get_fuel_prices from the dashboard."""
        raise NotImplementedYet(f"{self.store_code}: ліміт знижки на паливо для цього магазину не підтримується")

    async def search_catalog(self, access_token: str | None, extra: dict, query: str) -> list[CatalogProduct]:
        """Live full-text search in the store's own product catalog — separate
        from ReceiptItem-based history search (api/products.py's `/search`),
        this hits the store's real search API right now and reflects current
        listing/price, not just what this account has bought before. Optional;
        default: unsupported. Called best-effort across every store with an
        active account (one account per store — see api/products.py's
        live-search endpoint), same pattern as get_balance/get_fuel_prices."""
        raise NotImplementedYet(f"{self.store_code}: пошук по каталогу для цього магазину не підтримується")

    async def get_warranty_document(self, access_token: str | None, extra: dict, external_id: str) -> bytes:
        """Fetch the store's own warranty certificate/card for one purchase, as
        raw file bytes (typically a PDF) — proxied live through
        GET /api/receipts/{id}/warranty rather than stored on disk, since it's
        only occasionally needed and always fetchable again with a valid
        session (see dnipro_m.py). `external_id` is the receipt's own
        Receipt.external_id, which each adapter interprets as whatever order/
        purchase id it needs. Optional; default: unsupported."""
        raise NotImplementedYet(f"{self.store_code}: гарантійний документ для цього магазину не підтримується")
