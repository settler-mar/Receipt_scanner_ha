"""UPG fuel-station mobile API adapter.

The contract was recovered from UPG Android 2.3.17 and verified against the
real application flow.  The API returns an opaque user token; authenticated
requests use the app's ``Basic base64(DeviceId:Token)`` credential (there is no
refresh-token endpoint).
"""
from __future__ import annotations

import datetime as dt
import base64
import uuid

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

BASE_URL = "https://newmobi.upg.ua/v1"
_CREDENTIAL_LIFETIME = dt.timedelta(days=30)


def _normalize_phone(value: str) -> str:
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) == 10 and digits.startswith("0"):
        digits = "38" + digits
    if len(digits) != 12 or not digits.startswith("380"):
        raise AdapterError("UPG: введіть номер у форматі +380XXXXXXXXX")
    return "+" + digits


def _headers(access_token: str | None = None) -> dict[str, str]:
    headers = {
        "accept-charset": "application/json",
        "accept-language": "uk",
        "connection": "close",
        # The app configures Dio this way for both POST and GET.  In particular,
        # auth endpoints reject an equivalent JSON body with Validation Error.
        "content-type": "application/x-www-form-urlencoded;charset=utf-8",
    }
    if access_token:
        # confirm_login has already converted the server's UUID into the exact
        # "Basic base64(DeviceId:Token)" credential created by the Android app.
        headers["authorization"] = access_token
    return headers


def _payload(response) -> dict | list:
    try:
        value = response.json()
    except Exception as exc:
        raise AdapterError(f"UPG: сервер повернув HTTP {response.status_code}") from exc
    if response.status_code >= 400:
        errors = value.get("errors") if isinstance(value, dict) else None
        message = errors.get("ErrorMessage") if isinstance(errors, dict) else None
        raise AdapterError(f"UPG: {message or f'HTTP {response.status_code}'}")
    if not isinstance(value, (dict, list)):
        raise AdapterError("UPG: сервер повернув неочікувану відповідь")
    return value


def _data(value):
    return value.get("data") if isinstance(value, dict) and "data" in value else value


def _float(value) -> float:
    if value in (None, ""):
        return 0.0
    return float(str(value).replace(" ", "").replace(",", "."))


def _basic_token(device_id: str, token: str) -> str:
    encoded = base64.b64encode(f"{device_id}:{token}".encode()).decode()
    return f"Basic {encoded}"


def _parse_dt(value: str) -> dt.datetime:
    value = value.strip()
    iso = value.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(iso)
    except ValueError:
        parsed = None
        for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
            try:
                parsed = dt.datetime.strptime(value, fmt)
                break
            except ValueError:
                pass
        if parsed is None:
            raise AdapterError(f"UPG: невідомий формат дати транзакції: {value!r}")
    return store_local_to_utc(parsed)


class UpgAdapter(BaseAdapter):
    store_code = "upg"

    async def start_login(self, phone_number: str) -> AuthResult:
        phone = _normalize_phone(phone_number)
        device_id = f"{uuid.uuid4()}-{phone}"
        async with AsyncClient(timeout=20, headers=_headers()) as client:
            status_response = await client.post(
                f"{BASE_URL}/device/auth-status", data={"PhoneNumber": phone}
            )
            status = _data(_payload(status_response))
            activation_response = await client.post(
                f"{BASE_URL}/device/activation-phone",
                data={"DeviceId": device_id, "PhoneNumber": phone},
            )
            activation = _data(_payload(activation_response))

        if not isinstance(activation, dict) or not activation.get("MessageId"):
            raise AdapterError("UPG: сервер не повернув ідентифікатор SMS")
        return AuthResult(
            pending=True,
            extra={
                "phone_number": phone,
                "device_id": device_id,
                "message_id": activation["MessageId"],
                "is_user_registered": status.get("IsUserRegistered") if isinstance(status, dict) else None,
            },
        )

    async def confirm_login(self, extra: dict, code: str) -> AuthResult:
        device_id = extra.get("device_id")
        message_id = extra.get("message_id")
        if not device_id or message_id is None:
            raise AdapterError("UPG: сесія входу застаріла — запросіть SMS повторно")

        async with AsyncClient(timeout=20, headers=_headers()) as client:
            response = await client.post(
                f"{BASE_URL}/device/activation-confirm",
                data={
                    "DeviceId": device_id,
                    "ActivationCode": str(code).strip(),
                    "MessageId": message_id,
                },
            )
            result = _data(_payload(response))

        token = result.get("Token") if isinstance(result, dict) else None
        if not token:
            raise AdapterError("UPG: невірний або прострочений код підтвердження")
        phone = extra.get("phone_number")
        if not phone:
            raise AdapterError("UPG: у сесії входу відсутній номер телефону")
        return AuthResult(
            access_token=_basic_token(device_id, token),
            expires_at=dt.datetime.now(dt.timezone.utc) + _CREDENTIAL_LIFETIME,
            extra={"phone_number": phone, "device_id": device_id},
        )

    async def fetch_receipts(
        self, access_token: str, extra: dict, since: dt.datetime | None = None
    ) -> list[NormalizedReceipt]:
        async with AsyncClient(timeout=30, headers=_headers(access_token)) as client:
            response = await client.get(f"{BASE_URL}/activity/view")
            payload = _payload(response)

        rows = _data(payload)
        if not isinstance(rows, list):
            raise AdapterError("UPG: історія транзакцій має невідомий формат")
        receipts = [_normalize(row) for row in rows if isinstance(row, dict)]
        if since:
            boundary = since if since.tzinfo else since.replace(tzinfo=dt.timezone.utc)
            receipts = [receipt for receipt in receipts if receipt.purchased_at >= boundary]
        return receipts

    async def get_balance(self, access_token: str, extra: dict) -> LoyaltyBalance:
        async with AsyncClient(timeout=20, headers=_headers(access_token)) as client:
            response = await client.get(f"{BASE_URL}/common/init")
            payload = _data(_payload(response))
        if not isinstance(payload, dict) or payload.get("BonusesByMoney") is None:
            raise AdapterError("UPG: баланс бонусів відсутній у відповіді")
        return LoyaltyBalance(total=_float(payload["BonusesByMoney"]))

    async def get_fuel_prices(self, access_token: str, extra: dict) -> list[FuelPrice]:
        """Return the current network price board exposed by the UPG app.

        ``priceRetail`` is the app model's retail/pump-price field.  Older API
        payloads may only contain ``Price``, so keep it as a compatibility
        fallback.  Neither field is an account-specific discount.
        """
        async with AsyncClient(timeout=20, headers=_headers(access_token)) as client:
            response = await client.get(
                f"{BASE_URL}/sync-shared-data/get-prices?maxVersion=0"
            )
            payload = _payload(response)

        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise AdapterError("UPG: список цін на пальне має невідомий формат")
        return [
            price
            for row in rows
            if isinstance(row, dict) and (price := _normalize_fuel_price(row)) is not None
        ]


def _normalize(row: dict) -> NormalizedReceipt:
    goods = row.get("Good") or []
    items = [_normalize_item(item) for item in goods if isinstance(item, dict)]
    return NormalizedReceipt(
        external_id=str(row.get("TransactionId") or ""),
        purchased_at=_parse_dt(str(row.get("TransactionTime") or "")),
        total_amount=_float(row.get("ReceiptSum")),
        shop_name=row.get("GasStationName") or row.get("TransactionTitle"),
        shop_address=row.get("GasStationAddress"),
        items=items,
        raw=row,
        total_discount=_float(row.get("TotalDiscount")),
        # AccruedBonus/WithdrawBonus are бонуси (hundredths of a hryvnia); the
        # balance endpoint exposes the money value separately as BonusesByMoney.
        bonuses=_float(row.get("AccruedBonus")) / 100 or None,
        bonuses_used=_float(row.get("WithdrawBonus")) / 100 or None,
    )


def _normalize_fuel_price(row: dict) -> FuelPrice | None:
    name = str(row.get("Title") or "").strip()
    raw_price = row.get("priceRetail")
    if raw_price in (None, ""):
        raw_price = row.get("Price")
    if not name or raw_price in (None, ""):
        return None
    try:
        price = _float(raw_price)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    code = row.get("id")
    return FuelPrice(
        name=name,
        price=price,
        discount=None,
        code=str(code) if code is not None else None,
    )


def _normalize_item(item: dict) -> NormalizedItem:
    quantity = _float(item.get("GoodAmount")) or 1.0
    unit_price = _float(item.get("GoodPrice"))
    discount = _float(item.get("GoodDiscount"))
    total = item.get("GoodSum")
    total_price = _float(total) if total is not None else max(0.0, quantity * unit_price - discount)
    return NormalizedItem(
        name=str(item.get("GoodName") or "").strip(),
        quantity=quantity,
        unit_price=unit_price,
        total_price=total_price,
        discount=discount,
    )
