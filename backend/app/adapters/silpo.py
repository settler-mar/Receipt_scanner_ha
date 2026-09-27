"""Сільпо (com.silpo.online) — via Silpo's own official MCP server, not app
reverse-engineering. See https://ai-factory.silpo.ua/docs/mcp.

Auth: OAuth 2.1 authorization_code + PKCE against mcp.silpo.ua (endpoints
discovered from its /.well-known/oauth-authorization-server document). There's no
phone+SMS API to call directly — login happens on the real auth.silpo.ua page in
the user's browser (the same "Власний Рахунок" flow the app itself uses). That's
AuthType.oauth_browser: see app/api/oauth.py for the redirect dance; this adapter's
oauth_authorize_url/oauth_exchange plug into it instead of start_login/confirm_login.

CLIENT_ID below was obtained via Dynamic Client Registration (POST /register,
a public/no-secret client — token_endpoint_auth_method "none") with redirect_uri
hardcoded to this deployment's own callback (settings.base_url +
"/api/oauth/silpo/callback"). Re-register (see RESEARCH.md) if that base_url ever
changes — the provider checks it exactly. Re-registered 2026-09-25 after the
previous client_id started bouncing every /authorize attempt back to itself
(its redirect_uri no longer matched what we send — root cause never confirmed,
DCR has no read/update endpoint to compare, only re-register).

Receipts come from two MCP tools (run `tools/list` after auth for the live schema):
  silpo_get_my_offline_orders — in-store purchases. Its schema *requires*
      branchId/deliveryType/timeslotStart/timeslotEnd (meant for an AI shopping
      agent to check product availability) but doesn't actually validate them
      against anything for this read-only call — confirmed empirically that
      placeholder values still return real order history.
  silpo_get_my_online_orders — delivery/pickup orders placed via the app/site.
      Confirmed live but the test account had none, so the per-order shape in
      _normalize_online is a best-effort guess from field-name conventions, not a
      captured sample. Normalization is defensive (skips + logs on anything
      unrecognized) until a real one shows up to confirm the exact fields.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import re
import urllib.parse
from app.core.time import store_local_to_utc

import httpx
from exceptiongroup import BaseExceptionGroup
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from app.adapters.base import (
    AdapterError,
    AuthResult,
    BaseAdapter,
    CatalogProduct,
    LoyaltyBalance,
    LoyaltyCode,
    NormalizedItem,
    NormalizedReceipt,
    NormalizedReceiptComponent,
)

logger = logging.getLogger(__name__)

CLIENT_ID = "Tu-0lnMryiNB3e4_"
AUTHORIZE_ENDPOINT = "https://mcp.silpo.ua/authorize"
TOKEN_ENDPOINT = "https://mcp.silpo.ua/token"
MCP_URL = "https://mcp.silpo.ua/mcp"

# Placeholder cart-context values — silpo_get_my_offline_orders requires these but
# doesn't validate them for a plain history read (see module docstring).
_DUMMY_BRANCH_ID = "0"
_DUMMY_DELIVERY_TYPE = "SelfPickup"
_DUMMY_TIMESLOT_START = "2000-01-01T00:00:00Z"
_DUMMY_TIMESLOT_END = "2000-01-01T01:00:00Z"

_EARLIEST = "2000-01-01T00:00:00Z"


def _unwrap_exception_group(exc: BaseExceptionGroup) -> BaseException:
    """mcp's streamablehttp_client/ClientSession run their read/write loops in an
    anyio task group; any failure there (expired token, dropped connection,
    timeout) surfaces as an opaque "unhandled errors in a TaskGroup" instead of
    the actual cause. Unwrap to the first leaf exception so callers (collector's
    error_message, is_auth_failure) see the real error."""
    inner: BaseException = exc
    while isinstance(inner, BaseExceptionGroup) and inner.exceptions:
        inner = inner.exceptions[0]
    return inner


@contextlib.asynccontextmanager
async def _mcp_session(access_token: str):
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with streamablehttp_client(MCP_URL, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    except BaseExceptionGroup as eg:
        raise _unwrap_exception_group(eg) from eg


class SilpoAdapter(BaseAdapter):
    store_code = "silpo"

    def oauth_authorize_url(self, state: str, redirect_uri: str, code_challenge: str) -> str:
        params = {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        return f"{AUTHORIZE_ENDPOINT}?{urllib.parse.urlencode(params)}"

    async def oauth_exchange(self, code: str, code_verifier: str, redirect_uri: str) -> AuthResult:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                TOKEN_ENDPOINT,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": CLIENT_ID,
                    "code_verifier": code_verifier,
                },
            )
        if resp.status_code != 200:
            raise AdapterError(f"Сільпо: не вдалось обміняти код на токен ({resp.status_code}): {resp.text[:200]}")
        payload = resp.json()
        expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=payload.get("expires_in", 2592000))
        return AuthResult(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token"),
            expires_at=expires_at,
        )

    async def refresh(self, credential_extra: dict, refresh_token: str | None) -> AuthResult:
        if not refresh_token:
            raise AdapterError("Сільпо: немає refresh_token, потрібен повторний вхід")
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                TOKEN_ENDPOINT,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": CLIENT_ID,
                },
            )
        if resp.status_code != 200:
            raise AdapterError(f"Сільпо: не вдалось оновити токен ({resp.status_code}): {resp.text[:200]}")
        payload = resp.json()
        expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=payload.get("expires_in", 2592000))
        return AuthResult(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token", refresh_token),
            expires_at=expires_at,
        )

    async def fetch_receipts(
        self, access_token: str, extra: dict, since: dt.datetime | None = None
    ) -> list[NormalizedReceipt]:
        date_start = since.astimezone(dt.timezone.utc).isoformat() if since else _EARLIEST
        receipts: list[NormalizedReceipt] = []

        async with _mcp_session(access_token) as session:
            receipts += await self._fetch_offline(session, date_start)
            receipts += await self._fetch_online(session, since)

        return receipts

    async def get_loyalty_code(self, access_token: str, extra: dict) -> LoyaltyCode:
        async with _mcp_session(access_token) as session:
            data = await self._call(session, "silpo_get_loyalty_info", {})
        barcode = ((data.get("loyalty") or {}).get("card") or {}).get("barcode")
        if not barcode:
            raise AdapterError("Сільпо: код картки відсутній у відповіді")
        return LoyaltyCode(value=barcode, display="qr")

    async def get_balance(self, access_token: str, extra: dict) -> LoyaltyBalance:
        async with _mcp_session(access_token) as session:
            data = await self._call(session, "silpo_get_loyalty_info", {})
        balance = (data.get("loyalty") or {}).get("balance") or {}
        if "total" not in balance:
            raise AdapterError("Сільпо: баланс відсутній у відповіді")
        return LoyaltyBalance(total=float(balance["total"]), currency=balance.get("currency", "UAH"))

    async def search_catalog(self, access_token: str | None, extra: dict, query: str) -> list[CatalogProduct]:
        """silpo_find_products_batch — unlike silpo_get_my_offline_orders' dummy
        branchId/deliveryType/timeslot (unvalidated there for a plain history
        read), this tool actually uses them to pick which branch's stock/prices
        to search — confirmed live: a dummy branchId returns 0 results for
        every query. So fetch this account's own real, current cart context
        first (silpo_get_my_shopping_cart -> silpo_get_shopping_cart_by_id) and
        reuse whatever delivery type/branch/timeslot it's already set to, same
        as the real app would for its own search. No canonical product-page URL
        in the response, so product_url stays unset — same as Fora/Fozzy."""
        if not access_token:
            raise AdapterError("Сільпо: пошук по каталогу потребує токена")
        async with _mcp_session(access_token) as session:
            cart_ref = await self._call(session, "silpo_get_my_shopping_cart", {})
            cart_id = cart_ref.get("shoppingCartId")
            if not cart_id:
                raise AdapterError("Сільпо: не вдалось отримати кошик для пошуку")
            cart = (
                await self._call(session, "silpo_get_shopping_cart_by_id", {"shoppingCartId": cart_id})
            ).get("cart") or {}
            shipments = cart.get("shipments") or []
            branch_id = shipments[0].get("branchId") if shipments else None
            timeslot = cart.get("timeslot") or {}
            if not branch_id or not timeslot.get("start") or not timeslot.get("end"):
                raise AdapterError("Сільпо: кошик не має налаштованої доставки/філії — пошук недоступний")
            data = await self._call(
                session,
                "silpo_find_products_batch",
                {
                    "branchId": branch_id,
                    "deliveryType": cart.get("deliveryType") or "DeliveryHome",
                    "timeslotStart": timeslot["start"],
                    "timeslotEnd": timeslot["end"],
                    "products": [query],
                    "limit": 20,
                },
            )
        products: list[CatalogProduct] = []
        for q in data.get("queries") or []:
            for item in q.get("products") or []:
                try:
                    price = float(item["price"])
                except (KeyError, TypeError, ValueError):
                    continue
                products.append(
                    CatalogProduct(
                        name=item.get("name") or "",
                        price=price,
                        unit=item.get("displayRatio"),
                        image_url=item.get("image") or None,
                        in_stock=item.get("available"),
                    )
                )
        return products

    async def _fetch_offline(self, session: ClientSession, date_start: str) -> list[NormalizedReceipt]:
        out: list[NormalizedReceipt] = []
        offset = 0
        limit = 10  # silpo_get_my_offline_orders caps limit at 10
        while True:
            data = await self._call(session, "silpo_get_my_offline_orders", {
                "branchId": _DUMMY_BRANCH_ID,
                "deliveryType": _DUMMY_DELIVERY_TYPE,
                "timeslotStart": _DUMMY_TIMESLOT_START,
                "timeslotEnd": _DUMMY_TIMESLOT_END,
                "limit": limit,
                "offset": offset,
                "dateStart": date_start,
            })
            orders = data.get("orders") or []
            if not orders:
                break
            for entry in orders:
                try:
                    out.append(_normalize_offline(entry))
                except Exception:
                    logger.exception("Сільпо: не вдалось розібрати офлайн-чек %r", entry.get("receiptUrl"))
            total = (data.get("meta") or {}).get("total", len(out))
            offset += len(orders)
            if offset >= total or len(orders) < limit:
                break
        return out

    async def _fetch_online(self, session: ClientSession, since: dt.datetime | None) -> list[NormalizedReceipt]:
        out: list[NormalizedReceipt] = []
        offset = 0
        limit = 50
        while True:
            data = await self._call(session, "silpo_get_my_online_orders", {
                "limit": limit,
                "offset": offset,
            })
            orders = data.get("orders") or []
            if not orders:
                break
            stop = False
            for entry in orders:
                try:
                    normalized = _normalize_online(entry)
                except Exception:
                    logger.exception("Сільпо: не вдалось розібрати онлайн-замовлення %r", entry.get("id"))
                    continue
                if since and normalized.purchased_at < since:
                    stop = True
                    break
                out.append(normalized)
            total = (data.get("meta") or {}).get("total", len(out))
            offset += len(orders)
            if stop or offset >= total or len(orders) < limit:
                break
        return out

    @staticmethod
    async def _call(session: ClientSession, tool: str, args: dict) -> dict:
        try:
            result = await session.call_tool(tool, args)
        except Exception as exc:  # noqa: BLE001 — mcp raises its own McpError types
            raise AdapterError(f"Сільпо: помилка MCP-виклику {tool}: {exc}") from exc
        if result.isError:
            raise AdapterError(f"Сільпо: {tool} повернув помилку: {result.content}")
        for c in result.content:
            text = getattr(c, "text", None)
            if text:
                return json.loads(text)
        return {}


# Silpo's `unit` field is overloaded. For weight-sold products it's a genuine
# measurement unit and `quantity` is fractional (e.g. quantity=0.864, unit="кг").
# For count-sold products it's actually the *package size* — "1,5л", "500г",
# "14г" — with `quantity` being a plain count of packages (e.g. quantity=3,
# unit="14г" means three 14g candies, not "3 14г"). Detected by a leading digit;
# folded into the item name instead, with the quantity unit defaulted to "шт".
_PACKAGE_SIZE_RE = re.compile(r"^\d+([.,]\d+)?\s*[а-яіїєґ]+\.?$", re.IGNORECASE)


def _split_unit(raw_unit: str | None, name: str) -> tuple[str | None, str]:
    if raw_unit and _PACKAGE_SIZE_RE.match(raw_unit.strip()):
        size = raw_unit.strip()
        return "шт", f"{name}, {size}" if name else size
    return raw_unit, name


def _parse_dt(value: str) -> dt.datetime:
    # Silpo's timestamps have no timezone suffix (e.g. "2026-08-20T20:21:41") —
    # that is Kyiv local time (see app/core/time.py).
    return store_local_to_utc(dt.datetime.fromisoformat(value))


def _normalize_offline(entry: dict) -> NormalizedReceipt:
    receipt_url = entry.get("receiptUrl") or ""
    slug = receipt_url.rstrip("/").rsplit("/", 1)[-1] if receipt_url else None
    external_id = slug or f"{entry.get('filId')}:{entry.get('createdAt')}"

    products = entry.get("products") or []
    items = []
    for p in products:
        quantity = float(p.get("quantity") or 1)
        price = _to_float(p.get("price"))
        unit, name = _split_unit(p.get("unit"), (p.get("name") or "").strip())
        items.append(
            NormalizedItem(
                name=name,
                quantity=quantity,
                unit=unit,
                unit_price=price,
                total_price=round(quantity * (price or 0), 2),
                barcode=str(p["lagerId"]) if p.get("lagerId") is not None else None,
                product_image_url=p.get("image") or p.get("imageUrl"),
            )
        )

    shop_address = ", ".join(x for x in (entry.get("cityName"), entry.get("filialName")) if x) or None

    # rewards[] mixes bonus spending with other promo mechanics — CN_REWARD_VOUCHERS
    # is specifically "Використані балобонуси" (bonuses used as payment); confirmed
    # against a real example, so only sum that group as bonuses_used.
    bonuses_used = sum(
        r.get("applyRewardAmount") or 0
        for r in (entry.get("rewards") or [])
        if r.get("rewardGroupCodeName") == "CN_REWARD_VOUCHERS"
    ) or None
    components = []
    if bonuses_used:
        components.append(NormalizedReceiptComponent(
            kind="discount", code="bonuses_used", label="Використані балобонуси",
            amount=round(-bonuses_used, 2), currency="UAH", source="silpo.rewards",
            raw={"rewards": [r for r in (entry.get("rewards") or []) if r.get("rewardGroupCodeName") == "CN_REWARD_VOUCHERS"]},
        ))

    return NormalizedReceipt(
        external_id=external_id,
        purchased_at=_parse_dt(entry["createdAt"]),
        total_amount=_to_float(entry.get("sumReg")) or 0,
        shop_name=entry.get("filialName"),
        shop_address=shop_address,
        channel="offline",
        items=items,
        components=components,
        raw=entry,
        total_discount=_to_float(entry.get("sumDiscount")),
        bonuses=_to_float(entry.get("accruedBalaBonusesSum")),
        bonuses_used=bonuses_used,
    )


def _normalize_online(entry: dict) -> NormalizedReceipt:
    order_id = entry.get("id") or entry.get("orderId") or entry.get("orderNumber")
    created = entry.get("createdAt") or entry.get("orderedAt") or entry.get("date") or entry.get("dateCreated")
    if not order_id or not created:
        raise ValueError(f"unrecognized online order shape: keys={list(entry.keys())}")

    total = (
        entry.get("sumReg") or entry.get("total") or entry.get("sum")
        or entry.get("amount") or entry.get("totalAmount")
    )
    discount = entry.get("sumDiscount") or entry.get("discount") or entry.get("totalDiscount")

    raw_items = entry.get("products") or entry.get("items") or entry.get("lines") or []
    items = []
    for p in raw_items:
        quantity = float(p.get("quantity") or p.get("qty") or 1)
        price = _to_float(p.get("price") or p.get("unitPrice"))
        unit, name = _split_unit(p.get("unit"), (p.get("name") or p.get("title") or "").strip())
        items.append(
            NormalizedItem(
                name=name,
                quantity=quantity,
                unit=unit,
                unit_price=price,
                total_price=round(quantity * (price or 0), 2),
                barcode=str(p["lagerId"]) if p.get("lagerId") is not None else None,
                product_image_url=p.get("image") or p.get("imageUrl"),
            )
        )

    bonuses_used = sum(
        r.get("applyRewardAmount") or 0
        for r in (entry.get("rewards") or [])
        if r.get("rewardGroupCodeName") == "CN_REWARD_VOUCHERS"
    ) or None
    components = []
    if bonuses_used:
        components.append(NormalizedReceiptComponent(
            kind="discount", code="bonuses_used", label="Використані балобонуси",
            amount=round(-bonuses_used, 2), currency="UAH", source="silpo.rewards",
            raw={"rewards": [r for r in (entry.get("rewards") or []) if r.get("rewardGroupCodeName") == "CN_REWARD_VOUCHERS"]},
        ))

    return NormalizedReceipt(
        external_id=str(order_id),
        purchased_at=_parse_dt(created),
        total_amount=_to_float(total) or 0,
        channel="online",
        items=items,
        components=components,
        raw=entry,
        total_discount=_to_float(discount),
    )


def _to_float(value) -> float | None:
    if value in (None, ""):
        return None
    return float(str(value).replace(",", "."))
