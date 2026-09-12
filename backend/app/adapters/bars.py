"""БАРС (АЗК Барс, com.goodpage.bars_app) — 30+ АЗС на газі/бензині.

Full account login is NOT reverse-engineered here — it's blocked by something
qualitatively different from every other store in this project:

Confirmed live (via mitmproxy against a patched, debug-resigned APK): the app
signs in through Firebase (its own backend mints a Firebase custom token
issuer=firebase-adminsdk-h4t4n@bars-gs.iam.gserviceaccount.com — presumably
after verifying a phone OTP server-side), then talks to Firestore
(projects/bars-gs) for account data. That much worked fine over our mitmproxy
CA. But the actual OTP-verification call to BARS' own backend never appeared
in the capture at all — over Wi-Fi through the patched app + our proxy, OTP
confirmation just spins forever; the *same* login completed instantly over
mobile data (no interception). That's the signature of TLS certificate
pinning implemented in the app's own code (e.g. OkHttp CertificatePinner)
for its own backend host, independent of the OS trust store — patching
network_security_config (what every other adapter's APK patch relies on)
has no effect on it, since pinning is checked in application code after the
OS-level TLS handshake already succeeded against our proxy's cert.

Actually defeating that would mean hooking the app's own code (e.g. Frida) to
strip the pinning check — a materially more invasive step than anything else
in this project (all other adapters only ever needed a network_security_config
trust-anchor edit, which apps are meant to support for debugging). Not done
here without deciding that's worth it first.

What IS confirmed, from a live read against the user's own Firestore document
(`projects/bars-gs/databases/(default)/documents/users/{uid}`, fetched with a
Bearer ID token captured from one real login) is that this document only has
`phoneNumber`, `octopus_id`, `notifications` — no balance/discount field at
all. Fuel prices + personal discount (which the real app does show) aren't
stored there either. Fuel prices have since been recovered from the Flutter
AOT: the app combines the public ``/api/fuels`` product dictionary with
``/api/pricesByStation``. The adapter exposes the highest current pump price
per grade because the common FuelPrice model has no station dimension.

login_with_card (AuthType.card_number): the one thing this store doesn't need
any of the above for — BARS' loyalty QR appears to just encode the card
number, same as most chains in this project encode their own card numbers
directly. Confirmed only by convention (RESEARCH.md's pattern across every
other store checked so far), NOT against a live scan of this store's physical
card — verify against a real receipt/QR before trusting it at checkout.
"""
from __future__ import annotations

import datetime as dt

from app.adapters.base import (
    AdapterError,
    AuthResult,
    BaseAdapter,
    FuelPrice,
    LoyaltyCode,
    NormalizedReceipt,
)
from app.core.http import AsyncClient


BASE_URL = "https://barsgroup.com.ua/api"


class BarsAdapter(BaseAdapter):
    store_code = "bars"
    receipts_supported = False  # confirmed live: the app itself has no purchase-history screen

    def login_with_card(self, card_number: str) -> AuthResult:
        card_number = card_number.strip()
        if not card_number.isdigit():
            from app.adapters.base import AdapterError

            raise AdapterError("БАРС: номер картки має складатись лише з цифр")
        # No real token — get_loyalty_code below needs only the card number,
        # stored in extra. access_token is a placeholder so the generic
        # Credential/Account plumbing (which expects one) has something to store.
        return AuthResult(access_token=f"card:{card_number}", extra={"card_number": card_number})

    async def get_loyalty_code(self, access_token: str, extra: dict) -> LoyaltyCode:
        card_number = extra.get("card_number")
        if not card_number:
            raise ValueError("БАРС: в extra відсутній card_number")
        return LoyaltyCode(value=card_number, display="barcode")

    async def get_fuel_prices(self, access_token: str, extra: dict) -> list[FuelPrice]:
        # Both endpoints are public in the Android app. pricesByStation contains
        # the actual pump board prices, while /fuels supplies human-readable
        # product names and a separate talon-sale price that must not replace it.
        async with AsyncClient(timeout=20) as client:
            fuels_response = await client.get(f"{BASE_URL}/fuels")
            prices_response = await client.get(f"{BASE_URL}/pricesByStation")
        fuels = _json_list(fuels_response, "довідник пального")
        station_prices = _json_list(prices_response, "ціни АЗК")
        return _normalize_fuel_prices(fuels, station_prices)

    async def fetch_receipts(
        self, access_token: str, extra: dict, since: dt.datetime | None = None
    ) -> list[NormalizedReceipt]:
        return []


def _json_list(response, label: str) -> list:
    try:
        payload = response.json()
    except Exception as exc:
        raise AdapterError(f"БАРС: сервер повернув некоректні {label}") from exc
    if response.status_code >= 400 or not isinstance(payload, list):
        raise AdapterError(f"БАРС: не вдалося отримати {label} (HTTP {response.status_code})")
    return payload


def _normalize_fuel_prices(fuels: list, station_prices: list) -> list[FuelPrice]:
    names = {
        str(row["id"]): str(row.get("name") or "").strip()
        for row in fuels
        if isinstance(row, dict) and row.get("id") is not None and row.get("name")
    }
    highest: dict[str, float] = {}
    for row in station_prices:
        if not isinstance(row, dict) or row.get("ProductID") is None:
            continue
        code = str(row["ProductID"])
        try:
            price = float(str(row.get("Price") or "").replace(",", "."))
        except ValueError:
            continue
        if price > 0:
            highest[code] = max(price, highest.get(code, 0.0))
    return [
        FuelPrice(name=names[code], price=highest[code], code=code)
        for code in names
        if code in highest
    ]
