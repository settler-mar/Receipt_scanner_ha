import datetime as dt

from app.adapters.aliexpress import (
    AliExpressAdapter,
    _find_order_lists,
    _detail_components,
    _order_needs_detail,
    _receipt_adjustment,
)
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


def test_aliexpress_import_keeps_selected_product_modification():
    receipt = AliExpressAdapter().parse_import({"orders": [{
        "orderId": "815002",
        "createTime": "2026-08-20T12:00:00Z",
        "items": [{
            "productName": "Куртка",
            "skuInfo": {
                "skuPropertyValues": [
                    {"propertyName": "Color", "propertyValue": "Black"},
                    {"propertyName": "Size", "propertyValue": "XL"},
                ],
            },
            "quantity": 1,
            "unitPrice": "20",
        }],
    }]})[0]

    assert receipt.items[0].name == "Куртка (Color: Black; Size: XL)"


def test_aliexpress_import_keeps_string_sku_modification():
    receipt = AliExpressAdapter().parse_import({"orders": [{
        "orderId": "815003",
        "createTime": "2026-08-20T12:00:00Z",
        "items": [{"title": "Чохол", "skuAttr": "Color: Blue; Model: Pro"}],
    }]})[0]

    assert receipt.items[0].name == "Чохол (Color: Blue; Model: Pro)"


def test_aliexpress_dida_import_keeps_original_server_fields_for_raw_archive():
    receipt = AliExpressAdapter().parse_import({"data": {"module": {"fields": {
        "orderId": "815004",
        "orderDateText": "2026-08-20",
        "totalPriceText": "US $15.00",
        "currencyCode": "USD",
        "shippingFeeText": "US $3.00",
        "orderLines": [{"itemTitle": "Кабель", "quantity": 1, "itemPriceText": "US $12.00"}],
    }}}})[0]

    assert receipt.external_id == "815004"
    assert receipt.raw["shippingFeeText"] == "US $3.00"
    assert receipt.raw["orderLines"][0]["itemPriceText"] == "US $12.00"


def test_aliexpress_prioritizes_detail_capture_for_unexplained_totals():
    assert _order_needs_detail({
        "_raw_fields": {
            "totalPriceText": "грн.152.41",
            "orderLines": [{"itemPriceText": "грн.128.32", "quantity": 1}],
        },
    }) is True
    assert _order_needs_detail({
        "_raw_fields": {
            "totalPriceText": "грн.128.32",
            "orderLines": [{"itemPriceText": "грн.128.32", "quantity": 1}],
        },
    }) is False


def test_aliexpress_keeps_unexplained_difference_on_receipt_not_item():
    receipt = AliExpressAdapter().parse_import({"orders": [{
        "orderId": "815005",
        "createTime": "2026-08-20T12:00:00Z",
        "totalAmount": "US $15.00",
        "items": [{"productName": "Кабель", "quantity": 1, "totalPrice": 12}],
    }]})[0]

    assert receipt.total_adjustment == 3
    assert receipt.adjustment_reason is not None
    assert receipt.items[0].discount == 0


def test_aliexpress_detailed_price_breakdown_clears_unexplained_adjustment():
    order = {
        "orderId": "815006",
        "createTime": "2026-08-20T12:00:00Z",
        "totalAmount": "US $15.00",
        "items": [{"productName": "Кабель", "quantity": 1, "totalPrice": 12}],
        "_raw_fields": {"_detail_responses": [{
            "data": {"detail_order_price_block_111823": {"fields": {
                "priceDetails": [{"title": "Shipping", "value": "US $3.00"}],
                "totalPrice": {"value": "US $15.00"},
            }}}
        }]},
    }
    assert _receipt_adjustment(order, 15, AliExpressAdapter().parse_import({"orders": [order]})[0].items)[0] == 0


def test_aliexpress_detail_components_keep_delivery_and_discounts_off_items():
    order = {
        "orderId": "815007",
        "createTime": "2026-08-20T12:00:00Z",
        "totalAmount": "грн.95.00",
        "items": [{"productName": "Кабель", "quantity": 1, "totalPrice": 100}],
        "_raw_fields": {"_detail_responses": [{"data": {"fields": {
            "priceDetails": [
                {"title": "Subtotal", "value": "грн.100.00"},
                {"title": "Shipping", "value": "грн.10.00"},
                {"title": "AliExpress Coupons", "value": "-грн.15.00"},
            ],
            "totalPrice": {"value": "грн.95.00"},
        }}}]},
    }
    receipt = AliExpressAdapter().parse_import({"orders": [order]})[0]
    assert [(component.kind, component.label, component.amount) for component in receipt.components] == [
        ("delivery", "Shipping", 10),
        ("discount", "AliExpress Coupons", -15),
    ]
    assert receipt.total_adjustment == 0
    assert receipt.total_discount == 15


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
