from __future__ import annotations

import sys
import types
import unittest

try:
    import curl_cffi  # noqa: F401
except ModuleNotFoundError:
    http_stub = types.ModuleType("app.core.http")
    http_stub.AsyncClient = object
    sys.modules["app.core.http"] = http_stub

from app.adapters.base import AdapterError
from app.adapters.brsm import _normalize_fuel_prices, _normalize_phone


class BrsmAdapterTests(unittest.TestCase):
    def test_normalizes_phone(self):
        self.assertEqual(_normalize_phone("067 123-45-67"), "+380671234567")
        self.assertEqual(_normalize_phone("+380671234567"), "+380671234567")

    def test_rejects_invalid_phone(self):
        with self.assertRaises(AdapterError):
            _normalize_phone("12345")

    def test_normalizes_visible_positive_fuel_prices(self):
        rows = [
            {"id": 1, "name": "Газ", "price": 40.99, "walletPrice": 0, "code": "FUEL1", "isShowMap": True},
            {"id": 2, "name": "ДП", "price": "88,99", "walletPrice": 55, "code": "FUEL2", "isShowMap": True},
            {"id": 3, "name": "Приховане", "price": 50, "isShowMap": False},
            {"id": 4, "name": "Лише гаманець", "price": 0, "walletPrice": 45, "isShowMap": True},
        ]
        prices = _normalize_fuel_prices(rows)
        self.assertEqual([(p.name, p.price, p.discount, p.code) for p in prices], [
            ("Газ", 40.99, None, "FUEL1"),
            ("ДП", 88.99, None, "FUEL2"),
        ])


if __name__ == "__main__":
    unittest.main()
