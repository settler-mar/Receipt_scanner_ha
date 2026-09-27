import datetime as dt

from app.adapters.rozetka import RozetkaAdapter


def test_rozetka_import_normalizes_order_and_cancelled_status():
    receipts = RozetkaAdapter().parse_import({"orders": [
        {
            "id": "RZ-100", "date_created": "2026-08-27T12:30:00Z",
            "total": "1 250,50 грн", "status": "completed",
            "seller": {"name": "Магазин Rozetka"},
            "items": [{"title": "Монітор", "quantity": 1, "price": "1250,50", "sku": "A-1"}],
        },
        {"number": "RZ-101", "created_at": "27.08.2026 15:00", "amount": 10, "status": "Скасовано"},
    ]})

    assert receipts[0].external_id == "RZ-100"
    assert receipts[0].purchased_at == dt.datetime(2026, 8, 27, 12, 30, tzinfo=dt.timezone.utc)
    assert receipts[0].shop_name == "Магазин Rozetka"
    assert receipts[0].items[0].barcode == "A-1"
    assert receipts[1].excluded is True


def test_rozetka_import_normalizes_cabinet_api_shape():
    receipt = RozetkaAdapter().parse_import({"orders": [{
        "id": 886984253, "created": "19.03.26", "status": "complete",
        "order_detailed": {
            "created_raw": "2026-03-19 13:36:48",
            "cost_with_discount": 2795,
            "goods_seller": {"title": "Rozetka"},
            "warranty": {"guarantee": True},
            "order_products": [{
                "id": 564433950, "title": "Маршрутизатор", "quantity": 1,
                "price": 2799, "cost": 2799, "cost_with_discount": 2795,
                "href": "https://rozetka.com.ua/p564433950/",
                "images": {"main": "https://example/image.jpg"},
            }],
        },
    }]})[0]

    assert receipt.external_id == "886984253"
    assert receipt.total_amount == 2795
    assert receipt.has_warranty is True
    assert receipt.items[0].name == "Маршрутизатор"
    assert receipt.items[0].total_price == 2795


def test_rozetka_import_keeps_delivery_as_receipt_component():
    receipt = RozetkaAdapter().parse_import({"orders": [{
        "id": 886984254, "created": "19.03.26", "status": "complete",
        "order_detailed": {
            "created_raw": "2026-03-19 13:36:48",
            "cost_with_discount": 533,
            "goods_seller": {"title": "Rozetka"},
            "order_delivery_info": {"cost": 35, "cost_with_discount": 35},
            "order_products": [{
                "id": 564433950, "title": "Підгузки", "quantity": 1,
                "price": 498, "cost": 498, "cost_with_discount": 498,
            }],
        },
    }]})[0]

    assert [item.total_price for item in receipt.items] == [498]
    assert [(component.kind, component.amount) for component in receipt.components] == [("delivery", 35)]
