"""Manual per-item warranty: typed-in terms + attached photos, no PDF involved."""
import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import receipts as receipts_api
from app.db import Base, get_db
from app.main import app
from app.models import models


@pytest.fixture()
def client(tmp_path, monkeypatch):
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
    monkeypatch.setattr(receipts_api, "ITEM_FILE_DIR", tmp_path / "item_files")

    s = TestingSession()
    store = models.Store(code="s", name="S", auth_type=models.AuthType.manual)
    acc = models.Account(store=store, label="A")
    receipt = models.Receipt(account=acc, store_code="s", external_id="e1",
                             purchased_at=dt.datetime(2026, 9, 1), total_amount=999)
    item = models.ReceiptItem(receipt=receipt, name="Дриль", quantity=1, total_price=999)
    s.add_all([store, acc, receipt, item])
    s.commit()
    ids = (receipt.id, item.id)
    s.close()

    yield TestClient(app), ids
    app.dependency_overrides.clear()


def test_warranty_typed_in_by_hand(client):
    c, (receipt_id, item_id) = client
    r = c.patch(f"/api/receipts/{receipt_id}/items/{item_id}", json={
        "has_warranty": True,
        "warranty_text": "24 міс.",
        "warranty_expires_at": "2028-09-01T09:00:00+00:00",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_warranty"] is True
    assert body["warranty_text"] == "24 міс."
    # Typing it in counts as a check — nothing else sets this without an adapter.
    assert body["warranty_checked_at"] is not None

    listed = c.get("/api/warranties").json()
    assert [p["name"] for p in listed] == ["Дриль"]


def test_photo_attaches_to_one_item_and_flags_the_warranty(client):
    c, (receipt_id, item_id) = client
    r = c.post(
        f"/api/receipts/{receipt_id}/items/{item_id}/files?kind=warranty",
        files={"file": ("talon.jpg", b"jpeg-bytes", "image/jpeg")},
    )
    assert r.status_code == 201, r.text
    files = r.json()["files"]
    assert len(files) == 1 and files[0]["kind"] == "warranty"
    # A talon attached by hand is itself proof the line has a warranty.
    assert r.json()["has_warranty"] is True

    file_url = files[0]["url"]
    assert c.get(file_url).content == b"jpeg-bytes"

    # Attachments add up (a talon can be several pages), unlike the receipt photo.
    c.post(f"/api/receipts/{receipt_id}/items/{item_id}/files?kind=photo",
           files={"file": ("product.png", b"png-bytes", "image/png")})
    receipt = c.get(f"/api/receipts/{receipt_id}").json()
    assert len(receipt["items"][0]["files"]) == 2

    # The warranty card shows the hand-taken photo instead of a catalogue image.
    assert c.get("/api/warranties").json()[0]["image_url"] == file_url

    assert len(c.delete(file_url).json()["files"]) == 1
    assert c.get(file_url).status_code == 404


def test_rejects_a_pdf_on_the_item_route(client):
    c, (receipt_id, item_id) = client
    r = c.post(f"/api/receipts/{receipt_id}/items/{item_id}/files",
               files={"file": ("talon.pdf", b"%PDF-", "application/pdf")})
    assert r.status_code == 400


def test_link_is_downloaded_into_our_own_copy(client, monkeypatch):
    c, (receipt_id, item_id) = client
    png = b"\x89PNG\r\n\x1a\n" + b"rest"

    async def fake_download(url):
        assert url == "https://cdn.example/photo.png"
        return png, ".png", "image/png"

    monkeypatch.setattr(receipts_api, "download_image", fake_download)
    r = c.post(f"/api/receipts/{receipt_id}/items/{item_id}/files/link",
               json={"url": "https://cdn.example/photo.png", "kind": "photo"})
    assert r.status_code == 201, r.text
    file = r.json()["files"][0]
    # The link is provenance only — the bytes are served from our own storage.
    assert file["source_url"] == "https://cdn.example/photo.png"
    assert file["url"].startswith(f"/api/receipts/{receipt_id}/items/{item_id}/files/")
    assert c.get(file["url"]).content == png


def test_link_that_is_not_an_image_is_reported(client, monkeypatch):
    c, (receipt_id, item_id) = client

    async def fake_download(url):
        raise receipts_api.ImageDownloadError("За посиланням не зображення")

    monkeypatch.setattr(receipts_api, "download_image", fake_download)
    r = c.post(f"/api/receipts/{receipt_id}/items/{item_id}/files/link",
               json={"url": "https://example.com/page.html"})
    assert r.status_code == 400
    assert "зображення" in r.json()["detail"]
