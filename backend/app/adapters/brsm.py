"""БРСМ-Нафта / БРСМ Plus (ua.lemon.brsm), Android app 10.9.1.

The app exposes its current chain-wide fuel board publicly. Account login is
confirmed by an automated phone call rather than an SMS: create a request,
handle the call, then poll its status and exchange the request id for tokens.

Station-specific prices (``/Mobile/fuel_prices/{id}``) require an account, but
the app's own map starts from ``get_all_fuels``. We deliberately expose the
normal pump price only. ``walletPrice`` is a separate promotional wallet price,
not a verified personal per-litre discount, so mapping it to ``discount`` would
be misleading.
"""
from __future__ import annotations

import datetime as dt
import re
import uuid

from app.adapters.base import (
    AdapterError, AuthResult, BaseAdapter, FuelPrice, LoyaltyBalance, LoyaltyCode,
    NormalizedItem, NormalizedReceipt,
)
from app.core.http import AsyncClient
from app.core.time import store_local_to_utc


BASE_URL = "https://td4.brsm-nafta.com/api/v2"
HEADERS = {"Content-Type": "application/json"}
DEVICE_TYPE_ANDROID = 1


class BrsmAdapter(BaseAdapter):
    store_code = "brsm"
    receipts_supported = True

    async def start_login(self, phone_number: str) -> AuthResult:
        phone = _normalize_phone(phone_number)
        request_id = uuid.uuid4().hex
        payload = {"requestId": request_id, "phone": phone, "fcmToken": ""}
        async with AsyncClient(timeout=30, headers=HEADERS) as client:
            response = await client.post(
                f"{BASE_URL}/Mobile/call_auth_request",
                params={"deviceType": DEVICE_TYPE_ANDROID},
                json=payload,
            )
        data = _json_object(response, "запустити перевірочний дзвінок")
        if data.get("err_code") not in (None, 0):
            raise AdapterError(_server_error(data, "БРСМ: не вдалося запустити перевірочний дзвінок"))
        return AuthResult(pending=True, extra={"request_id": request_id})

    async def confirm_login(self, extra: dict, code: str) -> AuthResult:
        del code  # The common endpoint supplies this field; call auth has no OTP.
        request_id = str(extra.get("request_id") or "")
        if not request_id:
            raise AdapterError("БРСМ: сесія входу відсутня — запустіть дзвінок ще раз")

        async with AsyncClient(timeout=30, headers=HEADERS) as client:
            status_response = await client.post(
                f"{BASE_URL}/Mobile/call_auth_check_status",
                params={"deviceType": DEVICE_TYPE_ANDROID},
                json={"requestId": request_id},
            )
            status_data = _json_object(status_response, "перевірити статус дзвінка")
            status = status_data.get("payload")
            if status != 2:
                if status == 3:
                    raise AdapterError("БРСМ: дзвінок відхилено або перевірка не пройдена — спробуйте ще раз")
                raise AdapterError("БРСМ: дзвінок ще не підтверджено. Дайте відповідь і повторіть перевірку")

            token_response = await client.post(f"{BASE_URL}/Token/CallBackLogin", json={"requestId": request_id})
        tokens = _json_object(token_response, "завершити вхід")
        access_token = tokens.get("AccessToken")
        refresh_token = tokens.get("RefreshToken")
        if not isinstance(access_token, str) or not access_token:
            raise AdapterError("БРСМ: сервер підтвердив дзвінок, але не повернув токен доступу")
        return AuthResult(access_token=access_token, refresh_token=refresh_token)

    async def refresh(self, credential_extra: dict, refresh_token: str | None) -> AuthResult:
        del credential_extra
        if not refresh_token:
            raise AdapterError("БРСМ: немає refresh token — увійдіть повторно")
        async with AsyncClient(timeout=30, headers=HEADERS) as client:
            response = await client.post(f"{BASE_URL}/Token/RefreshTokenLogin", json={"refreshToken": refresh_token})
        tokens = _json_object(response, "оновити авторизацію")
        access_token = tokens.get("AccessToken")
        new_refresh_token = tokens.get("RefreshToken")
        if not isinstance(access_token, str) or not access_token:
            raise AdapterError("БРСМ: не вдалося оновити авторизацію — увійдіть повторно")
        return AuthResult(access_token=access_token, refresh_token=new_refresh_token or refresh_token)

    async def get_fuel_prices(self, access_token: str, extra: dict) -> list[FuelPrice]:
        del access_token, extra
        async with AsyncClient(timeout=20, headers=HEADERS) as client:
            response = await client.get(f"{BASE_URL}/Mobile/get_all_fuels")
        payload = _json_list(response, "отримати ціни на пальне")
        return _normalize_fuel_prices(payload)

    async def get_balance(self, access_token: str, extra: dict) -> LoyaltyBalance:
        del extra
        async with AsyncClient(timeout=20, headers={**HEADERS, "Authorization": f"Bearer {access_token}"}) as client:
            response = await client.get(f"{BASE_URL}/Mobile/get_points")
        payload = _json_object(response, "отримати баланс бонусів")
        value = _first_number(payload, "points", "bonus", "balance", "amount", "total")
        if value is None:
            raise AdapterError("БРСМ: баланс бонусів відсутній у відповіді")
        return LoyaltyBalance(total=value)

    async def get_loyalty_code(self, access_token: str, extra: dict) -> LoyaltyCode:
        del extra
        async with AsyncClient(timeout=20, headers={**HEADERS, "Authorization": f"Bearer {access_token}"}) as client:
            response = await client.post(f"{BASE_URL}/Mobile/GetUserProfile", json={})
        payload = _json_object(response, "отримати профіль")
        code = _find_card_code(payload)
        if not code:
            raise AdapterError("БРСМ: номер картки відсутній у профілі")
        return LoyaltyCode(value=code, display="barcode")

    async def fetch_receipts(
        self, access_token: str, extra: dict, since: dt.datetime | None = None
    ) -> list[NormalizedReceipt]:
        del extra
        start = (since or dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)).date().isoformat()
        end = dt.datetime.now(dt.timezone.utc).date().isoformat()
        async with AsyncClient(timeout=45, headers={**HEADERS, "Authorization": f"Bearer {access_token}"}) as client:
            response = await client.post(
                f"{BASE_URL}/Mobile/get_transactions_data", json={"start": start, "end": end}
            )
        payload = _json_payload(response, "отримати історію транзакцій")
        rows = payload
        if isinstance(payload, dict):
            rows = payload.get("data", payload.get("transactions", payload.get("items", [])))
            if isinstance(rows, dict):
                rows = _find_transaction_list(rows)
        if not isinstance(rows, list):
            raise AdapterError("БРСМ: історія транзакцій має невідомий формат")
        receipts: list[NormalizedReceipt] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                receipts.append(_normalize_transaction(row))
            except AdapterError as exc:
                # The endpoint may include summary/service rows alongside
                # transactions. Ignore only rows with no recognizable date;
                # malformed dated rows should still be visible to the user.
                if "не містить дати" in str(exc):
                    continue
                raise
        if rows and not receipts:
            sample_keys = ", ".join(str(key) for key in rows[0].keys()) if isinstance(rows[0], dict) else "необ'єкт"
            raise AdapterError(f"БРСМ: отримано {len(rows)} записів, але формат не розпізнано (поля: {sample_keys[:300]})")
        return receipts


def _normalize_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 10 and digits.startswith("0"):
        digits = "38" + digits
    if len(digits) != 12 or not digits.startswith("380"):
        raise AdapterError("БРСМ: введіть номер у форматі +380XXXXXXXXX")
    return "+" + digits


def _json_object(response, action: str) -> dict:
    try:
        payload = response.json()
    except Exception as exc:
        # Some BRSM validation failures are returned as plain text (rather than
        # the JSON envelope used by successful requests). Preserve that useful
        # diagnostic instead of hiding it behind "некоректна відповідь".
        raw = str(getattr(response, "text", "") or "").strip().replace("\n", " ")
        suffix = f": {raw[:240]}" if raw else ""
        raise AdapterError(
            f"БРСМ: сервер не зміг {action} (HTTP {response.status_code}, некоректна відповідь){suffix}"
        ) from exc
    if response.status_code >= 400:
        if isinstance(payload, dict):
            detail = payload.get("err_descr") or payload.get("message") or payload.get("error")
            if detail:
                raise AdapterError(f"БРСМ: {str(detail).strip()}")
        raise AdapterError(f"БРСМ: сервер не зміг {action} (HTTP {response.status_code})")
    if not isinstance(payload, dict):
        raise AdapterError(f"БРСМ: сервер не зміг {action} (HTTP {response.status_code})")
    return payload


def _json_list(response, action: str) -> list:
    try:
        payload = response.json()
    except Exception as exc:
        raise AdapterError(f"БРСМ: сервер не зміг {action} (некоректна відповідь)") from exc
    if response.status_code >= 400 or not isinstance(payload, list):
        raise AdapterError(f"БРСМ: сервер не зміг {action} (HTTP {response.status_code})")
    return payload


def _json_payload(response, action: str) -> object:
    try:
        payload = response.json()
    except Exception as exc:
        raise AdapterError(f"БРСМ: сервер не зміг {action} (некоректна відповідь)") from exc
    if response.status_code >= 400:
        if isinstance(payload, dict):
            detail = payload.get("err_descr") or payload.get("message") or payload.get("error")
            if detail:
                raise AdapterError(f"БРСМ: {str(detail).strip()}")
        raise AdapterError(f"БРСМ: сервер не зміг {action} (HTTP {response.status_code})")
    return payload


def _server_error(payload: dict, fallback: str) -> str:
    message = payload.get("message") or payload.get("error") or payload.get("err_message")
    return f"{fallback}: {message}" if message else fallback


def _first_string(payload: dict, *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = _first_string(value, *keys)
            if nested:
                return nested
    return None


def _find_card_code(value: object) -> str | None:
    """Profile cards are returned as card1/card11 (or card2/card21) pairs.
    The app uses the internal number, stripping its ``R_`` prefix, for the
    barcode shown to the cashier; this is not necessarily called barcode in
    the profile JSON.
    """
    if isinstance(value, dict):
        for key in ("internalNumber", "cardNumber", "card_number", "barcode", "loyaltyCard", "card11", "card21"):
            raw = value.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip().removeprefix("R_")
        for child in value.values():
            found = _find_card_code(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_card_code(child)
            if found:
                return found
    return None


def _normalize_transaction(row: dict) -> NormalizedReceipt:
    date_value = _find_date_value(row)
    if not date_value:
        raise AdapterError("БРСМ: транзакція не містить дати")
    purchased_at = _parse_date(str(date_value))
    raw_items = row.get("lines") or row.get("goods") or row.get("items") or row.get("positions") or []
    items = [_normalize_transaction_item(item) for item in raw_items if isinstance(item, dict)]
    total = _number(row, "totalSumma", "total", "amount", "sum", "totalAmount")
    if total is None:
        total = sum(item.total_price for item in items)
    external_id = str(row.get("transId") or row.get("id") or row.get("transactionId") or row.get("operationId") or f"brsm-{purchased_at.isoformat()}-{total}")
    return NormalizedReceipt(
        external_id=external_id, purchased_at=purchased_at, total_amount=total,
        shop_name=row.get("stationName") or row.get("ffsName") or "БРСМ-Нафта",
        shop_address=row.get("stationAddress") or row.get("address"), items=items, raw=row,
    )


def _normalize_transaction_item(row: dict) -> NormalizedItem:
    quantity = _number(row, "quantity", "count", "amount", "qty", "volume") or 1.0
    unit_price = _number(row, "unitPrice", "price", "cost", "pricePerUnit")
    total = _number(row, "lineSumma", "total", "sum", "amountTotal", "summa")
    if total is None:
        total = quantity * (unit_price or 0.0)
    if unit_price is None and quantity:
        unit_price = total / quantity
    return NormalizedItem(name=str(row.get("name") or row.get("fuelName") or row.get("productName") or row.get("lineName") or row.get("description") or "Позиція"), quantity=quantity, unit_price=unit_price, total_price=total)


def _number(row: dict, *keys: str) -> float | None:
    for key in keys:
        try:
            if row.get(key) not in (None, ""):
                return float(str(row[key]).replace(",", ".").replace(" ", ""))
        except (TypeError, ValueError):
            continue
    return None


def _first_value(row: dict, *keys: str) -> object:
    for key in keys:
        if row.get(key) not in (None, ""):
            return row[key]
    wanted = {key.lower() for key in keys}
    for key, value in row.items():
        normalized = str(key).lower().replace("-", "_")
        if value not in (None, "") and (normalized in wanted or ("date" in normalized and "time" in normalized)):
            return value
    return None


def _find_date_value(value: object) -> object:
    keys = ("date", "dat", "dateStr", "createdAt", "created_at", "transactionDate", "transaction_date", "transactionTime", "transaction_time", "dateTime", "operationDate", "operation_date", "purchaseDate", "purchase_time", "timestamp")
    if isinstance(value, dict):
        direct = _first_value(value, *keys)
        if direct is not None:
            return direct
        for child in value.values():
            found = _find_date_value(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_date_value(child)
            if found is not None:
                return found
    return None


def _find_transaction_list(value: dict) -> list:
    for child in value.values():
        if isinstance(child, list) and (not child or isinstance(child[0], dict)):
            return child
        if isinstance(child, dict):
            found = _find_transaction_list(child)
            if found:
                return found
    return []


def _parse_date(value: str) -> dt.datetime:
    value = value.strip().replace("Z", "+00:00")
    if value.isdigit():
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000
        return dt.datetime.fromtimestamp(stamp, tz=dt.timezone.utc)
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = dt.datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
        else:
            raise AdapterError(f"БРСМ: невідомий формат дати транзакції: {value!r}")
    return store_local_to_utc(parsed)


def _first_number(payload: dict, *keys: str) -> float | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, dict):
            nested = _first_number(value, *keys)
            if nested is not None:
                return nested
        try:
            if value is not None and not isinstance(value, bool):
                return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _normalize_fuel_prices(rows: list) -> list[FuelPrice]:
    prices: list[FuelPrice] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("isShowMap") is False:
            continue
        try:
            price = float(str(row.get("price") or "").replace(",", "."))
        except ValueError:
            continue
        name = str(row.get("name") or row.get("fullName") or "").strip()
        if not name or price <= 0:
            continue
        code = row.get("code") or row.get("extId") or row.get("id")
        prices.append(FuelPrice(name=name, price=price, code=str(code) if code is not None else None))
    return prices
