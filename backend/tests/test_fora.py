import datetime as dt

from app.adapters.fora import _normalize_offline


def test_fora_weighted_and_multi_quantity_prices_are_line_totals():
    receipt = _normalize_offline(
        {
            "id": "F-1", "createDate": "2026-08-20T12:00:00",
            "amount": 272.62, "filialName": "Фора",
        },
        {
            "items": [
                {"lagerName": "Виноград", "lagerCountString": "0.756 1", "priceOut": 74.01},
                {"lagerName": "Піца", "lagerCountString": "2 шт", "priceOut": 199.80},
            ],
            "paymentDetail": {"itemAmount": 273.81, "discount": 1.19, "totalAmount": 272.62},
        },
        dt.datetime(2026, 8, 20, tzinfo=dt.timezone.utc),
    )

    assert [item.total_price for item in receipt.items] == [74.01, 199.80]
    assert receipt.items[0].unit_price == 97.9
    assert receipt.components[0].kind == "discount"
    assert receipt.components[0].amount == -1.19
    assert receipt.total_adjustment == 0


def test_fora_empty_detail_is_audited_as_missing_line_data():
    receipt = _normalize_offline(
        {"id": "F-2", "amount": 151.52, "filialName": "Фора"},
        {
            "items": [],
            "paymentDetail": {"itemAmount": 155.0, "discount": 3.48, "totalAmount": 151.52},
        },
        dt.datetime(2026, 8, 20, tzinfo=dt.timezone.utc),
    )

    assert receipt.total_adjustment == 151.52
    assert receipt.adjustment_reason == "Фора: API повернув чек без товарних рядків"
