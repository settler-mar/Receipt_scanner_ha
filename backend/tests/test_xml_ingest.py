"""The shared ДПС XML entry point: does this file belong here, and what is it?

Two questions the import used to answer nowhere. A file dropped on a scan row
was filed onto it whatever receipt it described, and a file uploaded with no row
at all produced a receipt that the inbox never mentioned.
"""
from __future__ import annotations

import datetime as dt
import pathlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app
from app.models import models
from app.models import scanner as sc

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
RQ = (FIXTURES / "fiscal_check_ukrnafta.xml").read_bytes()      # fiscal:3000878603:20960071
CABINET = (FIXTURES / "cabinet_check_semerog.xml").read_bytes()  # no document number
# A ПРРО that keeps the ДПС document id in MAC/@ID and only the shift's running
# number in E/@NO — two receipts of that till can share the latter.
MAC_ID = (FIXTURES / "fiscal_check_mac_id.xml").read_bytes()
MAC_ID_URL = ("https://cabinet.tax.gov.ua/cashregs/check"
              "?date=20260921&id=synthetic-doc-id&sm=424.50&time=182902&fn=4000000001")

RQ_URL = ("https://cabinet.tax.gov.ua/cashregs/check"
          "?date=20260705&id=20960071&sm=1428.73&time=083943&fn=3000878603")
OTHER_URL = ("https://cabinet.tax.gov.ua/cashregs/check"
             "?date=20260705&id=11112222&sm=99.00&time=101010&fn=4000111222")
# Some ПРРО put an opaque document token in `id` instead of the fiscal document
# number the XML itself carries — the same receipt, two ids that never match.
TOKEN_URL = ("https://cabinet.tax.gov.ua/cashregs/check"
             "?date=20260705&id=BZ0nkpp_COs&sm=1428.73&time=083943&fn=3000878603")


@pytest.fixture()
def client(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'x.db'}",
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


def _db():
    return next(app.dependency_overrides[get_db]())


def _scan_row(**raw_ocr) -> str:
    db = _db()
    job = sc.ScanJob(source_filename="photo.jpg", status=sc.ScanStatus.needs_review,
                     method="generic", raw_ocr=raw_ocr)
    db.add(job)
    db.commit()
    return job.id


def _upload(client, data=RQ, name="check.xml", **form):
    return client.post("/api/fiscal-queue/import-response-file",
                       files={"file": (name, data, "text/xml")}, data=form)


def _scans(client):
    return client.get("/api/inbox/scans").json()


# --------------------------------------------------------------------------- #
# The file has to be the one this row is waiting for
# --------------------------------------------------------------------------- #
def test_xml_of_another_receipt_is_refused_on_a_scan_row(client):
    """Every download from the cabinet is called `check.xml`, so picking the
    wrong one is the ordinary accident — and it used to be filed silently."""
    job_id = _scan_row(fiscal_url=OTHER_URL)

    response = _upload(client, scan_job_id=job_id)
    assert response.status_code == 409, response.text
    # The message says which receipt the file is and which one the row wants.
    detail = response.json()["detail"]
    assert "3000878603:20960071" in detail and "4000111222:11112222" in detail

    # Nothing was written: not the receipt, not the row.
    assert client.get("/api/receipts").json()["total"] == 0
    assert _scans(client)[0]["receipt_id"] is None
    # The refusal is in the row's own history, where the user looks for it.
    events = _scans(client)[0]["raw_ocr"]["events"]
    assert any(e["outcome"] == "fail" and "іншого чека" in e["detail"] for e in events)


def test_the_matching_xml_is_accepted_on_that_row(client):
    job_id = _scan_row(fiscal_url=RQ_URL)

    body = _upload(client, scan_job_id=job_id).json()
    assert body["receipt_id"]
    assert body["fiscal_key"] == "fiscal:3000878603:20960071"

    row = _scans(client)[0]
    assert row["id"] == job_id and row["status"] == "parsed"
    assert row["receipt_id"] == body["receipt_id"]


def test_a_bad_reading_of_the_same_paper_is_not_treated_as_a_wrong_file(client):
    """Importing an XML is how a poor scan gets fixed, so a disagreement one
    misread receipt can produce on its own must never block the import."""
    job_id = _scan_row(draft={"total_amount": 143.00,           # a misread total
                              "purchased_at": "2026-07-05T05:39:43+00:00"})

    assert _upload(client, scan_job_id=job_id).status_code == 200
    assert _scans(client)[0]["total_amount"] == 1428.73


def test_a_row_whose_qr_was_never_read_still_takes_the_file(client):
    """The rows most in need of an XML are the ones nothing could be read from,
    so an empty row has nothing to contradict the file with."""
    job_id = _scan_row()

    body = _upload(client, scan_job_id=job_id).json()
    assert body["scan_job_id"] == job_id and body["receipt_id"]
    assert _scans(client)[0]["status"] == "parsed"


def test_an_opaque_document_token_is_not_read_as_a_different_receipt(client):
    """The row's link says `id=BZ0nkpp_COs`, the document says 5641 — the ids
    can never be equal, and comparing them refused the right file for the right
    row. Money to the kopeck at the same minute settles it instead."""
    job_id = _scan_row(fiscal_url=TOKEN_URL)

    response = _upload(client, scan_job_id=job_id)
    assert response.status_code == 200, response.text
    assert response.json()["scan_job_id"] == job_id
    assert _scans(client)[0]["status"] == "parsed"


def test_the_queue_keeps_the_link_that_was_actually_scanned(client):
    """Queueing the rebuilt link instead would leave a second row open for a
    receipt that is already in the database."""
    job_id = _scan_row(fiscal_url=TOKEN_URL)

    body = _upload(client, scan_job_id=job_id).json()
    rows = client.get("/api/fiscal-queue").json()
    assert len(rows) == 1
    assert rows[0]["url"] == TOKEN_URL and rows[0]["receipt_id"] == body["receipt_id"]
    assert client.get("/api/fiscal-queue?open_only=true").json() == []


def test_a_token_link_for_a_different_purchase_is_still_refused(client):
    """The relaxed rule is money *and* minute together — it must not become
    "anything from the same РРО goes"."""
    job_id = _scan_row(fiscal_url="https://cabinet.tax.gov.ua/cashregs/check"
                                  "?date=20260708&id=BZ0nkpp_COs&sm=99.00&time=101010"
                                  "&fn=3000878603")

    assert _upload(client, scan_job_id=job_id).status_code == 409


def test_xml_of_another_receipt_is_refused_on_a_queue_row(client):
    """The ДПС queue has its own per-row upload button, and its row names the
    document outright — there is no excuse for accepting someone else's."""
    queue = client.post("/api/fiscal-queue", json={"url": OTHER_URL}).json()

    response = _upload(client, fiscal_job_id=queue["id"])
    assert response.status_code == 409, response.text
    assert client.get("/api/receipts").json()["total"] == 0


# --------------------------------------------------------------------------- #
# One entry point: an arbitrary XML finds its own place
# --------------------------------------------------------------------------- #
def test_an_unknown_receipt_gets_its_own_inbox_row(client):
    body = _upload(client, name="Чек.xml").json()

    assert body["outcome"] == "created" and body["duplicate"] is False
    assert body["scan_job_created"] is True
    assert body["total_amount"] == 1428.73

    rows = _scans(client)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == body["scan_job_id"]
    assert row["status"] == "parsed" and row["receipt_id"] == body["receipt_id"]
    assert row["source_filename"] == "Чек.xml"
    # The file is in the row's history, so "where did this receipt come from"
    # has an answer without the photo a scan would have left.
    assert [e["kind"] for e in row["raw_ocr"]["events"]][:1] == ["upload"]
    assert row["raw_ocr"]["fiscal_url"] == body["url"]


def test_the_queue_row_for_that_receipt_is_closed_by_the_import(client):
    client.post("/api/fiscal-queue", json={"url": RQ_URL})
    body = _upload(client).json()

    assert body["status"] == "imported"
    assert client.get("/api/fiscal-queue?open_only=true").json() == []
    assert client.get("/api/fiscal-queue").json()[0]["receipt_id"] == body["receipt_id"]


def test_the_file_lands_on_the_scan_that_was_waiting_for_it(client):
    """Uploaded from the free import box, with no row named — the photo of this
    same receipt is still the row it belongs on, not a second entry beside it."""
    job_id = _scan_row(fiscal_url=RQ_URL)

    body = _upload(client).json()
    assert body["scan_job_id"] == job_id and body["scan_job_created"] is False
    assert len(_scans(client)) == 1


def test_a_half_read_qr_still_names_the_row_the_file_belongs_to(client):
    """A QR that decoded without the document number (or an `<RQ>` that carries
    none) leaves nothing to compare ids with — so the photo row and the XML used
    to end up as two rows for one purchase. Money to the kopeck within the same
    minute is the same receipt."""
    job_id = _scan_row(fiscal_url="https://cabinet.tax.gov.ua/cashregs/check"
                                  "?date=20260705&time=083950&sm=1428.73&fn=3000878603")

    body = _upload(client).json()
    assert body["scan_job_id"] == job_id and body["scan_job_created"] is False
    assert len(_scans(client)) == 1


def test_a_different_purchase_of_the_same_minute_keeps_its_own_row(client):
    """The fallback is money *and* the minute together: a different amount at
    the same till is a different receipt and must not swallow the row."""
    job_id = _scan_row(fiscal_url="https://cabinet.tax.gov.ua/cashregs/check"
                                  "?date=20260705&time=083950&sm=99.00&fn=3000878603")

    body = _upload(client).json()
    assert body["scan_job_id"] != job_id and body["scan_job_created"] is True
    assert len(_scans(client)) == 2


def test_a_row_whose_receipt_was_deleted_counts_as_waiting_again(client):
    """`ScanJob.receipt_id` has no foreign key, so a receipt deleted by hand
    leaves the id dangling. Such a row shows as unresolved in the journal, and
    the file the user uploads for it has to land there — not beside it."""
    job_id = _scan_row(fiscal_url=TOKEN_URL)
    db = _db()
    job = db.get(sc.ScanJob, job_id)
    job.receipt_id = "receipt-that-no-longer-exists"
    db.commit()

    body = _upload(client).json()
    assert body["scan_job_id"] == job_id and body["scan_job_created"] is False
    assert len(_scans(client)) == 1


def test_the_message_says_what_arrived_and_where_it_went(client):
    """"Чек уже був у базі" alone leaves the user hunting for the row that
    changed — which is exactly the report this text answers."""
    first = _upload(client).json()
    assert "1428.73 UAH" in first["message"]
    assert "створено рядок" in first["message"]

    again = _upload(client).json()
    assert "уже був у базі" in again["message"]
    assert "наявного рядка" in again["message"]


def test_the_same_file_twice_stays_one_receipt_and_one_row(client):
    first = _upload(client).json()
    second = _upload(client, name="check (1).xml").json()

    assert second["receipt_id"] == first["receipt_id"]
    assert second["outcome"] == "merged" and second["duplicate"] is True
    assert second["scan_job_id"] == first["scan_job_id"]
    assert client.get("/api/receipts").json()["total"] == 1
    assert len(_scans(client)) == 1


def test_a_cabinet_check_without_a_document_number_also_opens_a_row(client):
    """`<CHECK>` names no fiscal document, so it never reaches the queue — it
    still has to be visible as something that arrived."""
    body = _upload(client, data=CABINET, name="cabinet.xml").json()

    assert body["id"] is None and body["receipt_id"]
    assert body["outcome"] == "created"
    assert _scans(client)[0]["method"] == "cabinet_check_xml"


def test_an_unreadable_file_is_rejected_without_touching_anything(client):
    response = _upload(client, data=b"<nonsense/>")
    assert response.status_code == 400
    assert client.get("/api/receipts").json()["total"] == 0
    assert _scans(client) == []


# --------------------------------------------------------------------------- #
# The document id the ДПС link actually uses
# --------------------------------------------------------------------------- #
def test_the_file_meets_the_photo_of_that_very_receipt(client):
    """The QR link and the XML now name the document the same way, so the file
    lands on the scan it belongs to instead of opening a row of its own."""
    job_id = _scan_row(fiscal_url=MAC_ID_URL)

    body = _upload(client, data=MAC_ID, name="bWmxvOu915U.xml").json()
    assert body["fiscal_key"] == "fiscal:4000000001:synthetic-doc-id"
    assert body["scan_job_id"] == job_id and body["scan_job_created"] is False
    assert _scans(client)[0]["total_amount"] == 424.50


def test_two_receipts_of_one_till_are_not_merged_by_their_shift_number(client):
    """E/@NO repeats across receipts of the same till. Treating it as the
    document id made the second import merge into the first — which is what the
    user saw: a receipt showing another receipt's totals."""
    from app.adapters.base import NormalizedItem, NormalizedReceipt
    from app.api.inbox import _persist_receipt, _scanner_account

    db = _db()
    earlier = _persist_receipt(db, _scanner_account(db), NormalizedReceipt(
        # The same till, the same shift number, a different purchase.
        external_id="fiscal:4000000001:5641",
        purchased_at=dt.datetime(2026, 9, 21, 15, 32, 11, tzinfo=dt.timezone.utc),
        total_amount=593.00, shop_name="Аптека",
        items=[NormalizedItem("Інший чек", 593.00)]))

    body = _upload(client, data=MAC_ID, name="bWmxvOu915U.xml").json()
    assert body["receipt_id"] != earlier.id
    assert body["outcome"] == "created"
    assert client.get("/api/receipts").json()["total"] == 2
    assert client.get(f"/api/receipts/{earlier.id}").json()["total_amount"] == 593.00
