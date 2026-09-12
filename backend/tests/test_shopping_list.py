"""Deterministic shopping-list matcher + store suggestion + API (ROADMAP.md Крок 2).

No AI.
"""
import asyncio
import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app
from app.models import models  # noqa: F401 — registers receipts/stores tables
from app.models import shopping  # noqa: F401 — registers shopping tables
from app.models.models import Account, Receipt, ReceiptItem, Store
from app.models.shopping import ShoppingListItem, ShoppingListStatus
from app.services import shopping_list as svc
from app.services.text import normalize_name


@pytest.fixture()
def engine(tmp_path):
    # File-based, not ":memory:" — the API tests cross threads (TestClient) and
    # each in-memory connection would otherwise see its own empty database.
    eng = create_engine(
        f"sqlite:///{tmp_path / 'sl.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def db(engine):
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(engine):
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _override():
        s = TestingSession()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    yield TestClient(app)
    app.dependency_overrides.clear()


def _store(db, code="atb", name="АТБ"):
    s = Store(code=code, name=name, auth_type=models.AuthType.manual)
    db.add(s)
    db.flush()
    a = Account(store_id=s.id, label=f"{name} acc")
    db.add(a)
    db.commit()  # release the SQLite write lock — API tests share the db file
    return a


def _receipt(db, account, *, store_code="atb", when=None, items):
    r = Receipt(
        account_id=account.id,
        store_code=store_code,
        external_id=f"ext:{dt.datetime.now().timestamp()}",
        purchased_at=when or dt.datetime(2026, 8, 20, 12, 0, tzinfo=dt.timezone.utc),
        total_amount=sum(p for _, p, *_ in items),
    )
    db.add(r)
    db.flush()
    for spec in items:
        name, price = spec[0], spec[1]
        barcode = spec[2] if len(spec) > 2 else None
        db.add(ReceiptItem(
            receipt_id=r.id, name=name, total_price=price, unit_price=price,
            quantity=1, barcode=barcode,
        ))
    db.commit()
    db.refresh(r)
    return r


def _run(coro):
    return asyncio.run(coro)


def _add(db, text, **kw):
    row = ShoppingListItem(raw_text=text, normalized_name=normalize_name(text), **kw)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_normalized_name_match_flips_to_candidate(db):
    acc = _store(db)
    milk = _add(db, "молоко")
    _receipt(db, acc, items=[("Молоко Яготинське 2,6% 900г", 42.90), ("Хліб", 25.0)])

    matched = svc.rescan_pending(db)

    assert [m.id for m in matched] == [milk.id]
    db.refresh(milk)
    assert milk.status == ShoppingListStatus.bought
    assert milk.auto_matched is True
    assert milk.bought_receipt_item_id is not None


def test_barcode_match_wins_even_without_name_match(db):
    acc = _store(db)
    item = _add(db, "той самий йогурт", barcode="4820000123456")
    _receipt(db, acc, items=[("YOGURT STRAWBERRY", 30.0, "4820000123456")])

    svc.rescan_pending(db)
    db.refresh(item)
    assert item.status == ShoppingListStatus.bought


def test_reject_prevents_rematch(db):
    acc = _store(db)
    milk = _add(db, "молоко")
    r = _receipt(db, acc, items=[("Молоко 2,5%", 40.0)])
    svc.rescan_pending(db)

    db.refresh(milk)
    rej_id = milk.bought_receipt_item_id
    milk.rejected_receipt_item_id = rej_id
    milk.status = ShoppingListStatus.pending
    milk.bought_receipt_item_id = None
    db.commit()

    # Same receipt line must not be proposed again.
    svc.match_receipt(db, r)
    db.commit()
    db.refresh(milk)
    assert milk.status == ShoppingListStatus.pending


def test_no_match_leaves_pending(db):
    acc = _store(db)
    bananas = _add(db, "банани")
    _receipt(db, acc, items=[("Ковбаса", 120.0)])
    svc.rescan_pending(db)
    db.refresh(bananas)
    assert bananas.status == ShoppingListStatus.pending


def test_suggest_stores_ranks_by_coverage_then_basket(db):
    atb = _store(db, "atb", "АТБ")
    silpo = _store(db, "silpo", "Сільпо")

    _add(db, "молоко")
    _add(db, "хліб")
    _add(db, "сир")

    # АТБ has all three, Сільпо only milk (but cheaper milk).
    _receipt(db, atb, store_code="atb", items=[
        ("Молоко 2,6%", 40.0), ("Хліб житній", 30.0), ("Сир кисломолочний", 90.0),
    ])
    _receipt(db, silpo, store_code="silpo", items=[("Молоко добірне", 25.0)])

    out = svc.suggest_stores(db)
    assert [s["store_code"] for s in out] == ["atb", "silpo"]
    assert out[0]["covered_items"] == 3
    assert out[0]["total_items"] == 3
    assert out[0]["approx_basket"] == pytest.approx(160.0)
    assert out[1]["covered_items"] == 1


def test_suggest_stores_empty_without_pending(db):
    _store(db)
    assert svc.suggest_stores(db) == []


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def test_api_crud_and_confirm_reject_flow(client, db):
    acc = _store(db)

    r = client.post("/api/shopping-list", json={"raw_text": "молоко"})
    assert r.status_code == 201, r.text
    item_id = r.json()["id"]

    assert [i["raw_text"] for i in client.get("/api/shopping-list").json()] == ["молоко"]

    _receipt(db, acc, items=[("Молоко Яготинське 2,6% 900г", 42.90)])
    assert client.post("/api/shopping-list/rescan").json()["matched"][0]["id"] == item_id

    got = client.get("/api/shopping-list").json()[0]
    assert got["status"] == "bought" and got["auto_matched"] is True

    # reject -> back to pending, and not re-proposed from the same line
    assert client.post(f"/api/shopping-list/{item_id}/reject").json()["status"] == "pending"
    assert client.post("/api/shopping-list/rescan").json()["matched"] == []

    # manual tick + confirm
    client.patch(f"/api/shopping-list/{item_id}", json={"status": "bought"})
    assert client.post(f"/api/shopping-list/{item_id}/confirm").json()["auto_matched"] is False

    assert client.delete(f"/api/shopping-list/{item_id}").status_code == 204
    assert client.get("/api/shopping-list?status=all").json() == []


def test_resolve_query_falls_back_without_ai_and_does_not_cache(db):
    from app.models.shopping import ProductAlias

    q, source = _run(svc.resolve_query(db, "молоко", "atb"))
    assert (q, source) == ("молоко", "fallback")
    assert db.get(ProductAlias, ("молоко", "atb")) is None


def test_resolve_query_calls_ai_once_then_serves_cache(db, monkeypatch):
    from app.services import ai

    calls = []

    async def fake_normalize(_db, phrase, store_code):
        calls.append((phrase, store_code))
        return "молоко пастеризоване"

    monkeypatch.setattr(ai, "normalize_query", fake_normalize)

    q1, s1 = _run(svc.resolve_query(db, "молоко 2.5%", "atb"))
    assert (q1, s1) == ("молоко пастеризоване", "ai")

    q2, s2 = _run(svc.resolve_query(db, "МОЛОКО  2.5%", "atb"))  # same normalized phrase
    assert (q2, s2) == ("молоко пастеризоване", "cache")
    assert calls == [("молоко 2.5%", "atb")]  # AI hit exactly once


def test_api_suggest_store(client, db):
    atb = _store(db, "atb", "АТБ")
    client.post("/api/shopping-list", json={"raw_text": "молоко"})
    _receipt(db, atb, store_code="atb", items=[("Молоко 2,6%", 40.0)])
    out = client.get("/api/shopping-list/suggest-store").json()
    assert out[0]["store_code"] == "atb" and out[0]["covered_items"] == 1
