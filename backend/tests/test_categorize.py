"""Deterministic tagging + the background AI classification pass (Крок 1)."""
import asyncio
import datetime as dt

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import models
from app.models.models import (
    Category,
    ProductCategory,
    ProductCategorySource,
    Receipt,
    ReceiptItem,
)
from app.services import ai, categorize


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    food = Category(name="Продукти", slug="produkty", path="produkty", sort_order=0)
    dairy = Category(name="Молочне", slug="molochne", path="produkty/molochne", sort_order=0)
    session.add_all([food, dairy])
    session.flush()
    dairy.parent_id = food.id
    session.commit()
    session._paths = {"food": food.id, "dairy": dairy.id}
    try:
        yield session
    finally:
        session.close()


def _receipt_with(session, *names) -> Receipt:
    store = models.Store(code="s1", name="S1", auth_type=models.AuthType.manual)
    acc = models.Account(store=store, label="A")
    rec = Receipt(account=acc, store_code="s1", external_id="e1",
                  purchased_at=dt.datetime(2026, 8, 1), total_amount=1)
    rec.items = [ReceiptItem(name=n, total_price=1) for n in names]
    session.add_all([store, acc, rec])
    session.commit()
    return rec


def test_apply_known_categories_uses_cache(db):
    db.add(ProductCategory(normalized_name="молоко", category_id=db._paths["dairy"],
                           source=ProductCategorySource.ai, confidence=0.9))
    db.commit()
    rec = _receipt_with(db, "Молоко 900 г", "Хліб")
    assert categorize.apply_known_categories(db, rec) == 1
    db.commit()
    by_name = {i.name: i.category_id for i in rec.items}
    assert by_name["Молоко 900 г"] == db._paths["dairy"]
    assert by_name["Хліб"] is None


def test_run_classification_batch(monkeypatch, db):
    _receipt_with(db, "Молоко", "Кефір", "Загадковий предмет")

    calls: list[list[str]] = []

    async def fake_classify(session, names, taxonomy):
        calls.append(list(taxonomy))
        if taxonomy == ["produkty"]:  # pass 1 — route to a top-level category
            return {
                "молоко": ai.CategoryGuess(category_path="produkty", confidence=0.95),
                "кефір": ai.CategoryGuess(category_path="produkty", confidence=0.9),
                "загадковий предмет": ai.CategoryGuess(category_path="", confidence=0.0),
            }
        # pass 2 — split the "produkty" bucket
        assert "produkty/molochne" in taxonomy
        assert "загадковий предмет" not in names  # unknowns never reach pass 2
        return {
            "молоко": ai.CategoryGuess(category_path="produkty/molochne", confidence=0.95),
            "кефір": ai.CategoryGuess(category_path="produkty/molochne", confidence=0.3),
        }

    monkeypatch.setattr(ai, "classify_products", fake_classify)
    n = asyncio.run(categorize.run_classification_batch(db, limit=50))
    assert n == 3
    assert calls[0] == ["produkty"]  # coarse pass sends only the roots

    items = {i.name: i.category_id for i in db.execute(select(ReceiptItem)).scalars()}
    assert items["Молоко"] == db._paths["dairy"]
    assert items["Кефір"] is None            # min(0.9, 0.3) below confidence threshold
    assert items["Загадковий предмет"] is None

    assert categorize.pending_names(db, 50) == []  # nothing re-queued
    assert float(db.get(ProductCategory, "кефір").confidence) == pytest.approx(0.3)


def test_hierarchical_falls_back_to_top_level_when_subcategory_unclear(monkeypatch, db):
    _receipt_with(db, "Молоко")

    async def fake_classify(session, names, taxonomy):
        if taxonomy == ["produkty"]:
            return {"молоко": ai.CategoryGuess(category_path="produkty", confidence=0.9)}
        return {"молоко": ai.CategoryGuess(category_path="", confidence=0.0)}  # no subcat

    monkeypatch.setattr(ai, "classify_products", fake_classify)
    asyncio.run(categorize.run_classification_batch(db, limit=50))
    assert db.get(ProductCategory, "молоко").category_id == db._paths["food"]


def test_hierarchical_single_level_tree_uses_one_flat_call(monkeypatch, db):
    # Drop the sub-category so the tree is only roots.
    db.execute(delete(Category).where(Category.path == "produkty/molochne"))
    db.commit()
    _receipt_with(db, "Молоко")
    calls: list[list[str]] = []

    async def fake_classify(session, names, taxonomy):
        calls.append(list(taxonomy))
        return {"молоко": ai.CategoryGuess(category_path="produkty", confidence=0.9)}

    monkeypatch.setattr(ai, "classify_products", fake_classify)
    asyncio.run(categorize.run_classification_batch(db, limit=50))
    assert len(calls) == 1 and calls[0] == ["produkty"]
    assert db.get(ProductCategory, "молоко").category_id == db._paths["food"]


def test_user_choice_is_sticky(monkeypatch, db):
    _receipt_with(db, "Молоко")
    categorize.record_user_choice(db, "Молоко", db._paths["food"])
    db.commit()

    async def fake_classify(session, names, taxonomy):
        return {"молоко": ai.CategoryGuess(category_path="produkty/molochne", confidence=0.99)}

    monkeypatch.setattr(ai, "classify_products", fake_classify)
    assert categorize.pending_names(db, 50) == []
    asyncio.run(categorize.run_classification_batch(db, limit=50))
    row = db.get(ProductCategory, "молоко")
    assert row.source == ProductCategorySource.user
    assert row.category_id == db._paths["food"]


def test_no_ai_backend_is_noop(db):
    _receipt_with(db, "Молоко")
    assert asyncio.run(categorize.run_classification_batch(db, limit=50)) == 0


def test_forced_reset_keeps_user_choices(monkeypatch, db):
    rec = _receipt_with(db, "Молоко", "Кефір")
    # simulate the receipts.update_item endpoint: stamp the item + record alias
    milk = next(i for i in rec.items if i.name == "Молоко")
    milk.category_id = db._paths["food"]
    categorize.record_user_choice(db, "Молоко", db._paths["food"])
    db.commit()

    async def fake(session, names, taxonomy):
        path = "produkty" if taxonomy == ["produkty"] else "produkty/molochne"
        return {n: ai.CategoryGuess(category_path=path, confidence=0.9) for n in names}

    monkeypatch.setattr(ai, "classify_products", fake)
    asyncio.run(categorize.run_classification_batch(db, limit=50))
    # кефір -> AI молочне, молоко -> user продукти
    assert {i.name: i.category_id for i in rec.items} == {
        "Молоко": db._paths["food"], "Кефір": db._paths["dairy"],
    }

    res = asyncio.run(categorize.backfill_all(db, force=True))
    assert res["reset"]["aliases_cleared"] == 1          # only the кефір AI row
    db.expire_all()
    rec2 = db.get(Receipt, rec.id)
    by_name = {i.name: i.category_id for i in rec2.items}
    assert by_name["Молоко"] == db._paths["food"]        # manual choice survived
    assert by_name["Кефір"] == db._paths["dairy"]        # re-classified back
    assert db.get(ProductCategory, "молоко").source == ProductCategorySource.user


def test_reset_all_wipes_manual_and_ai(db):
    rec = _receipt_with(db, "Молоко", "Кефір")
    milk = next(i for i in rec.items if i.name == "Молоко")
    milk.category_id = db._paths["food"]
    categorize.record_user_choice(db, "Молоко", db._paths["food"])
    categorize.apply_guesses(
        db, {"кефір": ai.CategoryGuess(category_path="produkty/molochne", confidence=0.9)},
        backend="manual",
    )

    res = categorize.reset_all_classifications(db)
    assert res["aliases_cleared"] == 2
    assert res["items_cleared"] == 2
    db.expire_all()
    assert db.query(ProductCategory).count() == 0
    assert all(i.category_id is None for i in db.get(Receipt, rec.id).items)


def test_parse_classification_result_tolerant():
    p = categorize.parse_classification_result
    a = p('```json\n{"Молоко": {"category_path": "produkty/molochne", "confidence": 0.8}}\n```')
    assert a["молоко"].category_path == "produkty/molochne" and a["молоко"].confidence == 0.8
    b = p('{"Хліб": "produkty", "Сир": {"path": "produkty/molochne"}}')
    assert b["хліб"].category_path == "produkty" and b["сир"].category_path == "produkty/molochne"

    # chatbots often leave an unescaped `"` inside a product name — invalid JSON,
    # recovered by the lenient regex scan.
    c = p(
        '{"фломастери "batman" 18шт":{"category_path":"produkty","confidence":0.9},'
        '"сир 3/4\\" вз":{"category_path":"produkty/molochne","confidence":0.8}}'
    )
    first = list(c.values())[0]
    assert first.category_path == "produkty" and first.confidence == 0.9
    assert list(c.values())[1].category_path == "produkty/molochne"

    # numbered answer keys resolved through the sent `names` list
    d = p('{"2": {"category_path": "produkty/molochne", "confidence": 0.7}}',
          names=["Молоко", "Кефір"])
    assert d["кефір"].category_path == "produkty/molochne"


def test_apply_guesses_manual_path(db):
    rec = _receipt_with(db, "Молоко", "Кефір")
    guesses = {
        "молоко": ai.CategoryGuess(category_path="Produkty", confidence=0.9),      # loose (case) match
        "кефір": ai.CategoryGuess(category_path="produkty / molochne", confidence=0.9),  # loose (spacing)
    }
    out = categorize.apply_guesses(db, guesses, backend="manual")
    assert out == {"names": 2, "mapped": 2, "unknown_path": 0, "low_confidence": 0,
                   "no_guess": 0, "skipped_manual": 0, "items_tagged": 2}
    db.expire_all()
    by_name = {i.name: i.category_id for i in db.get(Receipt, rec.id).items}
    assert by_name["Молоко"] == db._paths["food"]
    assert by_name["Кефір"] == db._paths["dairy"]
