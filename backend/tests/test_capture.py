"""Free-phrase capture: reading a spoken message into a receipt draft or a
shopping list (CAPTURE_PLAN.md, крок 1).

The model call is mocked — what is tested here is the reading contract: the
arithmetic the app refuses to delegate, the unit/decimal normalization, and the
refusal to invent a purchase out of a phrase that carried none.
"""
import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import models  # noqa: F401 — registers receipts/stores tables
from app.models import scanner  # noqa: F401 — registers scan_jobs
from app.models import shopping  # noqa: F401 — registers shopping tables
from app.services import capture


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'capture.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# The four phrases the feature was asked for
# --------------------------------------------------------------------------- #
def test_receipt_with_line_totals():
    """«Стрижка Насти - 600. Покраска волос - 1200» — "- N" is the line total."""
    draft = capture.parse_payload({
        "intent": "receipt", "confidence": 0.9,
        "receipt": {
            "shop_name": "Парикмахерская Виктория",
            "shop_address": "Индустриальная",
            "purchased_at": None,
            "items": [
                {"name": "Стрижка Насти", "quantity": 1, "total_price": 600},
                {"name": "Покраска волос", "quantity": 1, "total_price": 1200},
            ],
        },
    })
    assert draft.intent == "receipt"
    assert draft.receipt.shop_name == "Парикмахерская Виктория"
    assert draft.receipt.shop_address == "Индустриальная"
    assert draft.receipt.total_amount == 1800
    # A quantity of one leaves no per-unit price to spell out.
    assert [i.unit_price for i in draft.receipt.items] == [None, None]


def test_receipt_mixes_line_total_and_unit_price():
    """«помидоры 2кг - 160грн. огурцы 0,7кг по 50грн» — 0.7 × 50 = 35, разом 195."""
    draft = capture.parse_payload({
        "intent": "receipt", "confidence": 0.8,
        "receipt": {
            "shop_name": "покупка у бабули",
            "shop_address": "возле Фори",
            "items": [
                {"name": "помидоры", "quantity": 2, "unit": "кг", "total_price": 160},
                {"name": "огурцы", "quantity": 0.7, "unit": "кг", "unit_price": 50},
            ],
        },
    })
    tomatoes, cucumbers = draft.receipt.items
    assert (tomatoes.total_price, tomatoes.unit_price) == (160, 80)
    assert (cucumbers.total_price, cucumbers.unit_price) == (35, 50)
    assert draft.receipt.total_amount == 195


def test_shopping_list_note_and_units():
    """«яйця 3десятка, морковку, сок, молоко для миланы»."""
    draft = capture.parse_payload({
        "intent": "shopping", "confidence": 0.9,
        "shopping": [
            {"text": "яйця", "qty": 3, "unit": "десятка"},
            {"text": "морква", "qty": 1},
            {"text": "сік", "qty": 1},
            {"text": "молоко", "qty": 1, "note": "для Мілани"},
        ],
    })
    assert draft.intent == "shopping"
    assert [(i.text, i.qty, i.unit) for i in draft.shopping][:1] == [("яйця", 3.0, "десяток")]
    assert draft.shopping[-1].note == "для Мілани"


def test_shopping_list_shared_modifier_expands():
    """«болты и гайки с шайбами M8 по 20шт, а так же крепеж для телевизора»."""
    draft = capture.parse_payload({
        "intent": "shopping", "confidence": 0.85,
        "shopping": [
            {"text": "болти M8", "qty": 20, "unit": "шт"},
            {"text": "гайки M8", "qty": 20, "unit": "шт"},
            {"text": "шайби M8", "qty": 20, "unit": "шт"},
            {"text": "кріплення для телевізора", "qty": 1},
        ],
    })
    assert len(draft.shopping) == 4
    assert all(i.unit == "шт" for i in draft.shopping[:3])
    # "для телевізора" says what the thing is for — it stays in the name.
    assert draft.shopping[-1].text == "кріплення для телевізора"
    assert draft.shopping[-1].note is None


# --------------------------------------------------------------------------- #
# Reading edge cases
# --------------------------------------------------------------------------- #
def test_numbers_survive_string_and_comma_forms():
    draft = capture.parse_payload({
        "intent": "receipt", "confidence": "0,9",
        "receipt": {"items": [{"name": "сир", "quantity": "0,5 кг", "unit_price": "160 грн"}]},
    })
    item = draft.receipt.items[0]
    assert (item.quantity, item.unit_price, item.total_price) == (0.5, 160.0, 80.0)


def test_empty_or_low_confidence_reading_is_unknown():
    assert capture.parse_payload({"intent": "receipt", "confidence": 0.9,
                                  "receipt": {"items": []}}).intent == "unknown"
    assert capture.parse_payload({"intent": "shopping", "confidence": 0.9,
                                  "shopping": []}).intent == "unknown"
    low = capture.parse_payload({
        "intent": "shopping", "confidence": 0.1,
        "shopping": [{"text": "молоко"}],
    })
    assert low.intent == "unknown" and low.shopping == []
    assert capture.parse_payload("not a dict").intent == "unknown"


def test_prompt_carries_known_shop_spellings():
    system, user = capture.build_prompt("додай чек у бабули", ["покупка у бабули"])
    assert "JSON" in system
    assert "покупка у бабули" in user
    assert "додай чек у бабули" in user


def test_known_shop_names_are_deduplicated_newest_first(db):
    import datetime as dt

    from app.models.models import Account, AuthType, Receipt, Store

    store = Store(id="s-1", code="scanner", name="Сканер", auth_type=AuthType.manual)
    account = Account(id="acc-1", store_id=store.id, label="scanner")
    db.add_all([store, account])
    db.commit()
    for day, name in [(1, "покупка у бабули"), (2, "Перукарня Вікторія"), (3, "покупка у бабули")]:
        db.add(Receipt(
            id=f"r-{day}", account_id=account.id, store_code="scanner",
            external_id=f"e-{day}", shop_name=name, total_amount=10,
            purchased_at=dt.datetime(2026, 9, day, tzinfo=dt.timezone.utc),
        ))
    db.commit()
    assert capture.known_shop_names(db) == ["покупка у бабули", "Перукарня Вікторія"]


# --------------------------------------------------------------------------- #
# Applying a draft
# --------------------------------------------------------------------------- #
def test_receipt_job_holds_a_reviewable_draft(db):
    from app.models.scanner import ScanStatus
    from app.services import scan_jobs

    draft = capture.parse_payload({
        "intent": "receipt", "confidence": 0.9,
        "receipt": {"shop_name": "Перукарня Вікторія", "shop_address": "Індустріальна",
                    "items": [{"name": "Стрижка", "total_price": 600}]},
    }, source_text="додай чек перукарня Вікторія, стрижка - 600")

    job = capture.create_receipt_job(db, draft)
    assert job.status is ScanStatus.needs_review
    assert job.raw_ocr["source_text"].startswith("додай чек")

    # The stored draft is the shape the review path already knows how to save.
    nr = scan_jobs.normalized_from_draft(job.raw_ocr["draft"], job.id)
    assert nr.shop_name == "Перукарня Вікторія"
    assert nr.shop_address == "Індустріальна"
    assert float(nr.total_amount) == 600
    assert [i.name for i in nr.items] == ["Стрижка"]


def test_parse_capture_uses_the_ai_backend(db, monkeypatch):
    seen = {}

    async def fake_run(db_, cfg, task, *, make_call, prompt_preview=""):
        seen["task"] = task
        seen["preview"] = prompt_preview
        return {"intent": "shopping", "confidence": 0.9,
                "shopping": [{"text": "молоко", "qty": 1}]}

    monkeypatch.setattr(capture.ai, "_run", fake_run)
    draft = asyncio.run(capture.parse_capture(db, " додай в список молоко "))
    assert seen["task"] == "intent"
    assert seen["preview"] == "додай в список молоко"
    assert draft.intent == "shopping"
    assert draft.source_text == "додай в список молоко"


def test_blank_text_never_reaches_the_model(db, monkeypatch):
    def explode(*args, **kwargs):  # pragma: no cover — must not run
        raise AssertionError("AI must not be called for an empty message")

    monkeypatch.setattr(capture.ai, "_run", explode)
    assert asyncio.run(capture.parse_capture(db, "   ")).intent == "unknown"


# --------------------------------------------------------------------------- #
# Refuelling — the station's own price fills in what was not said
# --------------------------------------------------------------------------- #
def test_fuel_by_amount_only():
    """«був на барс і заправився газом на 1000грн»."""
    draft = capture.parse_payload({
        "intent": "fuel", "confidence": 0.9,
        "fuel": {"store": "барс", "fuel": "газ", "amount": 1000},
    })
    assert draft.intent == "fuel"
    assert (draft.fuel.store, draft.fuel.fuel) == ("барс", "газ")
    # Litres stay unknown here — they come from the station price, not the model.
    assert (draft.fuel.amount, draft.fuel.liters) == (1000.0, None)


def test_fuel_by_liters_only():
    """«був на барс і залив 15л газа»."""
    draft = capture.parse_payload({
        "intent": "fuel", "confidence": 0.9,
        "fuel": {"store": "барс", "fuel": "газ", "liters": 15},
    })
    assert (draft.fuel.liters, draft.fuel.amount) == (15.0, None)


def test_fuel_without_a_number_is_not_an_instruction():
    """Neither a sum nor a volume — the price alone cannot say how much was bought."""
    draft = capture.parse_payload({
        "intent": "fuel", "confidence": 0.9,
        "fuel": {"store": "барс", "fuel": "газ"},
    })
    assert draft.intent == "unknown" and draft.fuel is None


def _fuel_station(db, *, price=24.5, discount=0):
    import datetime as dt

    from app.models.models import Account, AuthType, FuelPriceSnapshot, Store

    store = Store(id="s-bars", code="bars", name="Барс", auth_type=AuthType.manual,
                  network_type="azs")
    account = Account(id="acc-bars", store_id=store.id, label="Барс Максим")
    db.add_all([store, account, FuelPriceSnapshot(
        account_id=account.id, store_code="bars", product_code="lpg",
        product_name="Газ скраплений",
        price=price, discount=discount,
        recorded_at=dt.datetime(2026, 9, 8, tzinfo=dt.timezone.utc),
    )])
    db.commit()
    return store, account


def test_recorded_fuel_purchase_computes_the_missing_half(db):
    _fuel_station(db, price=25.0)

    by_amount = capture.record_fuel(db, capture.parse_payload({
        "intent": "fuel", "confidence": 0.9,
        "fuel": {"store": "барс", "fuel": "газ", "amount": 1000},
    }))
    assert by_amount["liters"] == 40.0
    assert by_amount["total_amount"] == 1000.0
    assert by_amount["store_name"] == "Барс"
    assert "ціна станції" in by_amount["price_source"]

    by_liters = capture.record_fuel(db, capture.parse_payload({
        "intent": "fuel", "confidence": 0.9,
        "fuel": {"store": "барс", "fuel": "газ", "liters": 15},
    }))
    assert by_liters["total_amount"] == 375.0
    assert by_liters["price_per_liter"] == 25.0


def test_recorded_fuel_uses_the_price_net_of_discount(db):
    _fuel_station(db, price=26.0, discount=1.0)
    out = capture.record_fuel(db, capture.parse_payload({
        "intent": "fuel", "confidence": 0.9,
        "fuel": {"store": "барс", "fuel": "газ", "liters": 10},
    }))
    assert (out["price_per_liter"], out["total_amount"]) == (25.0, 250.0)


def test_unknown_station_is_reported_not_guessed(db):
    _fuel_station(db)
    with pytest.raises(ValueError, match="не знайдено"):
        capture.record_fuel(db, capture.parse_payload({
            "intent": "fuel", "confidence": 0.9,
            "fuel": {"store": "няшка", "fuel": "газ", "amount": 500},
        }))
