"""Receipt hashtags — normalisation + the API surface."""
import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import models
from app.services.text import normalize_tag, normalize_tags


@pytest.mark.parametrize("raw, expected", [
    ("#поездка_львов_лето_2026", "поездка_львов_лето_2026"),
    ("  #Поездка Львов, лето-2026 ", "поездка_львов_лето_2026"),
    ("ремонт/кухня", "ремонт_кухня"),
    ("#подарки 🎁 маме", "подарки_маме"),
    ("###", ""),
])
def test_normalize_tag(raw, expected):
    assert normalize_tag(raw) == expected


def test_normalize_tags_dedupes_keeping_order():
    assert normalize_tags(["#Львів", "львів ", "ЛЬВІВ", "", "ремонт"]) == ["львів", "ремонт"]


def test_derive_unit_price_uses_receipt_precision():
    assert models.derive_unit_price(3, 100) == 33.33
    assert models.derive_unit_price(0, 100) is None


@pytest.fixture()
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)

    def _db():
        s = TestingSession()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    s = TestingSession()
    store = models.Store(code="s", name="S", auth_type=models.AuthType.manual)
    acc = models.Account(store=store, label="A")
    for i, total in enumerate((100, 200, 50)):
        s.add(models.Receipt(account=acc, store_code="s", external_id=f"e{i}",
                             purchased_at=dt.datetime(2026, 8, 1), total_amount=total))
    s.add_all([store, acc])
    s.commit()
    ids = [r.id for r in s.query(models.Receipt).all()]
    s.close()
    yield TestClient(app), ids
    app.dependency_overrides.clear()


def test_tag_lifecycle(client):
    c, ids = client
    c.patch(f"/api/receipts/{ids[0]}", json={"tags": ["#Поездка Львов, лето-2026", "львов", "ЛЬВОВ"]})
    c.patch(f"/api/receipts/{ids[1]}", json={"tags": ["поездка_львов_лето_2026"]})

    assert c.get(f"/api/receipts/{ids[0]}").json()["tags"] == ["львов", "поездка_львов_лето_2026"]

    summary = {x["tag"]: x for x in c.get("/api/receipts/tags").json()}
    assert summary["поездка_львов_лето_2026"] == {
        "tag": "поездка_львов_лето_2026", "receipt_count": 2, "total_amount": 300.0,
    }

    assert c.get("/api/receipts?tag=%23поездка_львов_лето_2026").json()["total"] == 2
    assert c.get("/api/receipts?tag=львов").json()["total"] == 1

    # rename merges into an existing tag without duplicating
    c.patch(f"/api/receipts/{ids[0]}", json={"tags": ["львов", "поездка_львов_лето_2026"]})
    c.post("/api/receipts/tags/rename", json={"from": "львов", "to": "поездка_львов_лето_2026"})
    assert c.get(f"/api/receipts/{ids[0]}").json()["tags"] == ["поездка_львов_лето_2026"]

    c.request("DELETE", "/api/receipts/tags/поездка_львов_лето_2026")
    assert c.get("/api/receipts/tags").json() == []


def test_create_receipt_with_tags(client):
    c, ids = client
    acc_id = c.get("/api/receipts?limit=1").json()["items"][0]["account_id"]
    r = c.post("/api/receipts", json={
        "account_id": acc_id, "purchased_at": "2026-08-02T10:00:00Z",
        "total_amount": 42, "tags": ["#Відпустка"], "items": [],
    })
    assert r.status_code == 201 and r.json()["tags"] == ["відпустка"]


def test_missing_unit_price_is_derived_from_quantity_and_line_total(client):
    c, _ = client
    acc_id = c.get("/api/receipts?limit=1").json()["items"][0]["account_id"]
    response = c.post("/api/receipts", json={
        "account_id": acc_id,
        "purchased_at": "2026-08-02T10:00:00Z",
        "total_amount": 75,
        "items": [{"name": "Товар", "quantity": 3, "total_price": 75}],
    })

    assert response.status_code == 201
    assert response.json()["items"][0]["unit_price"] == 25
