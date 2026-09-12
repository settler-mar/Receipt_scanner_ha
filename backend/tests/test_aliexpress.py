import datetime as dt

from app.adapters.aliexpress import AliExpressAdapter, _find_order_lists
from app.services.aliexpress_login import _login_field_value


def test_aliexpress_import_keeps_status_tracking_and_product_metadata():
    receipt = AliExpressAdapter().parse_import({"data": {"orders": [{
        "orderId": "815001",
        "createTime": "2026-08-20T12:00:00Z",
        "orderStatus": "Shipped",
        "storeName": "Baseus Official Store",
        "payAmount": "US $12.50",
        "currencyCode": "USD",
        "packages": [{"trackingNumber": "LP001"}, {"mailNo": "LP002"}],
        "items": [{
            "productId": "1005001", "skuId": "SKU-7", "productName": "USB cable",
            "quantity": 2, "unitPrice": 5, "totalPrice": 10,
            "images": ["//ae01.alicdn.com/a.jpg", "https://ae01.alicdn.com/b.jpg"],
        }],
    }]}})[0]

    assert receipt.external_id == "815001"
    assert receipt.purchased_at == dt.datetime(2026, 8, 20, 12, tzinfo=dt.timezone.utc)
    assert receipt.order_status == "Shipped"
    assert receipt.tracking_numbers == ["LP001", "LP002"]
    assert receipt.items[0].merchant_name == "Baseus Official Store"
    assert receipt.items[0].product_url == "https://www.aliexpress.com/item/1005001.html"
    assert len(receipt.items[0].product_image_urls) == 2


def test_aliexpress_has_no_catalog_search():
    capabilities = AliExpressAdapter().capabilities
    assert capabilities["receipts"] is True
    assert capabilities["catalog_search"] is False


def test_aliexpress_finds_deeply_nested_order_responses_only():
    payload = {"data": {"module": {"orderList": [{"orderId": "1", "items": [{"id": "p"}]}]}}}
    assert _find_order_lists(payload) == [[{"orderId": "1", "items": [{"id": "p"}]}]]


def test_aliexpress_ukrainian_phone_uses_national_part_in_login_field():
    assert _login_field_value("+380 67 123 4567") == "671234567"
    assert _login_field_value("380671234567") == "671234567"
    assert _login_field_value("user@example.com") == "user@example.com"
