"""Fora (ua.fora.android).

Auth: phone + SMS OTP -> "Token" JWT, over the same JSON-RPC gateway as everything
else. No captcha involved (unlike АТБ/Fozzy), so fully automatable:

  1. {"Method": "SendOtpV2", "Data": {"phone": "+380...", "guid": <client uuid4>, "captcha": ""}}
     -> {"otpSend": true, "retryIntervalInSeconds": 90, "error": {"errorCode": 0, ...}}
  2. {"Method": "ConfirmationOtp_V2", "Data": {"phone": "+380...", "guid": <same uuid4>, "otpCode": "123456"}}
     -> {"tokens": {"accessToken": {"expireat": <unix>, "value": <JWT>},
                     "refreshToken": {"expireat": <unix>, "value": <JWT>}}, ...}

`accessToken.value` is what's sent as `Authorization: Token <...>` on every other call.

Refresh (found live, 28.08.2026 — RESEARCH.md's original note that "no refresh
RPC method was found" is now stale): confirmed against the real app hitting an
already-expired accessToken. It calls a plain 401 first (`TOKEN_EXPIRED`,
errorCode 12), then:
  {"Method": "RefreshToken", "Data": {"headerInfo": {"token": <refreshToken>,
   "deviceInfo": <same object as _USER_INFO below>}}}
  Authorization: Token <refreshToken>   (the REFRESH token, not the expired
  access one, both as the header and duplicated inside the body)
  -> same {"tokens": {"accessToken": {...}, "refreshToken": {...}}} shape as
  login — refreshToken rotates on every use, same as Fozzy's connect/token.
(The real app also calls `MigrateSession` with the same shape immediately
before this and gets back `errorCode 3 CANCELLED` — harmless, RefreshToken
alone is what actually works; not replicated here.)

Single JSON-RPC-style gateway: every call POSTs to the same URL with a "Method"
name and "Data" body. Two receipt sources exist, each with its own list + detail
call (detail is where line items live — the list calls only carry totals):
  - GetChequesV2          + GetChequeDetail({"id": chequeId})       offline (in-store)
  - GetOnlineOrderHistory + GetOnlineOrderDetail({"id": orderId})   online (delivery/pickup)

Detail item shape differs slightly between the two (`lagerCountString` only, no
numeric qty, on offline; a real `count` float on online) but both give `priceOut`
(the actual per-unit price paid, discount already applied) and `oldPrice`
(pre-discount per-unit price, null if there wasn't one) — that's enough to derive
absolute per-line discount without needing to parse the `discount` percent field.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from app.core.time import store_local_to_utc

import httpx

from app.adapters.base import (
    AdapterError,
    AuthResult,
    BaseAdapter,
    CatalogProduct,
    LoyaltyBalance,
    LoyaltyCode,
    NormalizedItem,
    NormalizedReceipt,
)

RPC_URL = "https://api2.mob.fora.ua/api/2.0/exec/FZGlobal/"

# The gateway rejects requests missing these (errorCode 232 DEVICE_INFO_NOT_FOUND) —
# it doesn't validate that they match a real device, just that they're present.
_USER_INFO = (
    '{"brand":"POCO","model":"2207117BPG","sdkVersion":"33","sdkRelease":"13",'
    '"appVersionCode":"306","appVersionName":"3.1.0","androidVersion":33,"root":false,'
    '"geolocationTrackingCode":-1,"pushNotificationTrackingCode":"0"}'
)

_QTY_RE = re.compile(r"^([\d]+(?:[.,]\d+)?)")


def _base_headers() -> dict:
    return {
        "deviceTime": str(int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)),
        "user-info": _USER_INFO,
        "Accept": "application/json",
        "Accept-Charset": "UTF-8",
        "User-Agent": "ktor-client",
        "Content-Type": "application/json",
    }


class ForaAdapter(BaseAdapter):
    store_code = "fora"

    async def start_login(self, phone_number: str) -> AuthResult:
        guid = str(uuid.uuid4())
        async with httpx.AsyncClient(timeout=20) as client:
            result = await _rpc(
                client,
                _base_headers(),
                "SendOtpV2",
                {"phone": phone_number, "guid": guid, "captcha": ""},
                unwrap=False,
            )
        if not result.get("otpSend"):
            raise AdapterError("Фора: не вдалось надіслати SMS")
        return AuthResult(pending=True, extra={"guid": guid, "phone_number": phone_number})

    async def confirm_login(self, extra: dict, code: str) -> AuthResult:
        guid = extra.get("guid")
        phone_number = extra.get("phone_number")
        if not (guid and phone_number):
            raise AdapterError("Фора: сесія логіну застаріла — почніть спочатку")

        async with httpx.AsyncClient(timeout=20) as client:
            result = await _rpc(
                client,
                _base_headers(),
                "ConfirmationOtp_V2",
                {"phone": phone_number, "guid": guid, "otpCode": code},
                unwrap=False,
            )
        tokens = result.get("tokens") or {}
        access = tokens.get("accessToken") or {}
        refresh = tokens.get("refreshToken") or {}
        if not access.get("value"):
            raise AdapterError("Фора: невірний код підтвердження")

        return AuthResult(
            access_token=access["value"],
            refresh_token=refresh.get("value"),
            expires_at=dt.datetime.fromtimestamp(access["expireat"], tz=dt.timezone.utc)
            if access.get("expireat")
            else None,
        )

    async def refresh(self, credential_extra: dict, refresh_token: str | None) -> AuthResult:
        if not refresh_token:
            raise AdapterError("Фора: немає refresh_token, потрібен повторний вхід")

        # Authorization carries the REFRESH token here, not an access token —
        # confirmed against the real app (see module docstring).
        headers = {**_base_headers(), "Authorization": f"Token {refresh_token}"}
        async with httpx.AsyncClient(timeout=20) as client:
            result = await _rpc(
                client,
                headers,
                "RefreshToken",
                {"headerInfo": {"token": refresh_token, "deviceInfo": json.loads(_USER_INFO)}},
                unwrap=False,
            )
        tokens = result.get("tokens") or {}
        access = tokens.get("accessToken") or {}
        new_refresh = tokens.get("refreshToken") or {}
        if not access.get("value"):
            raise AdapterError("Фора: не вдалось оновити токен")

        return AuthResult(
            access_token=access["value"],
            refresh_token=new_refresh.get("value"),
            expires_at=dt.datetime.fromtimestamp(access["expireat"], tz=dt.timezone.utc)
            if access.get("expireat")
            else None,
        )

    async def get_loyalty_code(self, access_token: str, extra: dict) -> LoyaltyCode:
        """CheckUser — the same priming call fetch_receipts already makes and
        previously only used for its side effect — returns the card barcode too
        (confirmed live: matches what the real app's card screen shows exactly,
        including that it works offline once fetched, i.e. it's a static value, not
        a rotating one like АТБ's). GetQRKeys (EC public keys) looked like the real
        source at first but turned out to be for verifying an on-device-signed
        payment QR, unrelated to the loyalty card display."""
        headers = {**_base_headers(), "Authorization": f"Token {access_token}"}
        async with httpx.AsyncClient(timeout=20) as client:
            result = await _rpc(client, headers, "CheckUser", {})
        barcode = result.get("barcode")
        if not barcode:
            raise AdapterError("Фора: код картки відсутній у відповіді")
        return LoyaltyCode(value=barcode, display="qr")

    async def get_balance(self, access_token: str, extra: dict) -> LoyaltyBalance:
        """GetMenuInfo — the same call the dashboard/menu screen of the app itself
        uses — has cashBackAmount as the current wallet total (separate from the
        per-cheque cashbackAmount used for bonuses-earned-per-receipt)."""
        headers = {**_base_headers(), "Authorization": f"Token {access_token}"}
        async with httpx.AsyncClient(timeout=20) as client:
            result = await _rpc(client, headers, "GetMenuInfo", {})
        if "cashBackAmount" not in result:
            raise AdapterError("Фора: баланс відсутній у відповіді")
        return LoyaltyBalance(total=float(result["cashBackAmount"]))

    async def search_catalog(self, access_token: str | None, extra: dict, query: str) -> list[CatalogProduct]:
        """SearchCatalogItems on the same RPC gateway as everything else — unlike
        ATB's catalog search this one needs the account's own token (confirmed
        live: same gateway, same DEVICE_INFO_NOT_FOUND-if-missing headers as
        receipts). `sortOrderId`/empty filter fields match exactly what the real
        app sends for a plain text search; no evidence of a canonical product
        page URL in the response (unlike ATB), so product_url stays unset rather
        than guessing a URL pattern."""
        if not access_token:
            raise AdapterError("Фора: пошук по каталогу потребує токена")
        headers = {**_base_headers(), "Authorization": f"Token {access_token}"}
        async with httpx.AsyncClient(timeout=20) as client:
            result = await _rpc(
                client,
                headers,
                "SearchCatalogItems",
                {
                    "searchText": query,
                    "from": 1,
                    "to": 20,
                    "sortOrderId": "popular-asc",
                    "universalFilters": [],
                    "promoFilters": [],
                    "multiFilters": {},
                    "rangeFilters": {},
                },
            )
        products: list[CatalogProduct] = []
        for item in result.get("items") or []:
            try:
                price = float(item["price"])
            except (KeyError, TypeError, ValueError):
                continue
            products.append(
                CatalogProduct(
                    name=item.get("productTitle") or "",
                    price=price,
                    unit=item.get("unit"),
                    brand=item.get("brand") or None,
                    image_url=item.get("imageUrl") or None,
                    in_stock=(item.get("calcStoreQuantity") or 0) > 0,
                )
            )
        return products

    async def fetch_receipts(
        self, access_token: str, extra: dict, since: dt.datetime | None = None
    ) -> list[NormalizedReceipt]:
        headers = {**_base_headers(), "Authorization": f"Token {access_token}"}
        receipts: list[NormalizedReceipt] = []

        async with httpx.AsyncClient(timeout=20) as client:
            # GetOnlineOrderHistory fails with errorCode 512 ("user data unavailable")
            # on a freshly-issued token unless some other call primes the session
            # first — CheckUser is what the real app happens to call right after
            # login, and it's enough (confirmed live). Cheap, so just always do it.
            await _rpc(client, headers, "CheckUser", {})

            offline = await _rpc(client, headers, "GetChequesV2", {})
            for cheque in (offline.get("cheques") or []):
                purchased_at = store_local_to_utc(dt.datetime.fromisoformat(cheque["createDate"]))
                if since and purchased_at < since:
                    continue
                detail = await _rpc(client, headers, "GetChequeDetail", {"id": cheque["id"]})
                receipts.append(_normalize_offline(cheque, detail, purchased_at))

            online = await _rpc(client, headers, "GetOnlineOrderHistory", {"from": 0, "take": 50})
            for order in (online.get("orders") or []):
                purchased_at = store_local_to_utc(dt.datetime.fromisoformat(order["dateFrom"]))
                if since and purchased_at < since:
                    continue
                detail = await _rpc(client, headers, "GetOnlineOrderDetail", {"id": order["id"]})
                receipts.append(_normalize_online(order, detail, purchased_at))

        return receipts


async def _rpc(
    client: httpx.AsyncClient, headers: dict, method: str, data: dict, unwrap: bool = True
) -> dict:
    """unwrap=True returns payload["result"] (data-fetching calls); some RPC methods
    (SendOtpV2/ConfirmationOtp_V2) put their fields at the top level instead — pass
    unwrap=False for those."""
    resp = await client.post(RPC_URL, headers=headers, json={"Method": method, "Data": data})
    resp.raise_for_status()
    payload = resp.json()
    error = payload.get("error") or {}
    if error.get("errorCode"):
        raise AdapterError(f"Фора: {method} — {error.get('errorString') or error}")
    return (payload.get("result") or {}) if unwrap else payload


def _parse_qty(count_string: str | None) -> float:
    """'1 шт' -> 1.0, '0,536 кг' -> 0.536. Falls back to 1 if unparseable."""
    if not count_string:
        return 1.0
    m = _QTY_RE.match(count_string.strip())
    return float(m.group(1).replace(",", ".")) if m else 1.0


def _item_from_detail(name: str, qty: float, price_out, old_price, image_url: str | None) -> NormalizedItem:
    price_out = float(price_out or 0)
    discount = 0.0
    if old_price is not None and float(old_price) > price_out:
        discount = round((float(old_price) - price_out) * qty, 2)
    return NormalizedItem(
        name=name.strip(),
        quantity=qty,
        unit_price=price_out,
        total_price=round(price_out * qty, 2),
        discount=discount,
        product_image_url=image_url,
    )


def _normalize_offline(cheque: dict, detail: dict, purchased_at: dt.datetime) -> NormalizedReceipt:
    items = [
        _item_from_detail(
            name=it.get("lagerName", ""),
            qty=_parse_qty(it.get("lagerCountString")),
            price_out=it.get("priceOut"),
            old_price=it.get("oldPrice"),
            image_url=it.get("lagerImageUrl"),
        )
        for it in (detail.get("items") or [])
    ]
    return NormalizedReceipt(
        external_id=f"cheque:{cheque['id']}",
        purchased_at=purchased_at,
        total_amount=float(cheque.get("amount") or 0),
        shop_name=cheque.get("filialName"),
        payment_type="offline",
        channel="offline",
        items=items,
        bonuses=cheque.get("cashbackAmount"),
        raw={"list": cheque, "detail": detail},
    )


def _normalize_online(order: dict, detail: dict, purchased_at: dt.datetime) -> NormalizedReceipt:
    items = [
        _item_from_detail(
            name=it.get("name", ""),
            qty=float(it.get("count") or _parse_qty(it.get("lagerCountString"))),
            price_out=it.get("priceOut"),
            old_price=it.get("oldPrice"),
            image_url=it.get("lagerImageUrl"),
        )
        for it in (detail.get("items") or [])
    ]
    return NormalizedReceipt(
        external_id=f"online:{order['id']}",
        purchased_at=purchased_at,
        total_amount=float(order.get("amount") or 0),
        shop_address=order.get("address"),
        payment_type="online delivery",
        channel="online",
        items=items,
        bonuses=order.get("cashbackAmount"),
        raw={"list": order, "detail": detail},
    )
