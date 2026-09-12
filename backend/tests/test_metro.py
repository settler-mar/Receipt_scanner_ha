import base64
import asyncio
import datetime as dt

import pytest

from app.adapters.base import AdapterError
from app.adapters.metro import (
    MetroAdapter,
    _cart_barcode,
    _distribute_receipt_total,
    _invoice_items,
    _pdf_invoice_items,
)


class Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def test_device_login_uses_uuid_without_cart(monkeypatch):
    calls = []

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, json):
            calls.append((url, json))
            return Response(200, {"device": {"id": 77, "token": "jwt", "customerId": "shopper-1"}})

    monkeypatch.setattr("app.adapters.metro.AsyncClient", Client)
    result = asyncio.run(MetroAdapter().login_with_device_uuid("12345678-1234-1234-1234-123456789abc"))
    assert result.access_token == "jwt"
    assert result.extra["customerId"] == "shopper-1"
    assert calls[0][1]["deviceType"] == "Android"
    assert "cart" not in calls[0][1]


def test_registration_uses_cart_and_member_card(monkeypatch):
    calls = []

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, json):
            calls.append((url, json))
            if url.endswith("customer/signin"):
                return Response(200, {"device": {"id": 77, "token": "jwt", "customerId": "generated"}})
            if url.endswith("order/lookup"):
                return Response(200, {})
            if url.endswith("order/scan/cart"):
                return Response(200, {"order": {"id": 99}})
            return Response(200, {"customerId": "registered-shopper"})

    monkeypatch.setattr("app.adapters.metro.AsyncClient", Client)
    result = asyncio.run(MetroAdapter().login_with_device_registration("125", "CARD-2"))
    assert result.extra["customerId"] == "CARD-2"
    assert calls[2][1] == {"orderId": None, "deviceId": 77, "cartBarcode": "7430001100125743", "action": 1}
    assert calls[3][1]["customerId"] == "CARD-2"
    assert calls[3][1]["memberIdentifierType"] == "MEMBER_CARD"


def test_cart_number_pattern_and_arbitrary_code():
    assert _cart_barcode("125") == "7430001100125743"
    assert _cart_barcode("232") == "7430001100232743"
    assert _cart_barcode("1110") == "7430001101110743"
    assert _cart_barcode("CUSTOM-CART-CODE") == "CUSTOM-CART-CODE"


def test_fetches_history_and_invoice(monkeypatch):
    calls = []

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, json):
            calls.append((url, json))
            if url.endswith("historical/paid"):
                return Response(200, {"orders": [{"id": 42, "checkoutTime": 1_700_000_000_000, "totalPrice": 123.45}]})
            return Response(200, {"items": [{"name": "Молоко", "quantity": 2, "price": 20, "totalPrice": 40}]})

    monkeypatch.setattr("app.adapters.metro.AsyncClient", Client)
    receipts = asyncio.run(MetroAdapter().fetch_receipts(
        "jwt", {"customerId": "shopper-1", "device_uuid": "installation-uuid"}
    ))
    assert receipts[0].external_id == "42"
    assert receipts[0].total_amount == 123.45
    assert receipts[0].items[0].name == "Молоко"
    assert calls[0][1] == {"id": 0, "customerId": "shopper-1"}
    assert calls[1][1] == {"orderId": 42, "languageCode": "uk"}


def test_parses_base64_html_invoice():
    html = "<table><tr><th>Товар</th><th>К-сть</th><th>Ціна</th><th>Сума</th></tr><tr><td>Хліб</td><td>2</td><td>15,00</td><td>30,00</td></tr></table>"
    items = _invoice_items({"invoice": base64.b64encode(html.encode()).decode(), "invoiceFormat": "HTML"})
    assert [(x.name, x.quantity, x.unit_price, x.total_price) for x in items] == [("Хліб", 2, 15, 30)]


def test_parses_metro_pdf_text_rows():
    text = """I 30.08.2026 0111538: ЧУДО-ЧАДО ПЮРЕ МАН/ЯБЛ/БАН 90 . 0000002007 ШТ 16 22.58 361.28
30.08.2026 0199746: SPOMLEK СИР SALAMI 45% КГ . 0406909990 КГ 1.530 290.83 444.97"""
    items = _pdf_invoice_items(text)
    assert [(x.barcode, x.name, x.quantity, x.unit_price, x.total_price) for x in items] == [
        ("0111538", "ЧУДО-ЧАДО ПЮРЕ МАН/ЯБЛ/БАН 90", 16, 22.58, 361.28),
        ("0199746", "SPOMLEK СИР SALAMI 45% КГ", 1.53, 290.83, 444.97),
    ]


def test_distributes_aggregate_tax_without_rounding_drift():
    items = _pdf_invoice_items("""30.08.2026 0111538: ТОВАР А . 0000002007 ШТ 3 10.00 30.00
30.08.2026 0199746: ТОВАР Б . 0406909990 КГ 1.500 20.00 30.00
30.08.2026 0226788: ТОВАР В . 1008100000 ШТ 1 40.00 40.00""")
    _distribute_receipt_total(items, 120.01)
    assert sum(round(item.total_price * 100) for item in items) == 12001
    assert [item.total_price for item in items] == [36.0, 36.0, 48.01]
    assert [item.unit_price for item in items] == [12.0, 24.0, 48.01]


def test_rejects_invalid_device_uuid():
    with pytest.raises(AdapterError):
        asyncio.run(MetroAdapter().login_with_device_uuid("not-a-uuid"))


def test_capabilities_expose_device_login():
    assert MetroAdapter().capabilities["device_uuid_login"] is True
