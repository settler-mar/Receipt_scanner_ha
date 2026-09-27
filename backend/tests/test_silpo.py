import datetime as dt

from app.adapters.silpo import _normalize_offline


def test_silpo_used_bonus_is_receipt_discount_component():
    receipt = _normalize_offline({
        "receiptUrl": "https://example/receipt/test",
        "createdAt": "2026-08-20T20:21:41",
        "sumReg": 23,
        "products": [{"name": "Морозиво", "quantity": 1, "price": 54.99}],
        "rewards": [{
            "rewardGroupCodeName": "CN_REWARD_VOUCHERS",
            "applyRewardAmount": 31.99,
        }],
    })
    assert receipt.components[0].amount == -31.99
    assert receipt.bonuses_used == 31.99
    assert receipt.purchased_at == dt.datetime(2026, 8, 20, 17, 21, 41, tzinfo=dt.timezone.utc)
