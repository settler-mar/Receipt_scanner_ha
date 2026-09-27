from __future__ import annotations

import base64
import hashlib
import urllib.parse
import unittest
from unittest.mock import AsyncMock, patch

from app.adapters.base import AdapterError, AuthResult
from app.adapters.fozzy import (
    FozzyAdapter,
    WEB_CLIENT_ID,
    WEB_REDIRECT_URI,
    _local_phone,
    _oauth_url,
    _pkce_pair,
    _normalize_offline,
    _normalize_online,
)
import datetime as dt


class OAuthHelpersTests(unittest.TestCase):
    def test_phone_normalization(self):
        for raw in ("+380671234567", "380671234567", "0671234567", "+38 (067) 123 45 67"):
            self.assertEqual(_local_phone(raw), "+380671234567")
        with self.assertRaisesRegex(AdapterError, "некоректний"):
            _local_phone("123")

    def test_pkce_and_authorize_url(self):
        verifier, challenge = _pkce_pair()
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b"=").decode()
        self.assertEqual(challenge, expected)

        query = urllib.parse.parse_qs(urllib.parse.urlparse(_oauth_url("state-1", challenge)).query)
        self.assertEqual(query["client_id"], [WEB_CLIENT_ID])
        self.assertEqual(query["redirect_uri"], [WEB_REDIRECT_URI])
        self.assertEqual(query["state"], ["state-1"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertIn("offline_access", query["scope"][0])

    def test_offline_order_discount_is_receipt_component(self):
        receipt = _normalize_offline(
            {"chequeId": "F-1", "created": "2026-08-20T12:00:00Z", "sumReg": 100},
            {"chequeLines": [{"lagerInfo": {"name": "Товар"}, "amount": 1, "sumReg": 110}],
             "sumReg": 100, "sumDiscount": 10},
            dt.datetime(2026, 8, 20, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(receipt.components[0].amount, -10)
        self.assertIsNone(receipt.total_adjustment)

    def test_online_delivery_and_order_discount_are_separate_components(self):
        receipt = _normalize_online(
            {"id": "O-1", "createdDate": "2026-08-20T12:00:00Z", "sumActual": 159},
            {"items": [{"name": "Товар", "countActual": 1, "sumActual": 150, "lineStateId": 2}],
             "sumActualGoods": 150, "sumActualDelivery": 9, "sumActualDiscount": -10},
            dt.datetime(2026, 8, 20, tzinfo=dt.timezone.utc),
        )
        self.assertEqual([(x.kind, x.amount) for x in receipt.components], [("delivery", 9)])


class RefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_web_credential_uses_web_refresh_flow(self):
        result = AuthResult(access_token="new", refresh_token="rotated")
        with patch("app.adapters.fozzy._exchange_web", AsyncMock(return_value=result)) as exchange:
            refreshed = await FozzyAdapter().refresh({"oauth_client": "web"}, "refresh")
        self.assertEqual(refreshed.access_token, "new")
        self.assertEqual(refreshed.extra, {"oauth_client": "web"})
        exchange.assert_awaited_once()
        grant = exchange.await_args.args[1]
        self.assertEqual(grant, {"grant_type": "refresh_token", "refresh_token": "refresh"})

    async def test_refresh_requires_token(self):
        with self.assertRaisesRegex(AdapterError, "refresh_token"):
            await FozzyAdapter().refresh({"oauth_client": "web"}, None)


class LoginSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirm_without_session(self):
        with self.assertRaisesRegex(AdapterError, "сесія входу"):
            await FozzyAdapter().confirm_login({}, "123456")
