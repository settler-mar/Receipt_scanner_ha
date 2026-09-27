"""Deterministic shopping-list matcher + store suggestion + API (ROADMAP.md Крок 2).

No AI.
"""
import asyncio
import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app
from app.models import models  # noqa: F401 — registers receipts/stores tables
from app.models import shopping  # noqa: F401 — registers shopping tables
from app.models.models import Account, Category, Receipt, ReceiptItem, Store
from app.models.shopping import ShoppingListItem, ShoppingListStatus, ShoppingTripItem
from app.models.shopping import ShoppingTrip, ShoppingTripItemStatus, ShoppingTripStatus
from app.services import shopping_list as svc
from app.services.text import normalize_name
from app.api.shopping_list import archive


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
    kw.setdefault("added_at", dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc))
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
    assert milk.auto_matched is False
    assert milk.bought_receipt_item_id is not None


def test_names_match_russian_and_ukrainian_spellings():
    assert svc.names_match("нагетсы", "mc нагетси курячі 1кг")
    assert svc.names_match("сыр", "spomlek сир salami 45% кг")


def test_names_match_catalogue_punctuation_and_ukrainian_construction_terms():
    assert svc.names_match(
        normalize_name("WD 40"),
        normalize_name("Мастило універсальне WD-40 200 мл"),
    )
    assert svc.names_match(
        normalize_name("алюминевая пластина шириной 20мм"),
        normalize_name("Смуга алюмінієва 20x2 мм 1000 мм анодоване срібло"),
    )


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


def test_archive_groups_completed_list_items_by_receipt(db):
    acc = _store(db)
    first = _add(db, "молоко", status=ShoppingListStatus.bought)
    second = _add(db, "хлеб", status=ShoppingListStatus.bought)
    receipt = _receipt(db, acc, items=[
        ("Молоко 2,5%", 42.0),
        ("Хліб", 25.0),
    ])
    db.refresh(receipt)
    first.bought_receipt_item_id = receipt.items[0].id
    second.bought_receipt_item_id = receipt.items[1].id
    first.bought_at = receipt.purchased_at
    second.bought_at = receipt.purchased_at
    db.commit()

    groups = archive(db)

    assert len(groups) == 1
    assert groups[0]["receipt_id"] == receipt.id
    assert [item["receipt_item_name"] for item in groups[0]["items"]] == [
        "Молоко 2,5%", "Хліб"
    ]


def test_trip_keeps_items_unassigned_to_a_store(db):
    _store(db, "atb", "АТБ")
    _store(db, "fozzy", "Фоззі")
    _add(db, "молоко", target_store_code="atb")
    _add(db, "лампа", target_store_code="fozzy")

    trip = svc.create_trip(db, store_code="atb")
    rows = db.execute(select(ShoppingTripItem).where(ShoppingTripItem.trip_id == trip.id)).scalars().all()

    assert [row.raw_text for row in rows] == ["молоко", "лампа"]


def test_explicit_all_stores_resets_previous_store_and_syncs_pending(db):
    _store(db, "eva", "EVA")
    _add(db, "молоко")
    _add(db, "сахар")

    trip = svc.create_trip(db, store_code="eva")
    assert trip.store_code == "eva"
    all_stores = svc.create_trip(db, store_code="")

    rows = db.execute(select(ShoppingTripItem).where(
        ShoppingTripItem.trip_id == all_stores.id
    )).scalars().all()
    assert all_stores.store_code is None
    assert {row.raw_text for row in rows} == {"молоко", "сахар"}


def test_active_trip_endpoint_syncs_shared_checklist(db, client):
    _add(db, "молоко")
    created = client.post("/api/shopping-list/trips", json={"store_code": None, "owner_name": "Максим"})
    assert created.status_code == 201
    trip_id = created.json()["id"]

    _add(db, "сахар")
    active = client.get("/api/shopping-list/trips/active")

    assert active.status_code == 200
    payload = active.json()
    trip = next(row for row in payload if row["id"] == trip_id)
    assert trip["owner_name"] == "Максим"
    assert {row["raw_text"] for row in trip["items"]} == {"молоко", "сахар"}


def test_trips_can_be_opened_by_multiple_participants(db, client):
    _add(db, "молоко")
    first = client.post("/api/shopping-list/trips", json={"owner_name": "Максим"}).json()
    second = client.post("/api/shopping-list/trips", json={"owner_name": "Настя"}).json()

    active = client.get("/api/shopping-list/trips/active").json()

    assert {row["id"] for row in active} == {first["id"], second["id"]}
    assert {row["owner_name"] for row in active} == {"Максим", "Настя"}


def test_idle_trip_is_completed_and_checkbox_activity_keeps_it_alive(db, client):
    _add(db, "молоко")
    trip = client.post("/api/shopping-list/trips", json={"owner_name": "Максим"}).json()
    trip_row = db.get(ShoppingTrip, trip["id"])
    trip_row.last_activity_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3, minutes=1)
    db.commit()

    assert client.get("/api/shopping-list/trips/active").json() == []
    db.refresh(trip_row)
    assert trip_row.status == ShoppingTripStatus.completed

    fresh = client.post("/api/shopping-list/trips", json={"owner_name": "Настя"}).json()
    item = fresh["items"][0]
    response = client.patch(
        f"/api/shopping-list/trips/{fresh['id']}/items/{item['id']}",
        json={"status": "bought"},
    )
    assert response.status_code == 200
    db.refresh(db.get(ShoppingTrip, fresh["id"]))
    assert db.get(ShoppingTrip, fresh["id"]).last_activity_at > trip_row.last_activity_at


def test_one_receipt_line_cannot_match_two_list_rows(db):
    acc = _store(db)
    first = _add(db, "молоко")
    second = _add(db, "молоко")
    _receipt(db, acc, items=[("Молоко Яготинське 2,6%", 42.90)])

    matched = svc.rescan_pending(db)

    assert len(matched) == 1
    db.refresh(first)
    db.refresh(second)
    assert {first.status, second.status} == {ShoppingListStatus.pending, ShoppingListStatus.bought}


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

    # The matcher only links receipts purchased *after* the row was added, so
    # this receipt has to be "now" rather than the fixture's fixed past date.
    _receipt(db, acc, when=dt.datetime.now(dt.timezone.utc),
             items=[("Молоко Яготинське 2,6% 900г", 42.90)])
    assert client.post("/api/shopping-list/rescan").json()["matched"][0]["id"] == item_id

    assert client.get("/api/shopping-list").json() == []
    got = client.get("/api/shopping-list?status=all").json()[0]
    assert got["status"] == "bought" and got["auto_matched"] is False

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


def test_trip_snapshots_pending_items_and_returns_skipped_rows(db):
    first = _add(db, "молоко")
    second = _add(db, "хлеб")

    trip = svc.create_trip(db, store_code="atb")
    trip_items = db.query(svc.ShoppingTripItem).filter(svc.ShoppingTripItem.trip_id == trip.id).all()
    assert trip.status == ShoppingTripStatus.active
    assert {item.raw_text for item in trip_items} == {"молоко", "хлеб"}

    milk_trip_item = next(item for item in trip_items if item.raw_text == "молоко")
    svc.update_trip_item(db, milk_trip_item, ShoppingTripItemStatus.bought)
    completed = svc.complete_trip(db, trip)
    assert completed.status == ShoppingTripStatus.completed

    db.refresh(first)
    db.refresh(second)
    assert first.status == ShoppingListStatus.pending
    assert second.status == ShoppingListStatus.pending


def test_routine_suggestion_uses_purchase_intervals_and_can_be_postponed(db):
    acc = _store(db)
    list_row = _add(db, "Кава")
    list_row.status = ShoppingListStatus.cancelled
    db.commit()
    _receipt(db, acc, when=dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc), items=[("Кава", 100)])
    _receipt(db, acc, when=dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc), items=[("Кава", 100)])
    _receipt(db, acc, when=dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc), items=[("Кава", 100)])

    rows = svc.routine_suggestions(db)

    assert rows and rows[0]["display_name"] == "Кава"
    assert rows[0]["interval_days"] == pytest.approx(30.5)
    svc.routine_action(db, rows[0]["normalized_name"], "postpone", postpone_days=30)
    assert svc.routine_suggestions(db) == []


# --------------------------------------------------------------------------- #
# Categorization (dictionaries first, AI for what is left)
# --------------------------------------------------------------------------- #
def test_categorize_pending_falls_back_to_ai(db, monkeypatch):
    from app.services import ai, categorize

    root = Category(name="Продукты", slug="produkty", path="produkty")
    leaf = Category(name="Молоко и сливки", slug="moloko-ta-vershky", path="produkty/moloko-ta-vershky")
    db.add_all([root, leaf])
    db.add(ShoppingListItem(raw_text="вершки 10%", normalized_name=normalize_name("вершки 10%")))
    db.commit()

    async def _fake_classify(_db, names, taxonomy, contexts=None):
        # pass 1 sees the roots only, pass 2 the branch — answer both.
        path = "produkty" if taxonomy == ["produkty"] else "produkty/moloko-ta-vershky"
        return {n: ai.CategoryGuess(category_path=path, confidence=0.9) for n in names}

    monkeypatch.setattr(ai, "classify_products", _fake_classify)
    result = asyncio.run(svc.categorize_pending(db))

    assert result["checked"] == 1 and result["by_dictionary"] == 0
    assert result["by_ai"] == 1 and result["ai_error"] is None
    row = db.execute(select(ShoppingListItem)).scalars().one()
    assert row.category_id == leaf.id
    assert svc.category_name_ru(leaf) == "Молоко и сливки"
    assert svc.category_group_name(db, leaf) == "Продукты"


def test_categorize_pending_reports_ai_failure(db, monkeypatch):
    from app.services import ai

    db.add(Category(name="Продукты", slug="produkty", path="produkty"))
    db.add(Category(name="Молоко и сливки", slug="moloko-ta-vershky", path="produkty/moloko-ta-vershky"))
    db.add(ShoppingListItem(raw_text="щось невідоме", normalized_name=normalize_name("щось невідоме")))
    db.commit()

    async def _boom(*_a, **_kw):
        raise RuntimeError("AI не налаштовано")

    monkeypatch.setattr(ai, "classify_products", _boom)
    result = asyncio.run(svc.categorize_pending(db))

    assert result["categorized"] == 0 and "AI не налаштовано" in result["ai_error"]


# --------------------------------------------------------------------------- #
# Store classes (продуктовый / аптека / строительный / …)
# --------------------------------------------------------------------------- #
def test_store_class_prefers_pinned_store_over_category(db, client):
    from app.api.shopping_list import _store_class
    from app.models.models import Category
    from app.services import store_classes

    acc = _store(db, code="epicentrk", name="Епіцентр К")
    store = db.get(Store, acc.store_id)
    food = Category(name="Продукти", slug="produkty", path="produkty")

    # A DIY chain is seeded as network_type "home"; the per-code map wins.
    assert _store_class(store, food)["store_class"] == "diy"
    # No store pinned — the category's top level decides.
    assert _store_class(None, food)["store_class"] == "grocery"
    assert _store_class(None, None)["store_class"] == store_classes.UNKNOWN

    codes = [row["code"] for row in client.get("/api/stores/classes").json()]
    assert codes[0] == "grocery" and "pharmacy" in codes


# --------------------------------------------------------------------------- #
# Automatic categorization on a schedule + the journal entry it leaves
# --------------------------------------------------------------------------- #
def _taxonomy(db):
    from app.models.models import Category
    db.add(Category(name="Продукти", slug="produkty", path="produkty"))
    db.add(Category(name="Молоко и сливки", slug="moloko-ta-vershky",
                    path="produkty/moloko-ta-vershky"))
    db.commit()


def _fake_ai(monkeypatch):
    from app.services import ai

    async def _classify(_db, names, taxonomy, contexts=None):
        path = "produkty" if taxonomy == ["produkty"] else "produkty/moloko-ta-vershky"
        return {n: ai.CategoryGuess(category_path=path, confidence=0.9) for n in names}

    monkeypatch.setattr(ai, "classify_products", _classify)


def test_auto_categorize_waits_for_the_quiet_period(db, monkeypatch):
    from app.models.models import AppEvent
    from app.services import shopping_settings

    _taxonomy(db)
    _fake_ai(monkeypatch)
    now = dt.datetime.now(dt.timezone.utc)
    db.add(ShoppingListItem(raw_text="вершки", normalized_name=normalize_name("вершки"),
                            added_at=now - dt.timedelta(minutes=3)))
    db.commit()

    # Default delay is 10 minutes: a list that was touched 3 minutes ago waits.
    assert asyncio.run(svc.run_auto_categorize(db)) is None
    assert db.execute(select(ShoppingListItem)).scalars().one().category_id is None

    shopping_settings.save(db, {"auto_categorize_delay_minutes": 2})
    result = asyncio.run(svc.run_auto_categorize(db))
    assert result and result["categorized"] == 1
    assert db.execute(select(ShoppingListItem)).scalars().one().category_id is not None

    # The run is journalled once, and an unchanged list is not re-run.
    events = db.execute(select(AppEvent)).scalars().all()
    assert len(events) == 1 and events[0].source == "shopping"
    assert "розкладено 1" in events[0].message
    assert asyncio.run(svc.run_auto_categorize(db)) is None


def test_auto_categorize_can_be_switched_off(db, monkeypatch, client):
    from app.services import shopping_settings

    _taxonomy(db)
    _fake_ai(monkeypatch)
    db.add(ShoppingListItem(raw_text="вершки", normalized_name=normalize_name("вершки"),
                            added_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)))
    db.commit()

    saved = client.put("/api/shopping-list/settings",
                       json={"auto_categorize": False}).json()
    assert saved == {"auto_categorize": False, "auto_categorize_delay_minutes": 10}
    assert shopping_settings.load(db)["auto_categorize"] is False
    assert asyncio.run(svc.run_auto_categorize(db)) is None
    assert db.execute(select(ShoppingListItem)).scalars().one().category_id is None
