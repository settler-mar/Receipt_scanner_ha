"""WOG (d2.android.apps.wog).

Auth: phone + SMS OTP against WOG's own "MobileBackEnd" gateway — every call
(including login) carries a static per-app Basic-auth credential on top of the
phone/token-based user auth:

  1. POST /authorization/{phone}/2
     Authorization: Basic UFJpZGVXT0c6ZHlDOGpNYlE=  ("PRideWOG:dyC8jMbQ")
     {"version":"1.0","appVersion":"2.6.11","appOS":"Android","systemVersion":"33"}
     -> {"error":0,"status":0}   (sends the actual SMS)
  2. POST /gettoken/{phone}
     {..., "otpPass": "123456"}
     -> {"token": "<opaque, NOT a JWT>", "info": {"numberCard": "...", "name", "surname", ...}}

`token` doesn't decode as a JWT and the response carries no expiry — treated as
long-lived (see CREDENTIAL_LIFETIME below); `refresh()` is deliberately
unsupported, same default as atb.py, so an expired token just means re-login.

Every subsequent call sends `token` in the JSON body (not an Authorization
header) plus the loyalty card number and phone in the URL path:
    POST /{endpoint}/{numberCard}/{phone}/...
    {"version":"1.0","appVersion":"...","appOS":"Android","systemVersion":"...","token":"..."}

No captcha, no Cloudflare bot-management observed on this host (confirmed live
via mitmproxy against the patched APK — see apks/wog/) — nothing here needed
the manual-import workaround atb.py needs.

Receipts: `gettransactions` (see _fetch_transactions) — paginated backwards by
`endDate`, page size 50, until an empty page. Each operation mixes fuel and
regular-goods lines in one `LISTGOODS[]`; `FUEL` distinguishes them.

Fuel prices + personal discount: `getprice` (board prices, keyed by
`GOODSCODE`) joined with `personaloffers` (this account's personal per-grade
discount, same `GOODSCODE`/`id`) — see get_fuel_prices. `getprice` needs no
purchase history, so a fresh account can show it immediately.
"""
from __future__ import annotations

import datetime as dt

from app.adapters.base import (
    AdapterError,
    AuthResult,
    BaseAdapter,
    FuelPrice,
    LoyaltyBalance,
    NormalizedItem,
    NormalizedReceipt,
)
from app.core.http import AsyncClient
from app.core.time import store_local_to_utc

BASE_URL = "https://thebestapp4ever.wog.ua/MobileBackend/hs/MobileBackEnd"
# Static per-app Basic auth credential the mobile client sends on every call,
# on top of the phone+OTP user auth — not a secret tied to any one account.
APP_BASIC_AUTH = "Basic UFJpZGVXT0c6ZHlDOGpNYlE="

_APP_INFO = {
    "version": "1.0",
    "appVersion": "2.6.11",
    "appOS": "Android",
    "systemVersion": "33",
}

# WOG's token carries no expiry in the response; refresh() is unsupported (like
# atb.py's default), so this only bounds how long we go before asking the user
# to re-login rather than an actual server-enforced TTL.
_CREDENTIAL_LIFETIME = dt.timedelta(days=30)

_PAGE_SIZE = 50


def _headers() -> dict:
    return {"Authorization": APP_BASIC_AUTH}


def _check(payload: dict) -> dict:
    """WOG signals failure (expired/invalid token, auth errors, ...) with HTTP 200
    and a non-zero `error` in the body — e.g. {"status":1,"error":10,"message":
    {"ua":"Помилка авторизації.",...}} — never a 4xx/5xx status, so
    resp.raise_for_status() alone doesn't catch it. Check this on every call."""
    if payload.get("error"):
        message = payload.get("message")
        if isinstance(message, dict):
            message = message.get("ua") or message.get("en") or message
        raise AdapterError(f"WOG: {message or 'невідома помилка'} (error={payload['error']})")
    return payload


def _normalize_phone(phone_number: str) -> str:
    """WOG's API wants a bare 12-digit number ("380671234567"), no leading '+' —
    confirmed against real app traffic (see module docstring). Account.phone_number
    is a generic UI field shared by every store and typically entered as
    "+380...", so strip formatting here rather than relying on how it was typed."""
    return "".join(ch for ch in phone_number if ch.isdigit())


def _to_float(value) -> float:
    if value in (None, ""):
        return 0.0
    return float(str(value).replace(",", "."))


def _parse_dt(value: str) -> dt.datetime:
    # "dd.MM.yyyy HH:mm:ss", no timezone — Kyiv local time (see app/core/time.py).
    return store_local_to_utc(dt.datetime.strptime(value, "%d.%m.%Y %H:%M:%S"))


class WogAdapter(BaseAdapter):
    store_code = "wog"

    async def start_login(self, phone_number: str) -> AuthResult:
        phone_number = _normalize_phone(phone_number)
        async with AsyncClient(timeout=20, headers=_headers()) as client:
            resp = await client.post(f"{BASE_URL}/authorization/{phone_number}/2", json=_APP_INFO)
            resp.raise_for_status()
            payload = _check(resp.json())
        if payload.get("status"):
            raise AdapterError(f"WOG: не вдалось надіслати SMS ({payload})")
        return AuthResult(pending=True, extra={"phone_number": phone_number})

    async def confirm_login(self, extra: dict, code: str) -> AuthResult:
        phone_number = extra.get("phone_number")
        if not phone_number:
            raise AdapterError("WOG: сесія логіну застаріла — почніть спочатку")
        phone_number = _normalize_phone(phone_number)

        async with AsyncClient(timeout=20, headers=_headers()) as client:
            resp = await client.post(
                f"{BASE_URL}/gettoken/{phone_number}",
                json={**_APP_INFO, "otpPass": code},
            )
            resp.raise_for_status()
            payload = _check(resp.json())

        if payload.get("status") or not payload.get("token"):
            raise AdapterError("WOG: невірний код підтвердження")

        card_number = (payload.get("info") or {}).get("numberCard")
        if not card_number:
            raise AdapterError("WOG: у відповіді відсутній номер картки")

        expires_at = dt.datetime.now(dt.timezone.utc) + _CREDENTIAL_LIFETIME
        return AuthResult(
            access_token=payload["token"],
            expires_at=expires_at,
            extra={"phone_number": phone_number, "card_number": card_number},
        )

    async def fetch_receipts(
        self, access_token: str, extra: dict, since: dt.datetime | None = None
    ) -> list[NormalizedReceipt]:
        card_number, phone_number = _require_ids(extra)
        receipts: list[NormalizedReceipt] = []
        end_date = dt.datetime.now(dt.timezone.utc).strftime("%d.%m.%Y %H:%M:%S")

        async with AsyncClient(timeout=20, headers=_headers()) as client:
            while True:
                resp = await client.post(
                    f"{BASE_URL}/gettransactions/{card_number}/{phone_number}/",
                    json={**_APP_INFO, "token": access_token, "endDate": end_date, "fuel": 3, "bonuses": 1},
                )
                resp.raise_for_status()
                operations = _check(resp.json()).get("OPERATIONS") or []
                if not operations:
                    break

                stop = False
                for op in operations:
                    normalized = _normalize(op)
                    if since and normalized.purchased_at < since:
                        stop = True
                        break
                    receipts.append(normalized)

                if stop or len(operations) < _PAGE_SIZE:
                    break
                end_date = operations[-1]["DATE"]

        return receipts

    async def get_balance(self, access_token: str, extra: dict) -> LoyaltyBalance:
        card_number, phone_number = _require_ids(extra)
        async with AsyncClient(timeout=20, headers=_headers()) as client:
            resp = await client.post(
                f"{BASE_URL}/clientinfo/{card_number}/{phone_number}",
                json={**_APP_INFO, "token": access_token, "qr_tokens_count": 0, "qr_last_number": 0},
            )
            resp.raise_for_status()
            payload = _check(resp.json())
        if "Bonuses" not in payload:
            raise AdapterError("WOG: баланс бонусів відсутній у відповіді")
        # `Bonuses` is in points, not UAH — confirmed against the real app screen
        # (15704 points shown there as 157,04 грн) and consistent with
        # clientinfo's own Payments entry ("Продаж за бонуси", MaxSum 500 грн —
        # 15704 points / 100 = 157.04, well under that cap; 50000 points would be
        # the actual cap). 100 points == 1 UAH.
        return LoyaltyBalance(total=_to_float(payload["Bonuses"]) / 100)

    async def get_fuel_prices(self, access_token: str, extra: dict) -> list[FuelPrice]:
        card_number, phone_number = _require_ids(extra)
        async with AsyncClient(timeout=20, headers=_headers()) as client:
            price_resp = await client.post(
                f"{BASE_URL}/getprice/{card_number}/{phone_number}/",
                json={**_APP_INFO, "token": access_token},
            )
            price_resp.raise_for_status()
            price_payload = _check(price_resp.json())
            offers_resp = await client.post(
                f"{BASE_URL}/personaloffers/{card_number}/{phone_number}",
                json={**_APP_INFO, "token": access_token, "type": 5},
            )
            offers_resp.raise_for_status()
            offers_payload = _check(offers_resp.json())

        prices = price_payload.get("Info") or []
        # discount is per-liter, in kopecks (e.g. 500 == 5.00 UAH/L) — same unit
        # UAH prices are in, just x100; keyed by GOODSCODE == personaloffers' "id".
        offers = (
            (offers_payload.get("fuelOffer") or {}).get("BonusesOffer") or {}
        ).get("offers") or []
        discount_by_code = {o["id"]: o["discount"] / 100 for o in offers if o.get("id") is not None}

        return [
            FuelPrice(
                name=item["DESCRIPTION"],
                price=_to_float(item["PRICE"]),
                discount=discount_by_code.get(item.get("GOODSCODE")),
                code=item.get("GOODID"),
            )
            for item in prices
        ]


def _require_ids(extra: dict) -> tuple[str, str]:
    card_number = extra.get("card_number")
    phone_number = extra.get("phone_number")
    if not (card_number and phone_number):
        raise AdapterError("WOG: в extra відсутні card_number/phone_number — потрібен повторний вхід")
    return card_number, _normalize_phone(phone_number)


def _normalize(op: dict) -> NormalizedReceipt:
    goods = op.get("LISTGOODS") or []
    items = [_normalize_item(g) for g in goods]

    # BONUSES.INCREASE/DECREASE come in бали, i.e. hundredths of a hryvnia —
    # the same convention as the balance endpoint (get_balance divides by 100).
    # The rest of the app treats bonuses as money, so convert here.
    bonuses_earned = sum(_to_float((g.get("BONUSES") or {}).get("INCREASE")) for g in goods) / 100 or None
    bonuses_used = sum(_to_float((g.get("BONUSES") or {}).get("DECREASE")) for g in goods) / 100 or None
    total_amount = sum(_to_float(g.get("SUMM")) for g in goods)

    return NormalizedReceipt(
        external_id=op["ID"],
        purchased_at=_parse_dt(op["DATE"]),
        total_amount=total_amount,
        shop_name=op.get("AZS"),
        shop_address=op.get("ADDRESS"),
        # TYPE_TRANSACTION seen so far: "AZS" (in-person at a station) vs
        # "ANOTHER" (e.g. store.wog.ua web purchase) — best-effort mapping,
        # not confirmed against a large enough online-order sample.
        channel="offline" if op.get("TYPE_TRANSACTION") == "AZS" else "online",
        items=items,
        raw=op,
        bonuses=bonuses_earned,
        bonuses_used=bonuses_used,
    )


def _normalize_item(g: dict) -> NormalizedItem:
    quantity = _to_float(g.get("COUNT")) or 1
    return NormalizedItem(
        name=g.get("NAME", "").strip(),
        quantity=quantity,
        unit="л" if g.get("FUEL") == "1" else None,
        unit_price=_to_float(g.get("PRICE")),
        total_price=_to_float(g.get("SUMM")),
        discount=_to_float(g.get("DISCOUNT")),
        barcode=(g.get("GOODS") or "").strip() or None,
    )
