"""Dashboard widgets JSON API (app/api/widgets.py)."""
import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app
from app.models import models


@pytest.fixture()
def client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _override():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override
    c = TestClient(app)
    c._session = TestingSession
    yield c
    app.dependency_overrides.clear()


def _seed(client):
    db = client._session()
    store = models.Store(code="s1", name="S1", auth_type=models.AuthType.manual)
    acc = models.Account(store=store, label="A", status=models.AccountStatus.active)
    old = models.Receipt(account=acc, store_code="s1", external_id="e0",
                         purchased_at=dt.datetime(2020, 1, 1), total_amount=10, channel="offline")
    new = models.Receipt(account=acc, store_code="s1", external_id="e1",
                         purchased_at=dt.datetime(2026, 8, 1), total_amount=100,
                         total_discount=7, channel="online")
    db.add_all([store, acc, old, new])
    db.commit()
    db.close()


def test_list_widgets(client):
    keys = {w["key"] for w in client.get("/api/widgets").json()}
    assert {"stat-cards", "spend-trend", "by-store", "by-channel",
            "by-category", "fuel-prices", "recent-receipts"} <= keys


def test_stat_cards_summary(client):
    _seed(client)
    body = client.get("/api/widgets/stat-cards").json()
    assert body["widget"] == "stat-cards"
    assert body["summary"]["total_receipts"] == 2
    assert body["summary"]["total_spent"] == 110.0
    assert body["summary"]["total_discount"] == 7.0


def test_date_from_filters(client):
    _seed(client)
    body = client.get("/api/widgets/stat-cards", params={"date_from": "2026-01-01T00:00:00Z"}).json()
    assert body["summary"]["total_receipts"] == 1
    assert body["summary"]["total_spent"] == 100.0


def test_by_channel_summary(client):
    _seed(client)
    body = client.get("/api/widgets/by-channel").json()
    assert body["summary"]["online_spent"] == 100.0
    assert body["summary"]["offline_spent"] == 10.0
    assert body["summary"]["online_pct"] == 91


def test_recent_receipts_newest_first(client):
    _seed(client)
    items = client.get("/api/widgets/recent-receipts").json()["items"]
    assert items[0]["external_id" if "external_id" in items[0] else "id"]
    assert items[0]["total_amount"] == 100.0


def test_unknown_widget_404(client):
    assert client.get("/api/widgets/nope").status_code == 404


def test_store_category_matrix(client):
    db = client._session()
    s1 = models.Store(code="epi", name="Епіцентр", auth_type=models.AuthType.manual)
    s2 = models.Store(code="atb", name="АТБ", auth_type=models.AuthType.manual)
    acc1 = models.Account(store=s1, label="A1")
    acc2 = models.Account(store=s2, label="A2")
    eng = models.Category(name="Інженерка", slug="eng", path="eng", sort_order=0)
    pipe = models.Category(name="Каналізація", slug="kan", path="eng/kan", sort_order=0, parent=eng)
    food = models.Category(name="Їжа", slug="food", path="food", sort_order=1)
    db.add_all([s1, s2, acc1, acc2, eng, pipe, food])
    db.flush()
    r1 = models.Receipt(account=acc1, store_code="epi", external_id="e1",
                        purchased_at=dt.datetime(2026, 8, 1), total_amount=300)
    r1.items = [
        models.ReceiptItem(name="Труба", total_price=200, discount=0, category_id=pipe.id),
        models.ReceiptItem(name="Хліб", total_price=50, discount=0, category_id=food.id),
    ]
    r2 = models.Receipt(account=acc2, store_code="atb", external_id="e2",
                        purchased_at=dt.datetime(2026, 8, 2), total_amount=50)
    r2.items = [models.ReceiptItem(name="Батон", total_price=40, discount=0, category_id=food.id)]
    db.add_all([r1, r2])
    db.commit()
    db.close()

    m = client.get("/api/dashboard/store-category").json()
    assert [s["store_code"] for s in m["stores"]] == ["epi", "atb"]  # sorted by spend
    rows = {r["path"]: r for r in m["categories"]}
    # "eng" rolls up its "eng/kan" child, only bought at epi
    assert rows["eng"]["total"] == 200.0
    assert {c["store_code"]: c["total"] for c in rows["eng"]["per_store"]} == {"epi": 200.0}
    # food bought at both
    assert {c["store_code"]: c["total"] for c in rows["food"]["per_store"]} == {"epi": 50.0, "atb": 40.0}

    w = client.get("/api/widgets/store-category").json()
    assert w["summary"]["top_store"] == "Епіцентр"
    assert w["summary"]["stores"] == 2

    # drill-down: what was bought at epi in the "eng" branch (rolls up eng/kan)
    items = client.get(
        "/api/dashboard/store-category/items",
        params={"store_code": "epi", "category_id": rows["eng"]["category_id"]},
    ).json()
    assert [i["name"] for i in items] == ["Труба"]
    assert items[0]["net"] == 200.0 and items[0]["item_id"] and items[0]["receipt_id"]
    assert items[0]["quantity"] == 1.0 and "unit_price" in items[0]

    # and the drill-down feeds the existing item-category edit endpoint
    it = items[0]
    r = client.patch(
        f"/api/receipts/{it['receipt_id']}/items/{it['item_id']}",
        json={"category_id": rows["food"]["category_id"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["category_id"] == rows["food"]["category_id"]


def test_fuel_prices_read_from_cached_snapshots(client):
    """Dashboard fuel prices come straight from stored snapshots — no adapter
    call, no credential needed."""
    db = client._session()
    store = models.Store(code="wog", name="WOG", auth_type=models.AuthType.manual)
    acc = models.Account(store=store, label="WOG Max", status=models.AccountStatus.active)
    db.add_all([store, acc])
    db.flush()
    db.add_all([
        models.FuelPriceSnapshot(
            account_id=acc.id, store_code="wog", product_code="A95",
            product_name="A95", price=50.0, discount=2.0, currency="UAH",
            recorded_at=dt.datetime(2026, 8, 1),
        ),
        models.FuelPriceSnapshot(
            account_id=acc.id, store_code="wog", product_code="A95",
            product_name="A95", price=52.5, discount=2.0, currency="UAH",
            recorded_at=dt.datetime(2026, 8, 20),
        ),
    ])
    acc.fuel_discount_allowance = 40
    acc.fuel_discount_allowance_unit = "л"
    db.commit()
    db.close()

    rows = client.get("/api/dashboard/fuel-prices").json()
    assert len(rows) == 1
    assert rows[0]["store_code"] == "wog"
    assert rows[0]["discount_allowance"] == 40.0
    price = rows[0]["prices"][0]
    assert price["price"] == 52.5  # latest snapshot
    assert price["previous_price"] == 50.0  # prior distinct price

    widget = client.get("/api/widgets/fuel-prices").json()
    assert widget["summary"]["accounts"] == 1
