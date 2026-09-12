import datetime as dt

from app.adapters.brain import (
    BrainAdapter,
    _add_months,
    _format_phone,
    _parse_orders,
    _warranty_months,
)

# Trimmed real /cabinet/dashboard/ markup (one completed pickup order,
# one cancelled order).
DASHBOARD_HTML = """
<div class="br-prof-order-block">
  <div class="order-item" data-order-id="2075883">
    <div class="br-prof-table-line">
      <div class="br-prof-table-order">00000608453</div>
      <div class="br-prof-table-date">от 07.09.2026</div>
      <div class="br-prof-table-amount">999<span>₴</span></div>
      <div class="br-prof-table-status">
        <div class="br-prof-table-status-wrapper" style="color:#0a0">
          <span class="d-inline-block">Виконано</span>
        </div>
      </div>
    </div>
    <div class="br-prof-table-hidden">
      <div class="br-prof-th-list">
        <div class="br-prof-th-item" data-product-code="U0162513" data-order-line-id="2622167">
          <div class="br-prof-th-img"><a href="/Veb-kamera_Logitech-p219813.html">
            <img data-observe-src="https://brain.com.ua/static/images/prod_img/1/3/U0162513.jpg"></a></div>
          <div class="prof-th-cont"><div>
            <a class="link-hov-text" itemprop="url" href="/Veb-kamera_Logitech-p219813.html">
              Веб-камера Logitech Webcam C270 HD (960-001063) </a></div></div>
          <div class="br-prof-th-price"> 999 <span>₴</span> </div>
          <div class="br-prof-th-quantity"> 1 шт </div>
          <div class="br-prof-th-warranty">
            <div class="return isRepairable isReturnable">Сдать в ремонт/Вернуть</div>
            <div class="warranty-wrapper"><i class="warranty-icon"></i><span>Гарталон</span></div>
          </div>
        </div>
      </div>
      <div class="br-prof-th-info">
        <h4>Інформація про доставку</h4>
        <div class="br-prof-info-cont"><div>
          <div class="br-prof-info-chain br-prof-iс-pm">
            <div class="col-name">Спосіб оплати:</div>
            <div class="col-content"><div>Готівкою</div><div class="invoice-btn">Завантажити чек</div></div>
          </div>
          <div class="br-prof-info-chain">
            <div class="col-name">Адреса доставки:</div>
            <div class="col-content">Магазин Brain Софіївська Борщагівка, просп. Героїв 24/83</div>
          </div>
        </div></div>
      </div>
    </div>
  </div>
  <div class="order-item" data-order-id="2000001">
    <div class="br-prof-table-line">
      <div class="br-prof-table-order">00000600000</div>
      <div class="br-prof-table-date">от 01.02.2025</div>
      <div class="br-prof-table-amount">1<span>₴</span></div>
      <div class="br-prof-table-status">
        <div class="br-prof-table-status-wrapper"><span class="d-inline-block">Скасовано</span></div>
      </div>
    </div>
    <div class="br-prof-table-hidden"><div class="br-prof-th-list"></div></div>
  </div>
</div>
"""


def test_parse_orders_reads_items_status_and_warranty_flag():
    receipts = _parse_orders(DASHBOARD_HTML)
    assert [r.external_id for r in receipts] == ["2075883", "2000001"]

    done = receipts[0]
    assert done.excluded is False
    assert done.total_amount == 999
    assert done.purchased_at == dt.datetime(2026, 9, 7, 9, tzinfo=dt.timezone.utc)
    assert done.payment_type == "Готівкою"
    assert done.raw["order_number"] == "00000608453"
    assert done.receipt_url == "https://brain.com.ua/api/v1/myorder/invoice/2075883/"
    assert "Софіївська" in done.shop_address

    item = done.items[0]
    assert item.name == "Веб-камера Logitech Webcam C270 HD (960-001063)"
    assert item.barcode == "U0162513"
    assert item.quantity == 1
    assert item.unit_price == 999
    assert item.total_price == 999
    assert item.has_warranty is True
    assert item.product_url == "https://brain.com.ua/Veb-kamera_Logitech-p219813.html"
    assert item.product_image_url.endswith("U0162513.jpg")

    cancelled = receipts[1]
    assert cancelled.excluded is True
    assert "Скасовано" in cancelled.exclusion_reason


def test_parse_import_wraps_parse_orders():
    receipts = BrainAdapter().parse_import({"html": DASHBOARD_HTML})
    assert receipts[0].external_id == "2075883"


def test_warranty_months_from_json_and_text():
    assert _warranty_months('..."Warranty":"24",...') == 24
    assert _warranty_months('<p class="br-pay-p">Гарантія: 12 міс.</p>') == 12
    assert _warranty_months("<div>Гарантия: 2 года</div>") == 24
    assert _warranty_months("<div>нічого</div>") is None


def test_add_months_and_phone_helpers():
    assert _add_months(dt.datetime(2026, 9, 7), 24) == dt.datetime(2028, 9, 7)
    assert _add_months(dt.datetime(2026, 1, 31), 1) == dt.datetime(2026, 2, 28)
    assert _format_phone("+380 (67) 123-45-67") == "0671234567"
    assert _format_phone("0671234567") == "0671234567"
