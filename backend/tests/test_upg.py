from __future__ import annotations

import datetime as dt
import sys
import types
import unittest

from app.adapters.base import AdapterError

# The lightweight host test environment does not install the production
# curl_cffi transport.  Normalization/header tests do not open HTTP sessions,
# so provide only the import surface they need.
try:
    import curl_cffi  # noqa: F401
except ModuleNotFoundError:
    http_stub = types.ModuleType("app.core.http")
    http_stub.AsyncClient = object
    sys.modules["app.core.http"] = http_stub

from app.adapters.upg import (
    _basic_token,
    _headers,
    _normalize,
    _normalize_fuel_price,
    _normalize_phone,
)


class UpgAdapterTests(unittest.TestCase):
    def test_phone_normalization(self):
        self.assertEqual(_normalize_phone("067 123-45-67"), "+380671234567")
        self.assertEqual(_normalize_phone("+380671234567"), "+380671234567")
        with self.assertRaises(AdapterError):
            _normalize_phone("123")

    def test_token_is_raw_authorization_header(self):
        token = _basic_token("device-id", "opaque-token")
        headers = _headers(token)
        self.assertEqual(headers["authorization"], token)
        self.assertTrue(token.startswith("Basic "))
        self.assertEqual(headers["content-type"], "application/x-www-form-urlencoded;charset=utf-8")

    def test_normalizes_transaction_and_goods(self):
        row = {
            "TransactionId": 42,
            "TransactionTime": "2026-08-15T12:58:00Z",
            "GasStationName": "АЗК №1",
            "GasStationAddress": "Київ",
            "ReceiptSum": "1228.66",
            "TotalDiscount": "10",
            "AccruedBonus": "2864",
            "WithdrawBonus": 0,
            "Good": [{
                "GoodName": " Газ ",
                "GoodAmount": "20",
                "GoodPrice": "61.933",
                "GoodDiscount": "10",
            }],
        }
        receipt = _normalize(row)
        self.assertEqual(receipt.external_id, "42")
        self.assertEqual(receipt.purchased_at, dt.datetime(2026, 8, 15, 12, 58, tzinfo=dt.timezone.utc))
        self.assertEqual(receipt.total_amount, 1228.66)
        self.assertEqual(receipt.total_discount, 10)
        # AccruedBonus comes in бонусах (hundredths of a hryvnia) — stored as money.
        self.assertEqual(receipt.bonuses, 28.64)
        self.assertEqual(receipt.items[0].name, "Газ")
        self.assertAlmostEqual(receipt.items[0].total_price, 1228.66)

    def test_normalizes_fuel_price(self):
        price = _normalize_fuel_price({
            "id": 9,
            "Title": "upg95",
            "Price": "79.90",
            "priceRetail": "80,90",
        })
        self.assertIsNotNone(price)
        self.assertEqual(price.name, "upg95")
        self.assertEqual(price.price, 80.90)
        self.assertEqual(price.code, "9")
        self.assertIsNone(price.discount)

    def test_fuel_price_falls_back_and_skips_invalid_rows(self):
        price = _normalize_fuel_price({"Title": "Газ", "Price": "42.90"})
        self.assertEqual(price.price, 42.90)
        self.assertIsNone(_normalize_fuel_price({"Title": "", "Price": "42.90"}))
        self.assertIsNone(_normalize_fuel_price({"Title": "Газ", "Price": "bad"}))


if __name__ == "__main__":
    unittest.main()
