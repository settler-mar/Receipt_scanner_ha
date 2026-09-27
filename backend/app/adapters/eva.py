"""ЄВА (com.rush.eva) — мережа магазинів краси/догляду «EVA».

Reverse-engineered live via mitmproxy against app v5.6.2 (see RESEARCH.md / the
`apks/eva` capture notes). No certificate pinning; the store API is plain
okhttp over `https://app.eva.ua/api/...`. (The app also bundles Cronet/QUIC, but
only for telemetry — the loyalty/receipts API stays on OkHttp.)

Two-layer auth:
  1. An **app-level** bearer, minted from a static client credential with no user
     involvement:
       POST /oauth/token   (Authorization: Basic <app:secret>,
                            form: username/password/grant_type=password)
       -> {"access_token": <HS256 JWT, sub=aOSnAndroid, ROLE_APP>, "expires_in": 1800}
     The JWT lives only 30 min, but since it needs no user creds we just re-mint a
     fresh one at the start of every operation (`_app_bearer`) instead of storing
     and refreshing it.
  2. The **user session** is carried in the request *body*, not the bearer:
       POST /api/otp/generate  {phone, deviceId, infoDevice}   -> sends SMS
       POST /api/otp/validate  {phone, otp, deviceId}          -> confirms code
     The validated `otp` (the 4-digit SMS code itself) then doubles as a durable
     session key. Combined with `phone` it re-derives everything else:
       POST /api/processing/search {phone, otp}  -> buyer + balances + `uid`
     `uid` (a server-issued 32-hex id) is what the offline-receipt calls want, so
     the adapter fetches it fresh each run from `search` rather than persisting it.
     Only the `otp` is stored (encrypted, as the access_token slot); phone comes
     from the Account. No refresh endpoint for the session — when `otp` stops
     working the account flips to needs_reauth and the user re-logins (same as
     epicentrk.py).

Receipts, two independent sources:
  - Offline (in-store, fiscal):
      POST /api/processing/transactions/page   {phone, uid, page}      list (totals)
      POST /api/processing/transactions/detail {phone, uid, otp, transactionId}  items
    Money is in KOPECKS (strings). Online purchases also show up here with
    storeName='Інтернет-магазин "Єва"' as a total-only row — skipped, since the
    online source below carries their line items.
  - Online (delivery/pickup orders):
      POST /api/im/customer/info {phone, otp, page}  -> order_info[] with items
    Money is in HRYVNIAS (floats) here, unlike the processing/* endpoints.

Bonuses: processing/search's balances[]/balanceGroups[] ("Основной баланс",
balanceTypeId 0 = main points; 1 point = 1 UAH).
"""
from __future__ import annotations

import datetime as dt
import logging
import secrets

import httpx

from app.core.time import store_local_to_utc
from app.adapters.base import (
    AdapterError,
    AuthResult,
    BaseAdapter,
    LoyaltyBalance,
    NormalizedItem,
    NormalizedReceipt,
    NormalizedReceiptComponent,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://app.eva.ua"

# Static per-app OAuth client credential — same role as Fora's user-info blob or
# epicentrk's _API_KEY: the gateway only checks it's a valid app build, not that
# it matches a specific device. Captured from the real app's /oauth/token call.
_OAUTH_BASIC = "Basic YXBwOmVZeDZiS0lpbjhWMVB2NlhtWGZCQQ=="
_OAUTH_FORM = {
    "username": "aOSnAndroid",
    "password": "mAiVSx23EBt9Dg63R8Ydz86b2x",
    "grant_type": "password",
    "client_id": "app",
}
_APP_VERSION = "a377_5.6.2"
_INFO_DEVICE = {"systemDevice": "aOS", "verOS": "Android 13", "modelDevice": "Xiaomi 2207117BPG"}

# storeName marker the offline transactions feed uses for online purchases — those
# rows carry no line items here, so they're skipped in favour of /api/im (below).
_ONLINE_STORE_MARKER = "Інтернет-магазин"


def _normalize_phone(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    return "+" + digits


def _base_headers(bearer: str) -> dict:
    return {
        "Authorization": f"Bearer {bearer}",
        "app-version": _APP_VERSION,
        "Content-Type": "application/json; charset=UTF-8",
        "User-Agent": "okhttp/5.3.2",
        "Accept-Encoding": "gzip",
    }


def _to_float(value, scale: float = 1.0) -> float:
    try:
        return float(str(value).replace(",", ".")) / scale
    except (TypeError, ValueError):
        return 0.0


async def _app_bearer(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        f"{BASE_URL}/oauth/token",
        headers={
            "Authorization": _OAUTH_BASIC,
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "okhttp/5.3.2",
        },
        data=_OAUTH_FORM,
    )
    if resp.status_code != 200:
        raise AdapterError(f"ЄВА: не вдалось отримати токен застосунку ({resp.status_code})")
    token = resp.json().get("access_token")
    if not token:
        raise AdapterError("ЄВА: відповідь без access_token")
    return token


async def _search(client: httpx.AsyncClient, bearer: str, phone: str, otp: str) -> dict:
    """POST /api/processing/search {phone, otp} — validates the session and returns
    buyer/balances/uid in one call."""
    resp = await client.post(
        f"{BASE_URL}/api/processing/search",
        headers=_base_headers(bearer),
        json={"phone": phone, "otp": otp},
    )
    if resp.status_code != 200:
        raise AdapterError(f"ЄВА: сесія недійсна ({resp.status_code}) — увійдіть повторно")
    data = resp.json()
    if data.get("result") != 1 or not data.get("uid"):
        raise AdapterError("ЄВА: сесія недійсна — увійдіть повторно")
    return data


class EvaAdapter(BaseAdapter):
    store_code = "eva"

    async def start_login(self, phone_number: str) -> AuthResult:
        phone = _normalize_phone(phone_number)
        device_id = secrets.token_hex(8)
        async with httpx.AsyncClient(timeout=20) as client:
            bearer = await _app_bearer(client)
            resp = await client.post(
                f"{BASE_URL}/api/otp/generate",
                headers=_base_headers(bearer),
                json={"phone": phone, "deviceId": device_id, "infoDevice": _INFO_DEVICE},
            )
        if resp.status_code != 200 or resp.json().get("result") != 1:
            raise AdapterError(f"ЄВА: не вдалось надіслати SMS ({resp.status_code}): {resp.text[:200]}")
        return AuthResult(pending=True, extra={"phone_number": phone, "device_id": device_id})

    async def confirm_login(self, extra: dict, code: str) -> AuthResult:
        phone = extra.get("phone_number")
        device_id = extra.get("device_id")
        if not (phone and device_id):
            raise AdapterError("ЄВА: сесія логіну застаріла — почніть спочатку")
        code = code.strip()
        async with httpx.AsyncClient(timeout=20) as client:
            bearer = await _app_bearer(client)
            resp = await client.post(
                f"{BASE_URL}/api/otp/validate",
                headers=_base_headers(bearer),
                json={"phone": phone, "otp": code, "deviceId": device_id},
            )
            if resp.status_code != 200 or resp.json().get("result") != 1:
                raise AdapterError("ЄВА: невірний код підтвердження")
            # Prove the validated code works as a session key (and surface a clear
            # error now rather than on the first collection run).
            await _search(client, bearer, phone, code)
        # The SMS code itself is the durable session secret -> encrypted slot.
        return AuthResult(access_token=code, extra={"phone_number": phone})

    async def fetch_receipts(
        self, access_token: str, extra: dict, since: dt.datetime | None = None
    ) -> list[NormalizedReceipt]:
        otp = access_token
        phone = extra.get("phone_number")
        if not (otp and phone):
            raise AdapterError("ЄВА: немає даних сесії — увійдіть повторно")

        receipts: list[NormalizedReceipt] = []
        async with httpx.AsyncClient(timeout=30) as client:
            bearer = await _app_bearer(client)
            uid = (await _search(client, bearer, phone, otp))["uid"]
            headers = _base_headers(bearer)

            # --- Offline (in-store) receipts: list -> per-receipt detail for items ---
            page = 0
            while True:
                resp = await client.post(
                    f"{BASE_URL}/api/processing/transactions/page",
                    headers=headers,
                    json={"phone": phone, "uid": uid, "page": page},
                )
                if resp.status_code != 200:
                    raise AdapterError(f"ЄВА: не вдалось отримати чеки ({resp.status_code})")
                block = (resp.json() or {}).get("transactions") or {}
                for entry in block.get("content") or []:
                    if _ONLINE_STORE_MARKER in (entry.get("storeName") or ""):
                        continue  # online purchase — items come from /api/im instead
                    purchased_at = store_local_to_utc(_parse_dt(entry.get("date")))
                    if since is not None and purchased_at is not None and purchased_at < since:
                        continue
                    receipts.append(await self._fetch_offline_detail(client, headers, phone, uid, otp, entry))
                if block.get("last", True) or not (block.get("content")):
                    break
                page += 1

            # --- Online (delivery/pickup) orders, with line items in one call ---
            page = 1
            while True:
                resp = await client.post(
                    f"{BASE_URL}/api/im/customer/info",
                    headers=headers,
                    json={"phone": phone, "otp": otp, "page": page},
                )
                if resp.status_code != 200:
                    logger.warning("ЄВА: онлайн-замовлення недоступні (%s), пропускаю", resp.status_code)
                    break
                customer = (resp.json() or {}).get("customer") or {}
                for order in customer.get("order_info") or []:
                    receipt = _normalize_online(order)
                    if since is not None and receipt.purchased_at is not None and receipt.purchased_at < since:
                        continue
                    receipts.append(receipt)
                if customer.get("is_page_last", True) or not (customer.get("order_info")):
                    break
                page += 1

        return receipts

    async def _fetch_offline_detail(
        self, client: httpx.AsyncClient, headers: dict, phone: str, uid: str, otp: str, entry: dict
    ) -> NormalizedReceipt:
        receipt = _normalize_offline(entry)
        try:
            resp = await client.post(
                f"{BASE_URL}/api/processing/transactions/detail",
                headers=headers,
                json={"phone": phone, "uid": uid, "otp": otp, "transactionId": entry.get("transactionId")},
            )
            if resp.status_code == 200:
                detail = (resp.json() or {}).get("transaction") or {}
                receipt.shop_address = (detail.get("address") or "").replace("\n", " ").strip() or None
                receipt.items = [_normalize_offline_item(it) for it in detail.get("items") or []]
        except httpx.HTTPError:
            logger.warning("ЄВА: не вдалось отримати позиції чека %s", entry.get("transactionId"), exc_info=True)
        return receipt

    async def get_balance(self, access_token: str, extra: dict) -> LoyaltyBalance:
        otp = access_token
        phone = extra.get("phone_number")
        if not (otp and phone):
            raise AdapterError("ЄВА: немає даних сесії — увійдіть повторно")
        async with httpx.AsyncClient(timeout=20) as client:
            bearer = await _app_bearer(client)
            data = await _search(client, bearer, phone, otp)
        return _parse_balance(data)


def _parse_dt(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            return dt.datetime.strptime(value.strip(), "%Y-%m-%d")
        except ValueError:
            return None


def _normalize_offline(entry: dict) -> NormalizedReceipt:
    return NormalizedReceipt(
        external_id=str(entry.get("transactionId")),
        purchased_at=store_local_to_utc(_parse_dt(entry.get("date"))),
        total_amount=_to_float(entry.get("amount"), 100),
        shop_name=" ".join((entry.get("storeName") or "").replace('"', " ").split()) or None,
        channel="offline",
        total_discount=_to_float(entry.get("amountDiscount"), 100) or None,
        bonuses=_to_float(entry.get("amountBonus")) or None,
        receipt_url=entry.get("transactionLink") or None,
        raw=entry,
    )


def _normalize_offline_item(item: dict) -> NormalizedItem:
    qty = _to_float(item.get("qty")) or 1
    return NormalizedItem(
        name=(item.get("productName") or "").strip(),
        total_price=_to_float(item.get("priceWithDiscount"), 100),
        quantity=qty,
        unit_price=(_to_float(item.get("price"), 100) / qty) if qty else None,
        barcode=str(item.get("SKU")) if item.get("SKU") else None,
        discount=_to_float(item.get("discount"), 100),
    )


def _normalize_online(order: dict) -> NormalizedReceipt:
    items = [
        NormalizedItem(
            name=(it.get("product_name") or "").strip(),
            total_price=_to_float(it.get("row_total")),
            quantity=_to_float(it.get("qty")) or 1,
            unit_price=_to_float(it.get("price")) or None,
            barcode=str(it.get("sku")) if it.get("sku") else None,
            product_url=it.get("product_url") or None,
            product_image_url=it.get("image_url") or None,
            product_image_source_url=it.get("image_url") or None,
            merchant_name=it.get("merchant_name") or None,
        )
        for it in order.get("items") or []
    ]
    shipping = _to_float(order.get("shipping_amount"))
    components = ([NormalizedReceiptComponent(
        kind="delivery", amount=shipping, code="shipping_amount", label="Доставка",
        source="eva.order", raw={"shipping_amount": order.get("shipping_amount")},
    )] if shipping else [])
    return NormalizedReceipt(
        external_id=str(order.get("order_id")),
        purchased_at=store_local_to_utc(_parse_dt(order.get("created_at"))),
        total_amount=_to_float(order.get("grand_total")),
        shop_name='Інтернет-магазин «Єва»',
        shop_address=", ".join(p for p in (order.get("city"), order.get("street")) if p) or None,
        channel="online",
        order_status=order.get("status") or None,
        items=items,
        components=components,
        raw=order,
    )


def _parse_balance(data: dict) -> LoyaltyBalance:
    balances = data.get("balances") or []
    total = sum(_to_float(b.get("balance")) for b in balances)
    pending = sum(_to_float(b.get("inactiveBalance")) for b in balances)
    return LoyaltyBalance(total=total, pending=pending or None)
