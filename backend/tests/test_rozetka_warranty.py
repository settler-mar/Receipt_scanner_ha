from app.adapters.rozetka import RozetkaAdapter


def test_rozetka_import_marks_item_warranty_and_expiry():
    receipt = RozetkaAdapter().parse_import({"orders": [{
        "id": "RZ-200",
        "date": "2026-01-01T00:00:00Z",
        "items": [{
            "name": "SSD", "quantity": 1, "price": 1000,
            "warranty": "36 місяців",
            "warranty_months": 36,
        }],
    }]})[0]

    assert receipt.has_warranty is True
    assert receipt.items[0].has_warranty is True
    assert receipt.warranty_expires_at is not None


def test_rozetka_import_does_not_mark_missing_warranty():
    receipt = RozetkaAdapter().parse_import({"orders": [{
        "id": "RZ-201", "date": "2026-01-01T00:00:00Z",
        "items": [{"name": "Кабель", "warranty": "Гарантійний талон відсутній"}],
    }]})[0]
    assert receipt.has_warranty is False
    assert receipt.items[0].has_warranty is False
