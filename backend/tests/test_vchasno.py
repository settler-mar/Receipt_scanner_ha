from app.scanner.vchasno import parse_vchasno_html


HTML = '''
<html><head><style>.sum { font-weight: bold }</style></head><body>
<div class="header">
  <h4>ФОП ВАСИЛЬЧУК АРТЕМ ВІКТОРОВИЧ</h4>
  <p>МАГАЗИН Marco$olo</p><p>Волинська область</p><p>ІД 3095421098</p>
</div>
<table><tbody>
  <tr><td>1 x 1499.00</td><td></td><td></td></tr>
  <tr><td><b>Захисна огорожа</b></td><td></td><td><b>1499.00</b>&nbsp;Б</td></tr>
</tbody></table>
<table><tr><td>Безготівкова</td><td>1499.00 грн.</td></tr></table>
<table class="sum"><tr><td>СУМА</td><td>1499.00 грн.</td></tr></table>
<p>ЧЕК № DARnLkn-rj0</p><p>30.07.2026 12-52-23</p>
<p>Режим роботи: Онлайн</p><p>ФН ПРРО 4001348949</p>
</body></html>
'''


def test_parse_structured_vchasno_receipt():
    receipt = parse_vchasno_html(
        HTML, source_url="https://kasa.vchasno.ua/check-viewer/DARnLkn-rj0",
    )
    assert receipt is not None
    assert receipt.external_id == "fiscal:4001348949:DARnLkn-rj0"
    assert receipt.total_amount == 1499.0
    assert receipt.payment_type == "card"
    assert receipt.shop_name == "МАГАЗИН Marco$olo"
    assert len(receipt.items) == 1
    assert receipt.items[0].name == "Захисна огорожа"
    assert receipt.items[0].quantity == 1
    assert receipt.items[0].unit_price == 1499.0
    assert receipt.items[0].total_price == 1499.0


def test_rejects_unrelated_html():
    assert parse_vchasno_html(HTML, source_url="https://example.com/check") is None
