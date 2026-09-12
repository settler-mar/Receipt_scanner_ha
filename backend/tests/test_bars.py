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

from app.adapters.bars import _normalize_fuel_prices


class BarsAdapterTests(unittest.TestCase):
    def test_normalizes_highest_current_station_price_per_grade(self):
        fuels = [
            {"id": 1, "name": "Газ скраплений", "moneyPrice": 41000},
            {"id": 9, "name": "ДП EURO5", "moneyPrice": 90900},
        ]
        station_prices = [
            {"ProductID": 1, "StationID": 10, "Price": "40.34"},
            {"ProductID": 1, "StationID": 11, "Price": "41,84"},
            {"ProductID": 9, "StationID": 10, "Price": "89.90"},
        ]
        prices = _normalize_fuel_prices(fuels, station_prices)
        self.assertEqual([(p.name, p.price, p.code) for p in prices], [
            ("Газ скраплений", 41.84, "1"),
            ("ДП EURO5", 89.90, "9"),
        ])

    def test_skips_unknown_and_invalid_prices(self):
        fuels = [{"id": 1, "name": "Газ"}]
        rows = [
            {"ProductID": 1, "Price": "bad"},
            {"ProductID": 99, "Price": "50"},
        ]
        self.assertEqual(_normalize_fuel_prices(fuels, rows), [])


if __name__ == "__main__":
    unittest.main()
