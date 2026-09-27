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


def test_browse_category_items_groups_by_normalized_name(client):
    other = _mk(client, "Інше")

    db = client._session()
    store = models.Store(code="s1", name="S1", auth_type=models.AuthType.manual)
    acc = models.Account(store=store, label="A")
    rec = models.Receipt(account=acc, store_code="s1", external_id="e1",
                         purchased_at=dt.datetime(2026, 9, 1), total_amount=1)
    rec.items = [
        models.ReceiptItem(name="Корм Felix 1кг", total_price=1, category_id=other["id"]),
        # Same product, different casing/spacing — must collapse into one row.
        models.ReceiptItem(name="корм  felix 1кг", total_price=1, category_id=other["id"],
                           product_image_url="/api/product-images/felix.jpg"),
        models.ReceiptItem(name="Сигарети XYZ", total_price=1, category_id=other["id"]),
    ]
    db.add_all([store, acc, rec])
    db.add(models.ProductCategory(normalized_name="сигарети xyz", category_id=other["id"],
                                  source=models.ProductCategorySource.user, confidence=1.0))
    db.commit()
    rec_id = rec.id
    db.close()

    r = client.get(f"/api/categories/{other['id']}/items")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_items"] == 3
    assert body["total_names"] == 2
    by_norm = {n["normalized_name"]: n for n in body["names"]}
    from app.services.text import normalize_name
    felix_norm = normalize_name("Корм Felix 1кг")
    cig_norm = normalize_name("Сигарети XYZ")
    assert by_norm[felix_norm]["count"] == 2
    assert by_norm[felix_norm]["display_name"] in ("Корм Felix 1кг", "корм  felix 1кг")
    # Two occurrences share this normalized name — the one with a photo wins
    # as the example, even though it isn't the only match.
    assert by_norm[felix_norm]["example_image_url"] == "/api/product-images/felix.jpg"
    assert by_norm[felix_norm]["example_receipt_id"] == rec_id
    assert by_norm[cig_norm]["example_image_url"] is None
    assert by_norm[cig_norm]["example_receipt_id"] == rec_id
    assert by_norm[cig_norm]["source"] == "user"


def test_reassign_category_items_moves_items_and_creates_sticky_alias(client):
    other = _mk(client, "Інше")
    pets = _mk(client, "Товари для тварин")

    db = client._session()
    store = models.Store(code="s1", name="S1", auth_type=models.AuthType.manual)
    acc = models.Account(store=store, label="A")
    rec = models.Receipt(account=acc, store_code="s1", external_id="e1",
                         purchased_at=dt.datetime(2026, 9, 1), total_amount=1)
    rec.items = [
        models.ReceiptItem(name="Корм Felix 1кг", total_price=1, category_id=other["id"]),
        models.ReceiptItem(name="Корм Felix 1кг", total_price=1, category_id=other["id"]),
        models.ReceiptItem(name="Сигарети XYZ", total_price=1, category_id=other["id"]),
    ]
    db.add_all([store, acc, rec])
    db.commit()
    db.close()

    from app.services.text import normalize_name
    felix_norm = normalize_name("Корм Felix 1кг")
    r = client.post("/api/categories/items/reassign", json={
        "category_id": other["id"],
        "normalized_names": [felix_norm],
        "target_category_id": pets["id"],
    })
    assert r.status_code == 200, r.text
    assert r.json() == {"names": 1, "items_moved": 2}

    # Moved out of "Інше" ...
    left = client.get(f"/api/categories/{other['id']}/items").json()
    assert left["total_items"] == 1
    assert left["names"][0]["normalized_name"] == normalize_name("Сигарети XYZ")

    # ... and into "Товари для тварин", as a sticky user alias.
    arrived = client.get(f"/api/categories/{pets['id']}/items").json()
    assert arrived["total_items"] == 2
    assert arrived["names"][0]["source"] == "user"

    db = client._session()
    alias = db.get(models.ProductCategory, felix_norm)
    assert alias.category_id == pets["id"]
    assert alias.source == models.ProductCategorySource.user
    db.close()


def test_manual_prompt_and_apply_include_other_catch_all(client):
    """The include_other flag round-trips through the query param and the
    apply payload: a name parked in "Інше" is invisible by default, appears
    once include_other is asked for, and its sticky "Інше" pick is overridden
    on apply — same guarantees as categorize.apply_guesses's own tests."""
    food = _mk(client, "Продукти")
    other = _mk(client, "Інше")
    _seed_uncategorised(client, "Дивний товар")

    db = client._session()
    item = db.query(models.ReceiptItem).filter_by(name="Дивний товар").one()
    item.category_id = other["id"]
    db.add(models.ProductCategory(normalized_name="дивний товар", category_id=other["id"],
                                  source=models.ProductCategorySource.user, confidence=1.0))
    db.commit()
    db.close()

    default = client.get("/api/categories/manual-prompt").json()
    assert default["names"] == []

    prompt = client.get("/api/categories/manual-prompt", params={"include_other": True}).json()
    assert prompt["names"] == ["дивний товар"]

    r = client.post("/api/categories/manual-apply", json={
        "result": '{"1": {"category_path": "produkty", "confidence": 0.9}}',
        "names": prompt["names"],
        "include_other": True,
    })
    assert r.status_code == 200, r.text
    assert r.json() == {"names": 1, "mapped": 1, "unknown_path": 0, "low_confidence": 0,
                        "no_guess": 0, "skipped_manual": 0, "items_tagged": 1}

    db = client._session()
    item = db.query(models.ReceiptItem).filter_by(name="Дивний товар").one()
    assert item.category_id == food["id"]
    db.close()
