"""Укрнафта (com.ukrnafta.android).

Auth: plain phone + SMS OTP against a small set of REST services (no OIDC/PKCE
dance, no captcha, no Cloudflare — confirmed live via mitmproxy against the
patched APK, see apks/ukrnafta/):

  1. POST https://user-service.ukrnafta.com/auth/otp   {"phone": "380..."}
     -> 200, plain-text body "Sent" (not JSON) — sends the actual SMS
  2. POST https://user-service.ukrnafta.com/auth/sign-in   {"phone": "380...", "otp": "1234"}
     -> 200 {"token": "<JWT>", "holderId": <int>, "phone": "380..."}
        (wrong code: 401, plain-text body "Невірний OTP-код.")

`token`'s JWT payload carries `exp` — decoded (unverified, we already trust it
as our own Bearer credential) for an accurate expires_at instead of guessing a
lifetime like wog.py has to.

Every call sends a bunch of client-identity headers (deviceid, devicefingerprint,
packagesinstalled, ...) alongside the Bearer token — captured as-is from the
real app; unconfirmed whether the server actually validates any of them beyond
requiring them present (same caveat as atb.py's _AUTH_HEADERS).

Three separate hosts, same Bearer token works on all:
  - user-service.ukrnafta.com     — auth, profile (/users/{holderId})
  - payment-service.ukrnafta.com  — balance/discounts/transactions (receipts)
  - content-service.ukrnafta.com  — fuel price board (no auth needed, but sent anyway)

Receipts: payment-service's `/transactions` (list, page/limit + date range) +
`/transactions/details` (line items per transaction, `goods[]`). Only tested
against ~2 months of history in one page (limit=1000) — pagination beyond that
is implemented from the `pagesOf` field's apparent meaning (extra pages
available) but not confirmed against an account with >1000 transactions.

Fuel prices + personal discount: `/fuels?mode=1` (board prices, `fuelId`) joined
with `/wallets/discount-and-count` (`discounts[].code2` == `fuelId`, as a
string) — same shape as wog.py's getprice/personaloffers join.

Loyalty code: `/users/{holderId}` already includes a static per-account QR
string (`accounts[].cards[].qr`) — used here. There's also a dedicated
`/users/{holderId}/qr/offline` endpoint that returns a *batch* of rotating
single-use QR tokens (prefixed "QL0210zz...", vs the static one's "QL0210rr...")
— deliberately not used: consumption semantics (does fetching or scanning one
invalidate it, how many are safe to pre-fetch) aren't confirmed, and burning
through a real single-use code just to render a screen would be a real-world
side effect, not a harmless read.
"""
from __future__ import annotations

import base64
import datetime as dt
import json

from app.adapters.base import (
    AdapterError,
    AuthResult,
    BaseAdapter,
    FuelDiscountAllowance,
    FuelPrice,
    LoyaltyBalance,
    LoyaltyCode,
    NormalizedItem,
    NormalizedReceipt,
)
from app.core.http import AsyncClient
from app.core.time import store_local_to_utc

USER_BASE = "https://user-service.ukrnafta.com"
PAYMENT_BASE = "https://payment-service.ukrnafta.com"
CONTENT_BASE = "https://content-service.ukrnafta.com"

# Client-identity headers the real app sends on every call — captured as-is,
# not confirmed whether the server validates them beyond "present" (see module
# docstring). Not secrets, just fixed device/app metadata.
_CLIENT_HEADERS = {
    "user-agent": "ktor-client",
    "platform": "Android - 13 (API: 33)",
    "appversion": "2.0.13",
    "buildnumber": "123",
    "deviceid": "774eacab42beaf1c",
    "localdataroot": "/data/user/0/com.ukrnafta.android",
    "devicefingerprint": "POCO/rosemary_p_global/rosemary:13/TP1A.220624.014/V14.0.17.0.TFFMIXM:user/release-keys",
    "packagesinstalled": "com.google.android.apps.maps=true",
}

_EARLIEST = "2000-01-01T00:00:00"
_DATE_FMT = "%Y-%m-%dT%H:%M:%S"
_PAGE_SIZE = 1000


def _normalize_phone(phone_number: str) -> str:
    return "".join(ch for ch in phone_number if ch.isdigit())


def _decode_jwt_exp(token: str) -> dt.datetime | None:
    """Unverified decode of the JWT payload's `exp` claim — fine here since it's
    our own just-issued Bearer credential, not authenticating anything with it
    (same reasoning as fozzy.py's _decode_jwt_claims)."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        return dt.datetime.fromtimestamp(claims["exp"], tz=dt.timezone.utc)
    except (IndexError, ValueError, KeyError):
        return None


def _to_float(value) -> float:
    if value in (None, ""):
        return 0.0
    return float(value)


def _parse_dt(value: str) -> dt.datetime:
    # No timezone in "2026-08-06T20:14:11" — Kyiv local time (see app/core/time.py).
    return store_local_to_utc(dt.datetime.strptime(value, _DATE_FMT))


def _parse_reset_date(value) -> dt.datetime | None:
    """`schemeResetDate` — seen as a bare date or a naive datetime, Kyiv local."""
    if not value or not isinstance(value, str):
        return None
    for fmt in (_DATE_FMT, "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return store_local_to_utc(dt.datetime.strptime(value, fmt))
        except ValueError:
            continue
    return None


class UkrnaftaAdapter(BaseAdapter):
    store_code = "ukrnafta"

    async def start_login(self, phone_number: str) -> AuthResult:
        phone_number = _normalize_phone(phone_number)
        async with AsyncClient(timeout=20, headers=_CLIENT_HEADERS) as client:
            resp = await client.post(f"{USER_BASE}/auth/otp", json={"phone": phone_number})
            resp.raise_for_status()
        return AuthResult(pending=True, extra={"phone_number": phone_number})

    async def confirm_login(self, extra: dict, code: str) -> AuthResult:
        phone_number = extra.get("phone_number")
        if not phone_number:
            raise AdapterError("Укрнафта: сесія логіну застаріла — почніть спочатку")

        async with AsyncClient(timeout=20, headers=_CLIENT_HEADERS) as client:
            resp = await client.post(
                f"{USER_BASE}/auth/sign-in", json={"phone": phone_number, "otp": code}
            )
            if resp.status_code == 401:
                raise AdapterError(f"Укрнафта: {resp.text.strip() or 'невірний код підтвердження'}")
            resp.raise_for_status()
            payload = resp.json()

        token = payload.get("token")
        holder_id = payload.get("holderId")
        if not token or not holder_id:
            raise AdapterError("Укрнафта: токен або holderId відсутні у відповіді")

        return AuthResult(
            access_token=token,
            expires_at=_decode_jwt_exp(token),
            extra={"phone_number": phone_number, "holder_id": str(holder_id)},
        )

    async def _profile(self, client: AsyncClient, access_token: str, holder_id: str) -> dict:
        resp = await client.get(
            f"{USER_BASE}/users/{holder_id}",
            params={"lang": "uk", "cached": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_balance(self, access_token: str, extra: dict) -> LoyaltyBalance:
        holder_id = extra.get("holder_id")
        if not holder_id:
            raise AdapterError("Укрнафта: в extra відсутній holder_id — потрібен повторний вхід")
        async with AsyncClient(timeout=20, headers=_CLIENT_HEADERS) as client:
            profile = await self._profile(client, access_token, holder_id)

        accounts = profile.get("accounts") or []
        if not accounts:
            raise AdapterError("Укрнафта: баланс відсутній у відповіді")
        account = accounts[0]
        active = _to_float(account.get("activeBalance"))
        total = _to_float(account.get("totalBalance"))
        # totalBalance vs activeBalance: assumed earned-but-not-yet-active vs
        # spendable now, mirroring LoyaltyBalance.pending's Epicentr precedent —
        # not confirmed against a live account where the two actually differ.
        pending = (total - active) or None
        return LoyaltyBalance(total=active, pending=pending)

    async def get_loyalty_code(self, access_token: str, extra: dict) -> LoyaltyCode:
        holder_id = extra.get("holder_id")
        if not holder_id:
            raise AdapterError("Укрнафта: в extra відсутній holder_id — потрібен повторний вхід")
        async with AsyncClient(timeout=20, headers=_CLIENT_HEADERS) as client:
            profile = await self._profile(client, access_token, holder_id)

        accounts = profile.get("accounts") or []
        cards = (accounts[0].get("cards") or []) if accounts else []
        # API field is literally named "qr", but the real app renders this value as
        # a PDF417 (2D stacked) barcode under "Скануй на АЗК" — confirmed against a
        # live screenshot, not a QR square — so display is set accordingly despite
        # the misleading field name.
        code = cards[0].get("qr") if cards else None
        if not code:
            raise AdapterError("Укрнафта: код картки відсутній у відповіді")
        return LoyaltyCode(value=code, display="pdf417")

    async def get_fuel_prices(self, access_token: str, extra: dict) -> list[FuelPrice]:
        holder_id = extra.get("holder_id")
        if not holder_id:
            raise AdapterError("Укрнафта: в extra відсутній holder_id — потрібен повторний вхід")
        headers = {**_CLIENT_HEADERS, "Authorization": f"Bearer {access_token}"}

        async with AsyncClient(timeout=20, headers=_CLIENT_HEADERS) as client:
            profile = await self._profile(client, access_token, holder_id)
            accounts = profile.get("accounts") or []
            cards = (accounts[0].get("cards") or []) if accounts else []
            card_number = cards[0].get("cardNo") if cards else None

            prices_resp = await client.get(f"{CONTENT_BASE}/fuels", params={"mode": "1"}, headers=headers)
            prices_resp.raise_for_status()
            prices = prices_resp.json() or []

            discounts_by_code: dict[str, float] = {}
            if card_number:
                disc_resp = await client.get(
                    f"{PAYMENT_BASE}/wallets/discount-and-count",
                    params={"cardNumber": card_number, "holderId": holder_id, "cached": "true"},
                    headers=headers,
                )
                disc_resp.raise_for_status()
                for d in disc_resp.json().get("discounts") or []:
                    if d.get("code2") is not None:
                        discounts_by_code[str(d["code2"])] = _to_float(d.get("discountAmount"))

        return [
            FuelPrice(
                name=item["fuelName"],
                price=_to_float(item["fuelPrice"]),
                discount=discounts_by_code.get(str(item.get("fuelId"))),
                code=str(item.get("fuelId")) if item.get("fuelId") is not None else None,
            )
            for item in prices
        ]

    async def get_fuel_discount_allowance(self, access_token: str, extra: dict) -> FuelDiscountAllowance:
        """Ukrnafta's personal fuel discount is capped at a monthly volume that
        resets on `AccountRaw.schemeResetDate`. `/wallets/discount-and-count`
        returns a `counts[]` array whose `remainingAmount` (stored ×100, same as
        the app) is what's left of that allowance; we sum across schemes.

        NOTE: whether `remainingAmount` is litres or UAH isn't 100% confirmed
        from the APK alone — the app just divides by 100 and shows it. Assuming
        litres per the loyalty terms ("0,50 грн за 1 літр…"); flip `unit` to
        "грн" here if a live value proves otherwise.
        """
        holder_id = extra.get("holder_id")
        if not holder_id:
            raise AdapterError("Укрнафта: в extra відсутній holder_id — потрібен повторний вхід")
        headers = {**_CLIENT_HEADERS, "Authorization": f"Bearer {access_token}"}

        async with AsyncClient(timeout=20, headers=_CLIENT_HEADERS) as client:
            profile = await self._profile(client, access_token, holder_id)
            accounts = profile.get("accounts") or []
            account = accounts[0] if accounts else {}
            cards = account.get("cards") or []
            card_number = cards[0].get("cardNo") if cards else None
            if not card_number:
                raise AdapterError("Укрнафта: не знайдено номер картки для ліміту знижки")

            resp = await client.get(
                f"{PAYMENT_BASE}/wallets/discount-and-count",
                params={"cardNumber": card_number, "holderId": holder_id, "cached": "true"},
                headers=headers,
            )
            resp.raise_for_status()
            counts = resp.json().get("counts") or []

        remaining = sum(_to_float(c.get("remainingAmount")) for c in counts) / 100
        return FuelDiscountAllowance(
            remaining=remaining,
            unit="л",
            resets_at=_parse_reset_date(account.get("schemeResetDate")),
        )

    async def fetch_receipts(
        self, access_token: str, extra: dict, since: dt.datetime | None = None
    ) -> list[NormalizedReceipt]:
        holder_id = extra.get("holder_id")
        if not holder_id:
            raise AdapterError("Укрнафта: в extra відсутній holder_id — потрібен повторний вхід")

        headers = {**_CLIENT_HEADERS, "Authorization": f"Bearer {access_token}"}
        from_date = since.astimezone(dt.timezone.utc).strftime(_DATE_FMT) if since else _EARLIEST
        to_date = dt.datetime.now(dt.timezone.utc).strftime(_DATE_FMT)
        receipts: list[NormalizedReceipt] = []

        async with AsyncClient(timeout=20, headers=_CLIENT_HEADERS) as client:
            page = 0
            while True:
                resp = await client.get(
                    f"{PAYMENT_BASE}/transactions",
                    params={
                        "userId": holder_id,
                        "page": page,
                        "limit": _PAGE_SIZE,
                        "fromDate": from_date,
                        "toDate": to_date,
                    },
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
                if data.get("errorCode"):
                    raise AdapterError(f"Укрнафта: {data.get('errorDescription') or 'помилка транзакцій'}")

                transactions = data.get("transactions") or []
                if not transactions:
                    break

                for tx in transactions:
                    purchased_at = _parse_dt(tx["transactionDate"])
                    if since and purchased_at < since:
                        continue
                    goods = await self._fetch_goods(client, headers, holder_id, tx)
                    receipts.append(_normalize(tx, purchased_at, goods))

                if len(transactions) < _PAGE_SIZE:
                    break
                page += 1

        return receipts

    async def _fetch_goods(
        self, client: AsyncClient, headers: dict, holder_id: str, tx: dict
    ) -> list[dict]:
        if not tx.get("containsDetails"):
            return []
        resp = await client.get(
            f"{PAYMENT_BASE}/transactions/details",
            params={
                "userId": holder_id,
                "transactionId": tx["transactionId"],
                "operationType": tx.get("operationType", 1),
            },
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json().get("goods") or []


def _normalize(tx: dict, purchased_at: dt.datetime, goods: list[dict]) -> NormalizedReceipt:
    items = [
        NormalizedItem(
            name=g.get("name", "").strip(),
            quantity=_to_float(g.get("quantity")) or 1,
            # Quantity*price==amount for every fuel line seen (liters), but no
            # confirmed non-fuel example to tell a unit apart from a plain count —
            # left unset rather than guessed (see module docstring).
            unit_price=_to_float(g.get("price")),
            total_price=_to_float(g.get("amount")),
            discount=_to_float(g.get("discountAmount")),
            barcode=g.get("code2"),
        )
        for g in goods
    ]

    return NormalizedReceipt(
        external_id=str(tx["transactionId"]),
        purchased_at=purchased_at,
        total_amount=_to_float(tx.get("totalAmount")),
        shop_name=tx.get("terminalName"),
        shop_address=tx.get("terminalAddress"),
        payment_type=tx.get("payType"),
        items=items,
        raw=tx,
        total_discount=_to_float(tx.get("totalDiscount")) or None,
        bonuses=_to_float(tx.get("totalIssuance")) or None,
        bonuses_used=_to_float(tx.get("totalRedeem")) or None,
    )
