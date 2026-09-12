from app.adapters.vencon import (
    VenconAdapter,
    _format_phone,
    _parse_balance,
    _parse_orders,
    _parse_search,
    _warranty_months,
)


def test_parse_import_keeps_only_completed_orders():
    receipts = VenconAdapter().parse_import({"orders": [
        {
            "number": "331681", "purchased_at": "2026-04-28T20:58:43+03:00",
            "total_amount": 2660, "status": "completed",
            "items": [{"name": "Витяжка", "quantity": 1, "total_price": 2660}],
        },
        {
            "number": "304863", "purchased_at": "2026-03-02T12:00:00+02:00",
            "total_amount": 0, "status": "cancelled", "items": [],
        },
    ]})
    assert [receipt.external_id for receipt in receipts] == ["331681"]
    assert receipts[0].channel == "online"
    assert receipts[0].items[0].name == "Витяжка"


def test_parse_search_card():
    products = _parse_search('''<article>
      <div class="availability-label"><span>В наявності</span></div>
      <a class="product-title black-link" href="/ua/products/x" title="Товар X"><span>X</span></a>
      <img src="/uploads/x.jpg" alt="X">
      <div class=product-price><span class=actual>12 345 <span class=currency>грн</span></span></div>
    </article>''')
    assert len(products) == 1
    assert products[0].name == "Товар X"
    assert products[0].price == 12345
    assert products[0].in_stock is True
    assert products[0].product_url == "https://vencon.ua/ua/products/x"


def test_parse_orders_reads_products_bonuses_and_cancelled_status():
    receipts = _parse_orders('''
      <div class="item-order" id="order-331681">
        <div class="block-title">
          <span class="date">від 28.04.2026</span>
          <span class="total-sum">Сума замовлення: 2 660 грн</span>
          <span class="status status-id-4">Виконано</span>
        </div>
        <div class="pay-info">Оплата: Онлайн оплата Оплачено</div>
        <span class="accrued-bonuses-count">37</span>
        <span class="spent-bonuses-count">1139</span>
        <a class="download-receipt-link" href="https://files.vencon.ua/orders/331681/check.pdf">Чек</a>
        <div class="product mb-8">
          <img src="/uploads/goods/291207/main/thumb.jpg">
          <a class="title fs-14 black-link" href="/ua/products/minola">Minola HTL 6614</a>
          <span class="count-value">1 шт</span>
          <span class="old-price">4 199 грн</span>
          <span class="actual text-color-red">3 799 грн</span>
        </div>
      </div>
      <div class="item-order" id="order-304863">
        <div class="block-title"><span class="date">від 02.03.2026</span><span class="total-sum">0 грн</span><span class="status">Анульовано</span></div>
      </div>
    ''')
    assert len(receipts) == 2
    assert receipts[0].external_id == "331681"
    assert receipts[0].total_amount == 2660
    assert receipts[0].bonuses == 37
    assert receipts[0].bonuses_used == 1139
    assert receipts[0].items[0].unit_price == 3799
    assert receipts[0].items[0].discount == 400
    assert receipts[0].items[0].product_image_url == "https://vencon.ua/uploads/goods/291207/main/thumb.jpg"
    assert receipts[1].excluded is True


def test_balance_warranty_and_phone_helpers():
    assert _parse_balance('<div class="item">Бонуси <span>324</span></div>') == 324
    assert _warranty_months('<div class="warranty-from-manufacturer">24 міс. гарантії від виробника</div>') == 24
    assert _format_phone('+380 67 123 45 67') == '+380(67)-123-45-67'
