from app.adapters.atb import _normalize_item


def test_atb_line_total_is_net_of_line_discount():
    item = _normalize_item({
        "name": "Сметана",
        "qty": "1",
        "price": "58.80",
        "sum": "58.80",
        "discount": "5.88",
    })

    assert item.total_price == 52.92
    assert item.discount == 5.88
    assert item.unit_price == 52.92


def test_atb_weighted_line_adjusts_unit_price_after_discount():
    item = _normalize_item({
        "name": "Грудинка",
        "qty": "0.305",
        "price": "532.49",
        "sum": "162.41",
        "discount": "16.40",
    })

    assert item.total_price == 146.01
    assert item.unit_price == 478.72
