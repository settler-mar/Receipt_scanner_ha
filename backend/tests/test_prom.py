import datetime as dt

import pytest

from app.adapters.prom import PromAdapter, _graphql_receipt, _parse_catalog_html
from app.services.prom_login import _to_intl_phone


def test_prom_import_normalizes_orders_and_excludes_cancelled():
    receipts = PromAdapter().parse_import({"orders": [
        {
            "id": 12345,
            "date_created": "2026-08-27T12:30:00Z",
            "price": "1 250,50 грн",
            "status": "received",
            "company_name": "Магазин інструментів",
            "delivery_address": "Київ, відділення 1",
            "payment_option": {"name": "Пром-оплата"},
            "products": [{
                "name": "Шурупокрут", "quantity": 2, "price": "600", "total_price": 1200,
                "discount": 50, "url": "https://prom.ua/p1.html", "image": "https://example/img.jpg",
            }],
        },
        {
            "number": "P-2", "purchased_at": "27.08.2026 15:00", "total_amount": 10,
            "status": "Скасовано", "items": [],
        },
    ]})

    assert len(receipts) == 2
    assert receipts[0].external_id == "12345"
    assert receipts[0].purchased_at == dt.datetime(2026, 8, 27, 12, 30, tzinfo=dt.timezone.utc)
    assert receipts[0].total_amount == 1250.5
    assert receipts[0].shop_name == "Магазин інструментів"
    assert receipts[0].payment_type == "Пром-оплата"
    assert receipts[0].items[0].quantity == 2
    assert receipts[0].items[0].product_url == "https://prom.ua/p1.html"
    assert receipts[1].excluded is True


def test_prom_import_accepts_nested_data_and_calculates_total():
    receipt = PromAdapter().parse_import({"data": {"items": [{
        "order_number": "A-7", "created_at": 1787832000000,
        "items": [{"title": "Кабель", "count": 3, "unit_price": 40}],
    }]}})[0]
    assert receipt.total_amount == 120
    assert receipt.items[0].name == "Кабель"


def test_prom_import_rejects_unknown_shape():
    with pytest.raises(ValueError, match="масивом orders"):
        PromAdapter().parse_import({"result": []})


def test_prom_catalog_html_parser_extracts_products():
    products = _parse_catalog_html("""
      <a href="/p1926313099-shnur-kabel-hdmi.html"><img src="https://images.prom.ua/hdmi.jpg">Шнур кабель HDMI-HDMI 1,5 метра</a>
      <div>Готово к отправке</div>
      <div>98 ₴</div>
      <a href="/p2603499437-kabel-dlya-routera.html">Кабель для роутера 12v</a>
      <span>В наличии</span>
      <strong>68 ₴</strong>
    """)

    assert [p.name for p in products] == ["Шнур кабель HDMI-HDMI 1,5 метра", "Кабель для роутера 12v"]
    assert products[0].price == 98
    assert products[0].image_url == "https://images.prom.ua/hdmi.jpg"
    assert products[0].product_url == "https://prom.ua/p1926313099-shnur-kabel-hdmi.html"
    assert products[1].in_stock is True


def test_prom_graphql_order_normalization():
    receipt = _graphql_receipt({
        "id": 423211618,
        "dateCreated": "2026-08-23T23:05:00+03:00",
        "statusChain": [{"key": "received", "title": "Отримано", "isExist": True}],
        "company": {"name": "Бензоград"},
        "orderPaymentOption": {"type": "card"},
        "cart": {
            "totals": [{"amount": "180.00", "currency": "UAH", "currencyText": "₴"}],
            "cartItems": [{
                "productId": 10, "productName": "Щітки", "quantity": 2,
                "productPrice": 50, "productBasePrice": 60,
                "measureUnit": {"value": "шт."},
                "maybeProduct": {"product": {"name": "Щітки", "imageUrl": "https://images.prom.ua/a.jpg"}},
            }],
        },
    })

    assert receipt.external_id == "423211618"
    assert receipt.total_amount == 180
    assert receipt.shop_name == "Бензоград"
    assert receipt.payment_type == "card"
    assert receipt.items[0].total_price == 100
    assert receipt.items[0].discount == 20
    assert receipt.items[0].product_image_url == "https://images.prom.ua/a.jpg"


@pytest.mark.parametrize("raw", ["0671234567", "+380671234567", "+38 (067) 123-45-67", "380671234567"])
def test_prom_intl_phone(raw):
    assert _to_intl_phone(raw) == "+380671234567"
