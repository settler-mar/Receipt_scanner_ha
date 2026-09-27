import asyncio
import datetime as dt

import pytest

from app.adapters.base import AdapterError
from app.adapters.tabletki import (
    TabletkiAdapter,
    _cookies_from_token,
    _is_authenticated,
    _parse_reservations,
    _parse_search,
)


def _reservation(order_code: str, status_tag: str, status: str, items: str, total: str = "355,80") -> str:
    """Скелет однієї картки броні — класи й вкладеність як у живому кабінеті."""
    return f'''
    <div class="reservation-group__small-container ">
        <div class="reservation-group__small-container--header">
            <span class="reservation-group__reservation-type strong">Самовивіз</span>
            <div class="data-text">09.07.2026 в 11:04,</div>
            <div class="price-text strong ml-1">{total} грн</div>
            <button data-order-code="{order_code}" class="status-details__btn-share"></button>
        </div>
        <div class="reservation-group__holder">
            <div class="reservation-group__address">
                <div>
                    <span class="strong">АДОНІС АПТЕКА</span>
                    <span class="pharmacy-info" data-id="57906" data-viewmode="1"></span>
                </div>
                <div>пл.Чорновола В. 5, Трускавець</div>
            </div>
            <div class="reservation-group__body">{items}</div>
            <div class="tag-holder">
                <span class="tag state-tag {status_tag}"></span>
                <span class="tag-text">{status}</span>
            </div>
            <div class="status-details__address">пл.Чорновола В. 5, Трускавець</div>
        </div>
    </div>'''


_ITEM_PLAIN = '''
<div class="filter-group__list--item row ml-0 mr-0">
    <div class="reservation__quantity-info product-info-opener" data-product-id="34063">
        <div class="reservation__quantity-info--image">
            <img src="https://root.cdntbl.com/img/goods/abc/2/img_0.webp" alt="ФЕСТАЛ НЕО" />
        </div>
        <div class="reservation__quantity-info--text">
            <div><h2>ФЕСТАЛ НЕО 10 000 табл., в/о, киш./розч. №20</h2></div>
            <div class="reservation__quantity-info--manufacturer">Фармекс Груп, ТОВ</div>
            <div class="reservation__quantity-info--count">
                <span class="packages">2 упаковки</span>
                <div class="amount strong">190,00</div>
            </div>
        </div>
    </div>
</div>'''

_ITEM_PRICE_CHANGED = '''
<div class="filter-group__list--item row ml-0 mr-0">
    <div class="reservation__quantity-info product-info-opener" data-product-id="1021189">
        <div class="reservation__quantity-info--text">
            <div><h2>Новірин форте таблетки по 1000 мг</h2></div>
            <div class="reservation__quantity-info--count">
                <span class="text-nowrap packages py-2">1 упаковка</span>
                <div class="amount text-crossed fs-12">317,60</div>
                <div class="reservation__info-changed--value ml-2">
                    <div class="reservation__info-changed--warning">
                        <span class="fs-14 strong">317,61</span>
                    </div>
                </div>
            </div>
        </div>
    </div>
</div>'''

_ITEM_OUT_OF_STOCK = '''
<div class="filter-group__list--item row ml-0 mr-0">
    <div class="reservation__quantity-info product-info-opener" data-product-id="6928">
        <div class="reservation__quantity-info--text">
            <div><h2>Іхтіолова мазь 10 % по 30 г</h2></div>
            <div class="reservation__quantity-info--count">
                <span class="packages text-crossed">1 упаковка</span>
                <div class="amount text-crossed fs-12">60,47</div>
            </div>
            <div class="reservation__info-changed--value w-100">
                <div class="reservation__info-changed--warning w-100">
                    <span>Немає в наявності</span>
                    <span class="fs-14 strong">0,00 </span>
                </div>
            </div>
        </div>
    </div>
</div>'''


def test_parse_reservations_reads_bought_reservation():
    receipts = _parse_reservations(_reservation("782222352", "tag__picked", "Викуплено", _ITEM_PLAIN, "190,00"))

    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.external_id == "782222352"
    # 09.07.2026 11:04 за київським часом (UTC+3 влітку) == 08:04 UTC.
    assert receipt.purchased_at == dt.datetime(2026, 7, 9, 8, 4, tzinfo=dt.timezone.utc)
    assert receipt.total_amount == 190.0
    assert receipt.shop_name == "АДОНІС АПТЕКА"
    assert receipt.shop_address == "пл.Чорновола В. 5, Трускавець"
    assert receipt.channel == "offline"
    assert receipt.excluded is False
    assert receipt.raw["pharmacy_id"] == "57906"

    item = receipt.items[0]
    assert item.name == "ФЕСТАЛ НЕО 10 000 табл., в/о, киш./розч. №20"
    assert (item.quantity, item.unit_price, item.total_price) == (2.0, 95.0, 190.0)
    assert item.merchant_name == "Фармекс Груп, ТОВ"
    assert item.product_image_url == item.product_image_source_url


def test_parse_reservations_marks_cancelled_as_excluded():
    receipts = _parse_reservations(_reservation("256630380", "tag__cancelled", "Скасовано", _ITEM_PLAIN))

    assert receipts[0].excluded is True
    assert receipts[0].order_status == "Скасовано"
    assert "Скасовано" in receipts[0].exclusion_reason


def test_parse_reservations_uses_changed_price_and_drops_missing_item():
    receipts = _parse_reservations(_reservation(
        "130893345", "tag__picked", "Викуплено", _ITEM_PRICE_CHANGED + _ITEM_OUT_OF_STOCK, "317,61",
    ))

    items = receipts[0].items
    assert [item.total_price for item in items] == [317.61]
    assert sum(item.total_price for item in items) == receipts[0].total_amount


def test_parse_search_card():
    products = _parse_search('''
    <div class="card__category--info">
        <a title="Нурофєн форте таблетки, в/о по 400 мг №12" href="/uk/Nurofen/2948/?utm_source=main">
            <span>Нурофєн форте таблетки, в/о по 400 мг №12</span>
        </a>
    </div>
    <div class="card__category--info-additional">
        <div>Reckitt Benckiser Healthcare International Limited</div>
    </div>
    <div class="card__category--price">від<span class="large">&nbsp;166,90 </span> грн</div>
    <div class="card__category--info-where-found status-green">у 1140 аптеках</div>''')

    assert len(products) == 1
    assert products[0].name == "Нурофєн форте таблетки, в/о по 400 мг №12"
    assert products[0].price == 166.9
    assert products[0].brand == "Reckitt Benckiser Healthcare International Limited"
    assert products[0].product_url == "https://tabletki.ua/uk/Nurofen/2948/"
    assert products[0].in_stock is True


def test_anonymous_cabinet_is_not_authenticated():
    assert _is_authenticated("<a>Вхід / Реєстрація</a>") is False
    assert _is_authenticated("<a>Вихід</a>") is True


def test_broken_session_raises_adapter_error():
    with pytest.raises(AdapterError):
        _cookies_from_token("not-json")
    with pytest.raises(AdapterError):
        _cookies_from_token("{}")


def test_login_rejects_a_phone_number():
    with pytest.raises(AdapterError):
        asyncio.run(TabletkiAdapter().login_with_password("+380631234567", "secret"))


def test_login_field_is_email_and_default_stays_phone():
    from app.adapters.registry import get_adapter

    assert TabletkiAdapter().login_field == "email"
    assert get_adapter("vencon").login_field == "phone"


def test_capabilities_expose_password_login_and_catalog_search():
    capabilities = TabletkiAdapter().capabilities

    assert capabilities["phone_password_login"] is True
    assert capabilities["catalog_search"] is True
    assert capabilities["balance"] is False
