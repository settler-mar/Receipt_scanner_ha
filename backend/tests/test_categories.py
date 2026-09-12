"""Category-tree CRUD + by-category rollup (ROADMAP.md Крок 1)."""
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
    # Plain constructor (no `with`) — we don't want the app lifespan (real-DB
    # migrations, scheduler startup) to run in tests.
    c = TestClient(app)
    c._session = TestingSession
    yield c
    app.dependency_overrides.clear()


def _mk(client, name, parent_id=None):
    r = client.post("/api/categories", json={"name": name, "parent_id": parent_id})
    assert r.status_code == 201, r.text
    return r.json()


def test_create_nesting_and_paths(client):
    food = _mk(client, "Продукти")
    assert food["path"] == "produkty"
    dairy = _mk(client, "Молочне", parent_id=food["id"])
    assert dairy["path"] == "produkty/molochne"
    assert dairy["parent_id"] == food["id"]


def test_rename_cascades_to_descendants(client):
    food = _mk(client, "Продукти")
    dairy = _mk(client, "Молочне", parent_id=food["id"])
    milk = _mk(client, "Молоко", parent_id=dairy["id"])

    r = client.patch(f"/api/categories/{food['id']}", json={"name": "Їжа"})
    assert r.status_code == 200
    paths = {c["name"]: c["path"] for c in client.get("/api/categories").json()}
    assert paths["Їжа"] == "izha"
    assert paths["Молочне"] == "izha/molochne"
    assert paths["Молоко"] == "izha/molochne/moloko"


def test_reparent_rejects_cycle(client):
    a = _mk(client, "A")
    b = _mk(client, "B", parent_id=a["id"])
    r = client.patch(f"/api/categories/{a['id']}", json={"parent_id": b["id"]})
    assert r.status_code == 400


def test_delete_promotes_children(client):
    food = _mk(client, "Продукти")
    dairy = _mk(client, "Молочне", parent_id=food["id"])
    milk = _mk(client, "Молоко", parent_id=dairy["id"])

    assert client.delete(f"/api/categories/{dairy['id']}").status_code == 204
    cats = {c["name"]: c for c in client.get("/api/categories").json()}
    assert "Молочне" not in cats
    assert cats["Молоко"]["parent_id"] == food["id"]
    assert cats["Молоко"]["path"] == "produkty/moloko"


def test_by_category_rollup(client):
    food = _mk(client, "Продукти")
    dairy = _mk(client, "Молочне", parent_id=food["id"])

    db = client._session()
    store = models.Store(code="s1", name="S1", auth_type=models.AuthType.manual)
    acc = models.Account(store=store, label="A")
    rec = models.Receipt(
        account=acc, store_code="s1", external_id="e1",
        purchased_at=dt.datetime(2026, 8, 1), total_amount=100,
    )
    rec.items = [
        models.ReceiptItem(name="Молоко", total_price=40, discount=5, category_id=dairy["id"]),
        models.ReceiptItem(name="Цукерки", total_price=30, discount=0, category_id=food["id"]),
        models.ReceiptItem(name="Загадка", total_price=10, discount=0),
    ]
    db.add_all([store, acc, rec])
    db.commit()
    db.close()

    rows = {r["path"]: r for r in client.get("/api/dashboard/by-category").json()}
    assert rows["produkty/molochne"]["direct_total"] == 40.0
    assert rows["produkty"]["direct_total"] == 30.0
    assert rows["produkty"]["rollup_total"] == 70.0
    assert rows[""]["name"] == "Без категорії"
    assert rows[""]["direct_total"] == 10.0


def test_category_stats_counts(client):
    food = _mk(client, "Продукти")
    dairy = _mk(client, "Молочне", parent_id=food["id"])

    db = client._session()
    store = models.Store(code="s1", name="S1", auth_type=models.AuthType.manual)
    acc = models.Account(store=store, label="A")
    rec = models.Receipt(account=acc, store_code="s1", external_id="e1",
                         purchased_at=dt.datetime(2026, 8, 1), total_amount=1)
    rec.items = [
        models.ReceiptItem(name="Молоко", total_price=1, category_id=dairy["id"]),
        models.ReceiptItem(name="Кефір", total_price=1, category_id=dairy["id"]),
        models.ReceiptItem(name="Цукерки", total_price=1, category_id=food["id"]),
        models.ReceiptItem(name="Загадка", total_price=1),
    ]
    db.add_all([store, acc, rec])
    db.add(models.ProductCategory(normalized_name="молоко", category_id=dairy["id"],
                                  source=models.ProductCategorySource.ai, confidence=0.9))
    db.commit()
    db.close()

    st = client.get("/api/categories/stats").json()
    assert st["total_items"] == 3
    assert st["uncategorized_items"] == 1
    assert st["classified_names"] == 1
    by_id = {s["category_id"]: s for s in st["per_category"]}
    assert (by_id[dairy["id"]]["direct_items"], by_id[dairy["id"]]["rollup_items"]) == (2, 2)
    assert (by_id[food["id"]]["direct_items"], by_id[food["id"]]["rollup_items"]) == (1, 3)


def _seed_uncategorised(client, *names):
    db = client._session()
    store = models.Store(code="s1", name="S1", auth_type=models.AuthType.manual)
    acc = models.Account(store=store, label="A")
    rec = models.Receipt(
        account=acc, store_code="s1", external_id="e1",
        purchased_at=dt.datetime(2026, 8, 1), total_amount=100,
    )
    rec.items = [models.ReceiptItem(name=n, total_price=10) for n in names]
    db.add_all([store, acc, rec])
    db.commit()
    db.close()


def test_manual_prompt_single_and_batches(client):
    _mk(client, "Продукти")
    _seed_uncategorised(client, "Молоко", "Хліб", "Сир", "Яйця", "Мед")

    r = client.get("/api/categories/manual-prompt", params={"limit": 100, "batch_size": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 5
    assert "молоко" in body["prompt"]                       # one-message mode has all names
    assert "produkty" in body["preamble"]                   # taxonomy, no products
    assert "молоко" not in body["preamble"]
    assert [b["count"] for b in body["batches"]] == [2, 2, 1]
    assert len({b["filename"] for b in body["batches"]}) == 3
    # batches are numbered JSON objects with a running id offset
    import json as _j
    assert list(_j.loads(body["batches"][0]["prompt"])) == ["1", "2"]
    assert list(_j.loads(body["batches"][2]["prompt"])) == ["5"]


def test_manual_apply_maps_numeric_ids_to_names(client):
    _mk(client, "Продукти")
    _seed_uncategorised(client, "Молоко", "Хліб")
    names = client.get("/api/categories/manual-prompt").json()["names"]

    r = client.post("/api/categories/manual-apply", json={
        "result": '{"1": {"category_path": "produkty", "confidence": 0.9}}',
        "names": names,
    })
    assert r.status_code == 200, r.text
    assert r.json()["items_tagged"] == 1
    # the classified name left the queue for good
    again = client.get("/api/categories/manual-prompt").json()["names"]
    assert names[0] not in again


def test_reset_endpoint_clears_everything(client):
    _mk(client, "Продукти")
    _seed_uncategorised(client, "Молоко")
    client.post("/api/categories/manual-apply", json={"result": '{"Молоко": "produkty"}'})
    assert client.get("/api/categories/stats").json()["classified_names"] == 1

    r = client.post("/api/categories/reset")
    assert r.status_code == 200, r.text
    assert r.json() == {"aliases_cleared": 1, "items_cleared": 1}
    assert client.get("/api/categories/stats").json()["classified_names"] == 0
