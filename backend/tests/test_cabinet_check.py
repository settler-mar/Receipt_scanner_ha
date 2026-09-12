"""The `<CHECK>` XML the ДПС cabinet offers for download on a receipt page.

A third ДПС shape next to the ПРРО `<RQ>` document and the `<Chk>` API envelope
(see test_fiscal_check.py) — already decoded, windows-1251, and carrying no
fiscal document number.
"""
from __future__ import annotations

import datetime as dt
import pathlib

from app.scanner.cabinet_check import parse_cabinet_check_xml
from app.scanner.reconcile import reconcile

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "cabinet_check_semerog.xml"


def _parsed():
    return parse_cabinet_check_xml(FIXTURE.read_bytes())


def test_cabinet_check_xml_is_read_in_full():
    receipt = _parsed()
    assert receipt is not None

    # ORDERDATE/ORDERTIME are store-local (Kyiv): 16:53:27 → 13:53:27 UTC.
    assert receipt.purchased_at == dt.datetime(2026, 9, 6, 13, 53, 27, tzinfo=dt.timezone.utc)
    assert receipt.total_amount == 633.50
    assert receipt.shop_name == "Магазин ФОП СЕМЕРОГ ІННА ПЕТРІВНА"
    assert "БОЯРСЬКА" in receipt.shop_address
    # PAYFORMCD is not portable between ПРРО vendors, so the name decides.
    assert receipt.payment_type == "card"
    assert receipt.raw["seller_tax_number"] == "3074818469"
    assert receipt.raw["fiscal_number"] == "4001319262"

    assert [i.name for i in receipt.items] == [
        "Сік томатний 3л", "Сік яблучний 3л", "Малина",
    ]
    weighed = receipt.items[-1]
    # Weighed goods carry three decimals — money rounding would make this 0.55.
    assert weighed.quantity == 0.554
    assert weighed.unit_price == 250.00
    assert weighed.total_price == 138.50

    assert reconcile(receipt).ok


def test_document_uid_keeps_reimports_idempotent():
    """The format has no fiscal check number, but its UID is stable per receipt,
    so importing the same file twice must not mean two receipts."""
    first, second = _parsed(), _parsed()
    assert first.external_id == second.external_id
    assert first.external_id == "cabinet-check:a6008d71-b568-4a2b-8ce7-5ad28f8fd01d"


def test_other_xml_shapes_are_declined_rather_than_misread():
    """Returning None lets the caller go on to try the <RQ>/<Chk> parsers."""
    rq = (pathlib.Path(__file__).parent / "fixtures" / "fiscal_check_ukrnafta.xml").read_bytes()
    assert parse_cabinet_check_xml(rq) is None
    assert parse_cabinet_check_xml(b"<html>not xml at all</html>") is None
    assert parse_cabinet_check_xml("<CHECK></CHECK>") is None  # no date, no total


# --------------------------------------------------------------------------- #
# Import through the API
# --------------------------------------------------------------------------- #
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import models  # noqa: E402
from app.models import scanner as scanner_models  # noqa: F401,E402


@pytest.fixture()
def client(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'q.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with session() as s:
        s.add(models.Store(code="scanner", name="Сканер", auth_type=models.AuthType.manual))
        s.commit()

    def _override():
        db = session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override
    yield TestClient(app)
    app.dependency_overrides.clear()


def _upload(client, name="check.xml"):
    return client.post("/api/fiscal-queue/import-response-file",
                       files={"file": (name, FIXTURE.read_bytes(), "text/xml")})


def test_uploading_the_xml_creates_the_receipt(client):
    """This document names no fiscal check number, so there is no queue job to
    attach it to — it is already the answer the queue exists to fetch."""
    response = _upload(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "imported"
    assert body["id"] is None

    receipt = client.get(f"/api/receipts/{body['receipt_id']}").json()
    assert receipt["total_amount"] == 633.50
    assert receipt["payment_type"] == "card"
    assert len(receipt["items"]) == 3


def test_upload_shows_the_receipt_on_the_scan_row_it_came_from(client):
    """Without this the row stays on `needs_review` with no link, so the user is
    told the receipt was saved while the screen still shows the photo as
    unresolved — and a re-import looks like nothing happened."""
    from app.models import scanner as sc

    db = next(app.dependency_overrides[get_db]())
    job = sc.ScanJob(source_filename="photo.jpg", status=sc.ScanStatus.needs_review,
                     method="generic", raw_ocr={"fiscal_url": "https://cabinet.tax.gov.ua/x"})
    db.add(job)
    db.commit()

    response = client.post("/api/fiscal-queue/import-response-file",
                           files={"file": ("check.xml", FIXTURE.read_bytes(), "text/xml")},
                           data={"scan_job_id": job.id})
    assert response.status_code == 200, response.text
    receipt_id = response.json()["receipt_id"]

    row = next(r for r in client.get("/api/inbox/scans").json() if r["id"] == job.id)
    assert row["receipt_id"] == receipt_id
    assert row["status"] == "parsed"
    assert row["method"] == "cabinet_check_xml"
    assert row["total_amount"] == 633.50


def test_upload_without_a_scan_job_still_imports(client):
    """The same endpoint is used from places that have no inbox row."""
    body = client.post("/api/fiscal-queue/import-response-file",
                       files={"file": ("check.xml", FIXTURE.read_bytes(), "text/xml")}).json()
    assert body["receipt_id"]


def test_uploading_the_same_xml_twice_keeps_one_receipt(client):
    first = _upload(client).json()["receipt_id"]
    assert _upload(client, "downloaded (1).xml").json()["receipt_id"] == first
    assert client.get("/api/receipts").json()["total"] == 1


def test_xml_merges_into_the_same_receipt_scanned_from_its_qr(client):
    """The QR gives `fiscal:<fn>:<id>`, which this format cannot produce — so the
    two copies of one receipt meet through manual_dedup instead, on the total and
    the printed second, and the fuller XML wins."""
    from app.adapters.base import NormalizedItem, NormalizedReceipt
    from app.api.inbox import _persist_receipt, _scanner_account

    db = next(app.dependency_overrides[get_db]())
    from_qr = _persist_receipt(db, _scanner_account(db), NormalizedReceipt(
        external_id="fiscal:4001319262:7738620256",
        purchased_at=dt.datetime(2026, 9, 6, 13, 53, 27, tzinfo=dt.timezone.utc),
        total_amount=633.50, shop_name=None,
        items=[NormalizedItem("Чек за QR", 633.50)]))

    assert _upload(client).json()["receipt_id"] == from_qr.id
    assert client.get("/api/receipts").json()["total"] == 1

    merged = client.get(f"/api/receipts/{from_qr.id}").json()
    assert [i["name"] for i in merged["items"]] == [
        "Сік томатний 3л", "Сік яблучний 3л", "Малина"]
    assert merged["shop_name"] == "Магазин ФОП СЕМЕРОГ ІННА ПЕТРІВНА"
