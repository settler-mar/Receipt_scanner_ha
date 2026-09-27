import datetime as dt

import pytest

from app.adapters.base import AdapterError
from app.adapters.liki import (
    _AJAX_HEADERS,
    _normalize_login,
    _parse_fishka,
    _parse_orders,
    _parse_search,
    _ajax_payload,
)


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_normalize_login_accepts_email_and_every_phone_shape():
    assert _normalize_login(" user@example.com ") == "user@example.com"
    assert _normalize_login("0671234567") == "380671234567"
    assert _normalize_login("+38 (067) 123-45-67") == "380671234567"
    # The message names the digit count: the usual cause is a typo'd number in
    # the account card, and "неправильний формат" alone doesn't help find it.
    with pytest.raises(AdapterError, match="їх 11"):
        _normalize_login("+38063239292")


def test_ajax_requests_stay_form_encoded():
    # liki.ua reads POST bodies as form data only: a JSON body is parsed as no
    # fields at all, so a login sent that way comes back as "логін або пароль не
    # знайдені" even when the credentials are right.
    assert _AJAX_HEADERS["Content-Type"] == "application/x-www-form-urlencoded"
    assert _AJAX_HEADERS["X-Requested-With"] == "XMLHttpRequest"


def test_ajax_payload_flags_captcha_separately_from_a_plain_error():
    assert _ajax_payload(_Response({"result": "success", "data": {"txs": []}})) == {"txs": []}
    with pytest.raises(AdapterError, match="захисний код"):
        _ajax_payload(_Response({"result": "error", "errmsg": "Помилка", "errcode": 206}))
    with pytest.raises(AdapterError, match="Введені логін або пароль не знайдені"):
        _ajax_payload(_Response({"result": "error", "errmsg": "Введені логін або пароль не знайдені", "errcode": 200}))


# Trimmed from a live https://liki.ua/shop/search?query=парацетамол response.
SEARCH_HTML = '''
<div class="catalog-list__wrapper">
<div class="catalog-block"><div class="catalog-block__wrapper"><div class="catalog-block__thumb">
<picture><source type="image/webp" srcset="https://liki.ua/content/shop/products/46013/p-webplist-200x133-8ebd.webp 1x">
<img src="https://liki.ua/content/shop/products/46013/p-list-200x133-4813.jpg" width="200" height="133" alt="Парацетамол-Дарниця"></picture></div>
<div class="catalog-block__title"><a href="https://liki.ua/shop/paratsetamol-darnitsya-tabletki-po-500-mg-10-blister-p46013">Парацетамол-Дарниця таблетки по 500 мг №10 (блістер)</a></div>
<div class="catalog-block__subtitle"><div>Дарниця (Україна)</div><div><a href="/shop/series/paratsetamol-darnitsya-13553">Парацетамол-Дарниця</a></div></div>
<div class="catalog-block__price"><b>від <span class="span">32.00</span> ₴</b></div>
<div><div class="catalog-block__reality --green">Без рецепта</div><div class="catalog-block__reality --green">в 22 аптеках</div></div>
</div></div>
<div class="catalog-block"><div class="catalog-block__wrapper">
<div class="catalog-block__title"><a href="/shop/paratsetamol-p4429">Парацетамол капсули по 500 мг №10</a></div>
<div class="catalog-block__subtitle"><div>Фарм Компанія Здоров'я (Україна)</div></div>
<div class="catalog-block__price"><b>від <span class="span">25.50</span> ₴</b></div>
<div><div class="catalog-block__reality --red">немає в наявності</div></div>
</div></div>
</div>
'''


def test_parse_search_reads_price_brand_and_pharmacy_availability():
    products = _parse_search(SEARCH_HTML)
    assert len(products) == 2
    first, second = products
    assert first.name == "Парацетамол-Дарниця таблетки по 500 мг №10 (блістер)"
    assert first.price == 32.0
    assert first.brand == "Дарниця"
    assert first.in_stock is True
    assert first.product_url.endswith("-p46013")
    assert first.image_url.endswith("p-list-200x133-4813.jpg")
    # No "в N аптеках" line at all — the card doesn't say, so neither do we.
    assert second.in_stock is None
    assert second.product_url == "https://liki.ua/shop/paratsetamol-p4429"


# Trimmed from the live cabinet (https://liki.ua/shop/orders) — the customer's
# own phone block, which the real page renders in .lk-order-block__address-content,
# is kept here so the parser is pinned to never pick it up as the shop address.
ORDERS_HTML = '''
<div class="lk-order-block">
  <div class="lk-order-block__head">
    <button class="lk-order-block__head-top j-order-toggle" type="button">
      <div class="div1"><div class="lk-order-block__head-number"><b>№ 639139279</b><div>|</div><span>21 вересня 2026</span></div></div>
      <div class="div3"><div class="lk-order-block__label --completed">Виконано</div></div>
    </button>
    <div class="lk-order-block__head-wrapper">
      <div class="lk-order-block__head-count">
        <div><span class="color">2</span><span>товари</span></div><div>|</div><div><span class="color">424.50 ₴</span></div>
      </div>
    </div>
  </div>
  <div class="lk-order-block__order collapse j-order-content"><div class="lk-one-order">
    <div class="drugstore-card__pharmacy">
      <div class="drugstore-card__pharmacy-title"><img src="/content/map/brands/937/x.png"><span>Аптека оптових ц...</span></div>
      <div class="drugstore-card__pharmacy-location"><i class="icon-pin"></i><span>с. Святопетрівське, вул. Теплична, 32а</span></div>
    </div>
    <div class="lk-one-order__info"><div class="lk-one-order__info-content"><div class="lk-order-block__address">
      <div class="lk-order-block__address-content"><p>+38 (063) 000-00-00</p></div>
    </div></div></div>
    <div class="lk-one-order__list">
      <div class="lk-one-order__block">
        <div class="lk-one-order__block-thumb"><img src="https://liki.ua/content/shop/products/50483/panadol-cart-60-60.jpg" width="60" height="60"></div>
        <div class="lk-one-order__block-content">
          <div class="lk-one-order__block-title"><a href="https://liki.ua/shop/panadol-bebi-p50483">Панадол Бебі суспензія оральна 120 мг/5 мл флакон 100 мл</a></div>
          <div class="lk-one-order__block-desc">Фармаклер (Франція)</div>
          <div class="lk-one-order__block-count">
            <div class="div1">упаковка: <b>&nbsp;2</b></div>
            <div class="lk-one-order__block-price">188.00 <small>₴</small></div>
          </div>
        </div>
      </div>
      <div class="lk-one-order__block">
        <div class="lk-one-order__block-content">
          <div class="lk-one-order__block-title"><a href="https://liki.ua/shop/nazoferon-p20091">Назоферон спрей назальний 100 000 МО/мл флакон 5 мл 1 шт</a></div>
          <div class="lk-one-order__block-desc">Фармак (Україна)</div>
          <div class="lk-one-order__block-count">
            <div class="div1">упаковка: <b>&nbsp;1</b></div>
            <div class="lk-one-order__block-price">236.50 <small>₴</small></div>
          </div>
        </div>
      </div>
    </div>
    <div class="lk-one-order__foot">
      <div class="lk-one-order__foot-left"><a href="https://liki.ua/shop/orderspdf/639139279-mec6srby.pdf">Роздрукувати</a></div>
      <div class="lk-one-order__total">Разом: 424.50 ₴</div>
    </div>
  </div></div>
</div>
<div class="lk-order-block">
  <div class="lk-order-block__head">
    <div class="lk-order-block__head-number"><b>№ 639100001</b><div>|</div><span>2 вересня 2026</span></div>
    <div class="lk-order-block__label --cancel">Скасовано</div>
    <div class="lk-order-block__head-count"><div><span>0</span></div><div>|</div><div><span>0.00 ₴</span></div></div>
  </div>
</div>
'''


def test_parse_orders_reads_items_pharmacy_and_cancelled_status():
    receipts = _parse_orders(ORDERS_HTML)
    assert [r.external_id for r in receipts] == ["639139279", "639100001"]

    done = receipts[0]
    assert done.total_amount == 424.5
    assert done.shop_name == "Аптека оптових ц..."
    assert done.shop_address == "с. Святопетрівське, вул. Теплична, 32а"
    assert done.channel == "online"
    assert done.raw["source"] == "pharmacy_preorder"
    assert done.excluded is False
    assert done.receipt_url == "https://liki.ua/shop/orderspdf/639139279-mec6srby.pdf"
    # "21 вересня 2026", no time on the card — midday Kyiv (UTC+3 in September).
    assert done.purchased_at == dt.datetime(2026, 9, 21, 9, 0, tzinfo=dt.timezone.utc)
    # The card prints one price per line; it is read as the line total (the two
    # real lines sum exactly to "Разом: 424.50"). Quantities above 1 are not
    # confirmed against a live order yet — this account has none.
    assert [(i.name[:20], i.quantity, i.total_price, i.unit_price) for i in done.items] == [
        ("Панадол Бебі суспенз", 2, 188.0, 94.0),
        ("Назоферон спрей наза", 1, 236.5, 236.5),
    ]
    assert done.items[0].merchant_name == "Фармаклер (Франція)"
    assert done.items[0].product_image_url.endswith("panadol-cart-60-60.jpg")

    cancelled = receipts[1]
    assert cancelled.excluded is True
    assert cancelled.exclusion_reason == "Статус замовлення: Скасовано"


def test_parse_fishka_keys_accruals_by_order_and_builds_the_pdf_url():
    accruals = _parse_fishka({"txs": [
        {"txID": 1, "txDtAdd": "2026-09-14", "orderRID": 20418832, "orderPdfKey": "a1b2c3",
         "txAmount": "9.88", "txStatus": "confirm", "statusName": "Нараховано"},
        {"txID": 2, "txDtAdd": "2026-09-10", "txComment": "Ручне коригування", "txAmount": "5",
         "txStatus": "confirm", "statusName": "Нараховано"},
    ]})
    assert list(accruals) == ["20418832"]
    assert accruals["20418832"]["bonuses"] == 9.88
    assert accruals["20418832"]["receipt_url"] == "https://liki.ua/orderspdf/20418832-a1b2c3.pdf"
