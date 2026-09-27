from app.adapters.olx import OlxAdapter, _parse_orders_html, _parse_search_html, _phone


def test_olx_search_parser_extracts_listing_and_price():
    products = _parse_search_html('''
      <a href="/d/uk/obyavlenie/telefon-IDabc.html">
        <img src="https://ireland.apollo.olxcdn.com/a.jpg">
        <span>iPhone 15 128GB</span><span>25 500 грн</span>
      </a>
    ''')
    assert len(products) == 1
    assert products[0].name == "iPhone 15 128GB"
    assert products[0].price == 25500
    assert products[0].product_url == "https://www.olx.ua/d/uk/obyavlenie/telefon-IDabc.html"


def test_olx_phone_is_normalized_only_for_ukrainian_numbers():
    assert _phone("Телефон: +38 (067) 123-45-67") == "+380671234567"
    assert _phone("Телефон прихований") is None


def test_olx_is_public_catalog_without_receipt_import():
    adapter = OlxAdapter()
    assert adapter.catalog_search_requires_auth is False
    assert adapter.receipts_supported is True


def test_olx_order_import_normalizes_purchase_to_receipt():
    receipt = OlxAdapter().parse_import({"orders": [{
        "orderId": "OLX-42",
        "createdAt": "2026-09-20T12:30:00Z",
        "totalAmount": 1250,
        "status": "completed",
        "seller": {"name": "Іван", "phone": "+380671234567"},
        "items": [{
            "title": "Вживаний ноутбук", "quantity": 1, "price": 1250,
            "url": "https://www.olx.ua/d/uk/obyavlenie/laptop-IDabc.html",
        }],
    }]})[0]
    assert receipt.external_id == "OLX-42"
    assert receipt.total_amount == 1250
    assert receipt.shop_name == "Іван"
    assert receipt.items[0].name == "Вживаний ноутбук"
    assert receipt.items[0].product_url.endswith("IDabc.html")
    assert receipt.raw["seller_phone"] == "+380671234567"


def test_olx_embedded_order_html_is_supported():
    receipts = _parse_orders_html('''
      <script type="application/json">
        {"orders":[{"orderId":"7","createdAt":"2026-09-20T12:00:00Z",
        "price":321,"title":"Навушники","status":"completed"}]}
      </script>
    ''')
    assert len(receipts) == 1
    assert receipts[0].items[0].name == "Навушники"
    assert receipts[0].total_amount == 321
