from __future__ import annotations

import datetime as dt
import unittest
from unittest.mock import AsyncMock, patch

from app.adapters.base import ROTATED_ACCESS_TOKEN_KEY, AdapterError
from app.adapters.dnipro_m import (DniproMAdapter, _is_auth_failure, _is_captcha_error,
    _item_from, _login_error_in, _normalize, _pop_session, _to_local_phone)
from app.core.security import decrypt
from app.services.collector import persist_rotated_access_token


def order(number: str = "42", created_at: str = "2026-08-20T12:30:00") -> dict:
    return {
        "order_number": number,
        "created_at": created_at,
        "total": "318.00",
        "creation_provider": "site",
        "delivery_name": "Dnipro-M",
        "delivery_data": {"address": "Kyiv"},
        "payment_name": "card",
        "current_purchase_bonuses": "3.5",
        "bonuses_debiting_amount": "1",
        "has_warranty_card": True,
        "order_items": [{"name": " Drill ", "price": "399", "discount_cost": "318", "itemQuantity": "1", "code": "123"}],
    }


class NormalizeTests(unittest.TestCase):
    def test_item_from(self):
        item = _item_from(order()["order_items"][0])
        self.assertEqual(item.name, "Drill")
        self.assertEqual(item.total_price, 318)
        self.assertEqual(item.discount, 81)

    def test_normalize(self):
        receipt = _normalize(order())
        self.assertEqual(receipt.external_id, "42")
        self.assertEqual(receipt.channel, "online")
        self.assertEqual(receipt.bonuses, 3.5)
        self.assertTrue(receipt.has_warranty)

    def test_expired_session_shapes(self):
        self.assertTrue(_is_auth_failure(403, "https://dnipro-m.ua/cabinet/", "application/json", {}))
        self.assertTrue(_is_auth_failure(200, "https://dnipro-m.ua/ru/login/", "text/html", "login"))
        self.assertTrue(_is_auth_failure(200, "x", "application/json", {"message": "Необхідно увійти"}))


class LoginHelperTests(unittest.TestCase):
    def test_to_local_phone(self):
        for raw in ("+380671234567", "380671234567", "0671234567", "+38 (067) 123 45 67"):
            self.assertEqual(_to_local_phone(raw), "0671234567")

    def test_login_error_in(self):
        self.assertIsNone(_login_error_in("ми надіслали код у sms на 380671234567"))
        self.assertEqual(
            _login_error_in("телефон\nне вдалося підтвердити, що запит виконано людиною\n"),
            "не вдалося підтвердити, що запит виконано людиною",
        )
        self.assertIsNotNone(_login_error_in("код введено невірно, вам надіслано новий код"))

    def test_is_captcha_error(self):
        self.assertTrue(_is_captcha_error("Не вдалося підтвердити, що запит виконано людиною"))
        self.assertFalse(_is_captcha_error("Неправильний код підтвердження"))


class LoginSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_pop_missing_session(self):
        with self.assertRaisesRegex(AdapterError, "почніть вхід спочатку"):
            await _pop_session(None)
        with self.assertRaisesRegex(AdapterError, "почніть вхід спочатку"):
            await _pop_session("nope")

    async def test_confirm_without_session(self):
        with self.assertRaisesRegex(AdapterError, "сесія входу"):
            await DniproMAdapter().confirm_login({}, "1234")


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_paginated_history(self):
        adapter = DniproMAdapter()
        replies = iter([
            {"status": "success", "orders": [order("1")], "orders_left": True, "offset": 10},
            {"status": "success", "orders": [order("2")], "orders_left": False},
        ])

        async def fake_browser_json(access_token, extra, path):
            return next(replies)

        adapter._browser_json = fake_browser_json
        receipts = await adapter.fetch_receipts("state", {})
        self.assertEqual([receipt.external_id for receipt in receipts], ["1", "2"])

    async def test_history_stops_at_since(self):
        adapter = DniproMAdapter()

        async def fake_browser_json(access_token, extra, path):
            return {"status": "success", "orders": [order(created_at="2026-01-01T00:00:00")], "orders_left": True}

        adapter._browser_json = fake_browser_json
        receipts = await adapter.fetch_receipts("state", {}, since=dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc))
        self.assertEqual(receipts, [])

    async def test_history_reports_expired_session(self):
        adapter = DniproMAdapter()

        async def fake_browser_json(access_token, extra, path):
            return {"status": "error", "message": "Необхідно увійти"}

        adapter._browser_json = fake_browser_json
        with self.assertRaisesRegex(AdapterError, "повторна авторизація"):
            await adapter.fetch_receipts("state", {})


class BrowserLaunchTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_full_chromium_new_headless(self):
        chromium = unittest.mock.Mock()
        chromium.launch = AsyncMock(return_value="browser")
        playwright = unittest.mock.Mock(chromium=chromium)
        manager = unittest.mock.Mock()
        manager.start = AsyncMock(return_value=playwright)
        with patch("app.adapters.dnipro_m.async_playwright", return_value=manager):
            from app.adapters.dnipro_m import _launch_browser
            result = await _launch_browser()
        self.assertEqual(result, (playwright, "browser"))
        chromium.launch.assert_awaited_once_with(headless=True, channel="chrome")


class RotatedSessionTests(unittest.TestCase):
    def test_rotated_storage_state_is_encrypted_and_removed_from_extra(self):
        credential = unittest.mock.Mock(access_token_enc=None)
        account = unittest.mock.Mock(credential=credential)
        extra = {ROTATED_ACCESS_TOKEN_KEY: '{"cookies":[]}'}

        self.assertTrue(persist_rotated_access_token(account, extra))
        self.assertEqual(decrypt(credential.access_token_enc), '{"cookies":[]}')
        self.assertNotIn(ROTATED_ACCESS_TOKEN_KEY, extra)


if __name__ == "__main__":
    unittest.main()
