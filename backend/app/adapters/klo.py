"""KLO (ua.klo).

Auth: plain phone + SMS OTP against a clean REST API, no captcha, no
Cloudflare, no TLS pinning — confirmed live via mitmproxy against the patched
APK (see apks/klo/):

  1. POST https://prod-app.klo.link/api/v1/auth/requestOtp?platform=android&deviceId=...&version=426&locale=uk
     {"phoneNumber": "380..."}
     -> 201, empty body — sends the actual SMS
  2. POST https://prod-app.klo.link/api/v1/auth/login?...
     {"phoneNumber": "380...", "authCode": "123456"}
     -> 201 {"cardNumber": "...", "accessToken": "<JWT>"}

`accessToken`'s JWT payload carries `exp` ~180 days out — decoded (unverified,
it's our own just-issued Bearer credential) for an accurate expires_at.

Every call needs the same `deviceId` query param the login used (unconfirmed
whether the server actually ties the token to it, or just wants it present —
same caveat as every other adapter's device-identity headers); generated once
per adapter instance and reused for all calls, matching what the real app
does for one install.

Two calls cover almost everything:
  - GET /api/v2/profile/me — balance (`balance.card.amount`), personal fuel
    discount + current prices in one shot (`digitalFuelPrices.items[]`,
    already per-grade, no per-station dimension to average like bars.py has
    to — this is KLO's own single "digital" price/discount, the same one
    the app itself shows on its price board).
  - GET /api/v1/profile/tx (+ /api/v1/profile/tx/{id} for line items) —
    purchase history. `type: "ANNULLED_BONUS"` entries are bonus corrections,
    not purchases (address is literally "Системна транзакція", zero value) —
    skipped. No pagination param seen; the one live account tested had 6
    transactions total and got them all in one response — unconfirmed whether
    a larger account would need paging (see RESEARCH.md caveat pattern).

Loyalty code: `cardNumber` from login/profile — display format (QR vs
barcode) not confirmed against a physical card scan, defaulted to "barcode"
matching the majority convention across other stores in this project.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import uuid

from app.adapters.base import (
    AdapterError,
    AuthResult,
    BaseAdapter,
    FuelPrice,
    LoyaltyBalance,
    LoyaltyCode,
    NormalizedItem,
    NormalizedReceipt,
)
from app.core.http import AsyncClient
from app.core.time import store_local_to_utc

BASE_URL = "https://prod-app.klo.link/api"
APP_VERSION = "426"

# One per-process device id, reused for every call this adapter makes — the
# real app has exactly one per install; we don't need a fresh one per request.
_DEVICE_ID = f"android_{uuid.uuid4()}"

# Not a purchase — a system-generated bonus correction (address is literally
# "Системна транзакція", value always 0). Skipped in fetch_receipts.
_NON_PURCHASE_TYPES = {"ANNULLED_BONUS"}


def _normalize_phone(phone_number: str) -> str:
    return "".join(ch for ch in phone_number if ch.isdigit())


def _params(locale: str = "uk") -> dict:
    return {"platform": "android", "deviceId": _DEVICE_ID, "version": APP_VERSION, "locale": locale}


def _decode_jwt_exp(token: str) -> dt.datetime | None:
    """Unverified decode of the JWT payload's `exp` claim — fine here since
    it's our own just-issued Bearer credential (same reasoning as
    ukrnafta.py's _decode_jwt_exp)."""
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
    # e.g. "2026-01-22T13:43:05.000+01:00" — usually already carries an offset,
    # unlike most stores here; store_local_to_utc handles both cases.
    return store_local_to_utc(dt.datetime.fromisoformat(value))


class KloAdapter(BaseAdapter):
    store_code = "klo"

    async def start_login(self, phone_number: str) -> AuthResult:
        phone_number = _normalize_phone(phone_number)
        async with AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{BASE_URL}/v1/auth/requestOtp", params=_params(), json={"phoneNumber": phone_number}
            )
            resp.raise_for_status()
        return AuthResult(pending=True, extra={"phone_number": phone_number})

    async def confirm_login(self, extra: dict, code: str) -> AuthResult:
        phone_number = extra.get("phone_number")
        if not phone_number:
            raise AdapterError("KLO: сесія логіну застаріла — почніть спочатку")

        async with AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{BASE_URL}/v1/auth/login",
                params=_params(),
                json={"phoneNumber": phone_number, "authCode": code},
            )
            if resp.status_code == 401 or resp.status_code == 400:
                raise AdapterError("KLO: невірний код підтвердження")
            resp.raise_for_status()
            payload = resp.json()

        token = payload.get("accessToken")
        card_number = payload.get("cardNumber")
        if not token or not card_number:
            raise AdapterError("KLO: токен або номер картки відсутні у відповіді")

        return AuthResult(
            access_token=token,
            expires_at=_decode_jwt_exp(token),
            extra={"phone_number": phone_number, "card_number": card_number},
        )

    async def _profile(self, client: AsyncClient, access_token: str) -> dict:
        resp = await client.get(
            f"{BASE_URL}/v2/profile/me", params=_params(), headers={"Authorization": f"Bearer {access_token}"}
        )
        resp.raise_for_status()
        return resp.json()

    async def get_balance(self, access_token: str, extra: dict) -> LoyaltyBalance:
        async with AsyncClient(timeout=20) as client:
            profile = await self._profile(client, access_token)
        card = ((profile.get("balance") or {}).get("card")) or {}
        if "amount" not in card:
            raise AdapterError("KLO: баланс відсутній у відповіді")
        return LoyaltyBalance(total=_to_float(card["amount"]))

    async def get_loyalty_code(self, access_token: str, extra: dict) -> LoyaltyCode:
        card_number = extra.get("card_number")
        if not card_number:
            raise AdapterError("KLO: в extra відсутній card_number — потрібен повторний вхід")
        return LoyaltyCode(value=card_number, display="barcode")

    async def get_fuel_prices(self, access_token: str, extra: dict) -> list[FuelPrice]:
        async with AsyncClient(timeout=20) as client:
            profile = await self._profile(client, access_token)
        items = ((profile.get("digitalFuelPrices") or {}).get("items")) or []
        return [
            FuelPrice(
                name=item.get("title") or item.get("key") or "",
                price=_to_float(item.get("price")),
                discount=_to_float(item["discount"]) if item.get("discount") is not None else None,
                code=str(item.get("key") or item.get("id") or "") or None,
            )
            for item in items
        ]

    async def fetch_receipts(
        self, access_token: str, extra: dict, since: dt.datetime | None = None
    ) -> list[NormalizedReceipt]:
        headers = {"Authorization": f"Bearer {access_token}"}
        receipts: list[NormalizedReceipt] = []

        async with AsyncClient(timeout=20) as client:
            resp = await client.get(f"{BASE_URL}/v1/profile/tx", params=_params(), headers=headers)
            resp.raise_for_status()
            summaries = resp.json() or []

            for summary in summaries:
                if summary.get("type") in _NON_PURCHASE_TYPES:
                    continue
                purchased_at = _parse_dt(summary["when"])
                if since and purchased_at < since:
                    continue

                detail_resp = await client.get(
                    f"{BASE_URL}/v1/profile/tx/{summary['id']}", params=_params(), headers=headers
                )
                detail_resp.raise_for_status()
                receipts.append(_normalize(summary, detail_resp.json(), purchased_at))

        return receipts


def _normalize(summary: dict, detail: dict, purchased_at: dt.datetime) -> NormalizedReceipt:
    items = [
        NormalizedItem(
            name=g.get("name", "").strip(),
            quantity=_to_float(g.get("quantity")) or 1,
            unit="л" if summary.get("type") == "FUEL" else None,
            unit_price=_to_float(g.get("pricePerLitre")) or None,
            total_price=_to_float(g.get("amount")),
            discount=_to_float(g.get("discount")),
        )
        for g in (detail.get("basket") or [])
    ]

    return NormalizedReceipt(
        external_id=str(summary["id"]),
        purchased_at=purchased_at,
        total_amount=_to_float(detail.get("totalAmount") or summary.get("value")),
        shop_address=detail.get("placeName") or summary.get("address"),
        items=items,
        raw={"summary": summary, "detail": detail},
        total_discount=_to_float(detail.get("totalDiscount")) or None,
        bonuses=_to_float(detail.get("primaryBalanceEarned")) or None,
        bonuses_used=_to_float(detail.get("primaryBalanceBurned")) or None,
    )
