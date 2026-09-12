"""Upload routing + document inbox API (ROADMAP.md Крок 3)."""
from __future__ import annotations

import datetime as dt
import io
import pathlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app
from app.models import models  # noqa: F401
from app.models import scanner as scanner_models  # noqa: F401


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # keep uploaded document bytes, scan inputs and receipt photos in the tmp dir
    monkeypatch.setattr("app.services.inbox.INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr("app.api.inbox.SCAN_INPUT_DIR", tmp_path / "scan_inputs")
    monkeypatch.setattr("app.api.receipts.PHOTO_DIR", tmp_path / "photos")

    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    # a Store row for 'scanner' so the receipt branch can attach an Account
    with TestingSession() as s:
        s.add(models.Store(code="scanner", name="Сканер", auth_type=models.AuthType.manual))
        s.commit()

    def _override():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()


def _png(w: int, h: int) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, format="PNG")
    return buf.getvalue()


def _color_png(w: int, h: int) -> bytes:
    """A saturated image — routing's colour signal treats this as a document."""
    import numpy as np
    from PIL import Image

    rng = np.random.default_rng(0)
    arr = rng.integers(0, 255, size=(h, w, 3), dtype=np.uint8)
    arr[:, :, 0] = 220  # push saturation up
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


def test_wide_image_routes_to_document_inbox(client):
    r = client.post("/api/inbox/upload",
                    files={"file": ("scan.png", _color_png(1600, 1000), "image/png")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "document"
    assert body["storage"] == "local"
    doc_id = body["document_id"]

    listing = client.get("/api/inbox").json()
    assert [d["id"] for d in listing] == [doc_id]

    # process → renamed + closed
    p = client.post(f"/api/inbox/{doc_id}/process",
                    json={"final_name": "Договір оренди 2026"})
    assert p.status_code == 200, p.text
    assert p.json()["status"] == "done"
    assert client.get("/api/inbox").json() == []


def test_monochrome_wide_image_still_routes_to_receipt(client):
    # a grey thermal receipt photographed crumpled/short: not tall, but monochrome
    r = client.post("/api/inbox/upload",
                    files={"file": ("scan.png", _png(1600, 1000), "image/png")})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "receipt"


def test_tall_image_enters_receipt_pipeline(client):
    # blank tall image: no QR, no local OCR binary, no AI key → nothing usable
    r = client.post("/api/inbox/upload",
                    files={"file": ("receipt.png", _png(400, 1400), "image/png")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "receipt"
    assert body["scan_job_id"]
    assert body["confidence"] in ("none", "low")

    scans = client.get("/api/inbox/scans").json()
    assert len(scans) == 1
    stats = client.get("/api/inbox/scans/stats").json()
    assert stats["total"] == 1


def _scan_job(client) -> str:
    r = client.post("/api/inbox/upload",
                    files={"file": ("receipt.png", _png(400, 1400), "image/png")})
    assert r.status_code == 200, r.text
    return r.json()["scan_job_id"]


def test_scan_is_deleted_together_with_its_stored_image(client, tmp_path):
    job_id = _scan_job(client)
    stored = tmp_path / "scan_inputs" / job_id
    assert stored.is_file()

    assert client.delete(f"/api/inbox/scans/{job_id}").status_code == 204
    assert client.get("/api/inbox/scans").json() == []
    assert not stored.exists()
    assert client.delete(f"/api/inbox/scans/{job_id}").status_code == 404


def test_rescan_replaces_the_old_job_instead_of_piling_up(client):
    job_id = _scan_job(client)

    r = client.post(f"/api/inbox/scans/{job_id}/rescan")
    assert r.status_code == 200, r.text
    new_id = r.json()["scan_job_id"]
    assert new_id != job_id

    scans = client.get("/api/inbox/scans").json()
    assert [j["id"] for j in scans] == [new_id]


def test_rescan_carries_forward_a_qr_an_earlier_pass_decoded(client):
    """Pressing «Пересканувати» retries the QR first. Decoding is the fragile
    step, so a URL already recovered from this photo is handed back into the
    rerun — it can only start from the best QR any attempt produced."""
    from app.db import get_db
    from app.models import scanner as sc

    job_id = _scan_job(client)  # blank image: nothing decodes from it
    url = ("https://cabinet.tax.gov.ua/cashregs/check"
           "?id=7738620256&date=20260906&time=165327&fn=4001319262&sm=633.50")
    db = next(app.dependency_overrides[get_db]())
    job = db.get(sc.ScanJob, job_id)
    job.raw_ocr = {**(job.raw_ocr or {}), "fiscal_url": url}
    db.commit()

    assert client.post(f"/api/inbox/scans/{job_id}/rescan").status_code == 200
    row = client.get("/api/inbox/scans").json()[0]
    assert row["raw_ocr"]["fiscal_url"] == url
    assert "reusing a previously decoded fiscal QR" in " ".join(row["raw_ocr"]["notes"])


def test_ai_rescan_skips_local_ocr_and_templates(client):
    job_id = _scan_job(client)
    r = client.post(f"/api/inbox/scans/{job_id}/rescan?ai=true")
    assert r.status_code == 200, r.text

    scans = client.get("/api/inbox/scans").json()
    assert "ai_first: local OCR/templates skipped" in scans[0]["raw_ocr"]["notes"]


def test_rescan_of_a_missing_job_is_a_404(client):
    assert client.post("/api/inbox/scans/nope/rescan").status_code == 404


def _parsed_job(client) -> tuple[str, str]:
    """A scan job that already produced a receipt, as a QR-only receipt does."""
    from app.api.inbox import _scanner_account
    from app.db import get_db
    from app.models import scanner as sc

    job_id = _scan_job(client)
    db = next(app.dependency_overrides[get_db]())
    account = _scanner_account(db)
    receipt = models.Receipt(account_id=account.id, store_code="scanner",
                             external_id="old", total_amount=250,
                             purchased_at=dt.datetime(2026, 9, 6, tzinfo=dt.timezone.utc))
    db.add(receipt)
    db.commit()
    job = db.get(sc.ScanJob, job_id)
    job.receipt_id, job.status = receipt.id, sc.ScanStatus.parsed
    db.commit()
    return job_id, receipt.id


def test_rescanning_a_saved_receipt_needs_an_explicit_replace(client):
    """A parse can look complete and still be wrong, so the rerun stays
    available — but it rewrites saved data, so it is never implicit."""
    job_id, receipt_id = _parsed_job(client)

    refused = client.post(f"/api/inbox/scans/{job_id}/rescan")
    assert refused.status_code == 409
    assert "підтвердіть заміну" in refused.json()["detail"]

    allowed = client.post(f"/api/inbox/scans/{job_id}/rescan?replace=true")
    assert allowed.status_code == 200, allowed.text


def test_deleting_a_scan_takes_its_receipt_with_it(client):
    """Clearing a bad scan out of the journal is pointless while the wrong
    receipt it created stays in the reports."""
    from app.db import get_db

    job_id, receipt_id = _parsed_job(client)

    refused = client.delete(f"/api/inbox/scans/{job_id}")
    assert refused.status_code == 409
    assert "підтвердіть видалення" in refused.json()["detail"]

    assert client.delete(f"/api/inbox/scans/{job_id}?with_receipt=true").status_code == 204
    assert client.get("/api/inbox/scans").json() == []
    db = next(app.dependency_overrides[get_db]())
    assert db.get(models.Receipt, receipt_id) is None


def test_scan_journal_reports_shop_and_total(client):
    """The journal is read to spot duplicates, which needs the two fields that
    tell one purchase from another without opening every row."""
    job_id, receipt_id = _parsed_job(client)
    from app.db import get_db

    db = next(app.dependency_overrides[get_db]())
    receipt = db.get(models.Receipt, receipt_id)
    receipt.shop_name = "ФОП СЕМЕРОГ"
    db.commit()

    row = client.get("/api/inbox/scans").json()[0]
    assert row["shop_name"] == "ФОП СЕМЕРОГ"
    assert row["total_amount"] == 250.0
    assert row["currency"] == "UAH"
    # When the purchase happened, not when the photo was uploaded.
    assert row["purchased_at"].startswith("2026-09-06T00:00:00")
    assert row["created_at"] != row["purchased_at"]


def test_journal_reads_the_purchase_date_off_an_unsaved_draft(client):
    """A row awaiting review has no receipt yet, so its date comes from the
    draft — otherwise the column would be empty for exactly the rows being
    compared against each other for duplicates."""
    from app.db import get_db
    from app.models import scanner as sc

    job_id = _scan_job(client)
    db = next(app.dependency_overrides[get_db]())
    job = db.get(sc.ScanJob, job_id)
    job.raw_ocr = {**(job.raw_ocr or {}), "draft": {
        "shop_name": "ФОП СЕМЕРОГ", "total_amount": 633.5,
        "purchased_at": "2026-09-06T13:53:27+00:00", "items": [],
    }}
    db.commit()

    row = client.get("/api/inbox/scans").json()[0]
    assert row["purchased_at"].startswith("2026-09-06T13:53:27")
    assert row["total_amount"] == 633.5
    assert row["shop_name"] == "ФОП СЕМЕРОГ"


def test_a_hand_deleted_receipt_is_not_reported_as_still_there(client):
    """`ScanJob.receipt_id` has no foreign key, so deleting a receipt from its
    own page leaves the journal pointing at nothing. The row must not keep
    offering a dead link — or guarding actions on a receipt that is gone."""
    from app.db import get_db

    job_id, receipt_id = _parsed_job(client)
    assert client.delete(f"/api/receipts/{receipt_id}").status_code == 204

    row = client.get("/api/inbox/scans").json()[0]
    assert row["receipt_id"] is None
    assert row["receipt_deleted"] is True

    # …and the "this scan has a receipt" guards stand down accordingly.
    assert client.post(f"/api/inbox/scans/{job_id}/rescan").status_code == 200
    db = next(app.dependency_overrides[get_db]())
    assert db.query(models.Receipt).count() == 0


def _job_with_draft(client) -> str:
    """A needs_review scan — the state the user is asked to review."""
    from app.db import get_db
    from app.models import scanner as sc

    job_id = _scan_job(client)
    db = next(app.dependency_overrides[get_db]())
    job = db.get(sc.ScanJob, job_id)
    job.status = sc.ScanStatus.needs_review
    job.method = "generic"
    job.raw_ocr = {**(job.raw_ocr or {}), "draft": {
        "shop_name": "ФОП СЕМЕРОГ",
        "purchased_at": "2026-09-06T13:53:27+00:00",
        "total_amount": 633.5,
        "items": [
            {"name": "Сік томатний 3л", "quantity": 1, "unit_price": 270.0, "total_price": 270.0},
            {"name": "Сік яблучний 3л", "quantity": 1, "unit_price": 225.0, "total_price": 225.0},
            {"name": "Малина", "quantity": 0.554, "unit_price": 250.0, "total_price": 138.5},
        ],
    }}
    db.commit()
    return job_id


def test_draft_conversion_fills_in_what_the_scan_left_out():
    """Shared by the web review button and the Telegram one, so a half-parsed
    draft must not blow up on missing quantity, date or total."""
    from app.services.scan_jobs import normalized_from_draft

    nr = normalized_from_draft({
        "shop_name": "АТБ",
        "purchased_at": "2026-09-01T10:00:00+00:00",
        "total_amount": 123.45,
        "total_discount": 5,
        "items": [
            {"name": "Хліб", "quantity": 2, "unit_price": 10, "total_price": 20, "discount": 0},
            {"name": "Молоко", "total_price": 40},
        ],
    }, "job-7")
    assert nr.external_id == "scan-review:job-7"
    assert nr.shop_name == "АТБ"
    assert nr.total_amount == 123.45
    assert nr.total_discount == 5
    assert [i.name for i in nr.items] == ["Хліб", "Молоко"]
    assert nr.items[1].quantity == 1  # defaulted

    bare = normalized_from_draft({"items": [{"name": "X", "total_price": 7}]}, "j")
    assert bare.purchased_at is not None  # falls back to now
    assert bare.total_amount == 7  # summed from the items


def test_a_draft_can_be_accepted_as_a_receipt(client):
    """`needs_review` told the user something was wrong but offered no way to
    act on it. Accepting the draft turns it into a receipt they can then edit."""
    job_id = _job_with_draft(client)

    body = client.post(f"/api/inbox/scans/{job_id}/save-draft").json()
    assert body["receipt_id"]
    assert body["status"] == "parsed"
    assert "вручну" in body["method"]

    receipt = client.get(f"/api/receipts/{body['receipt_id']}").json()
    assert receipt["total_amount"] == 633.5
    assert receipt["shop_name"] == "ФОП СЕМЕРОГ"
    assert [i["name"] for i in receipt["items"]] == [
        "Сік томатний 3л", "Сік яблучний 3л", "Малина"]
    assert receipt["items"][2]["quantity"] == 0.554


def test_accepting_the_same_draft_twice_is_refused(client):
    job_id = _job_with_draft(client)
    assert client.post(f"/api/inbox/scans/{job_id}/save-draft").status_code == 200

    again = client.post(f"/api/inbox/scans/{job_id}/save-draft")
    assert again.status_code == 409
    assert "уже збережено" in again.json()["detail"]
    assert client.get("/api/receipts").json()["total"] == 1


def test_accepting_a_draft_that_does_not_exist(client):
    job_id = _scan_job(client)  # failed scan, no draft behind it
    refused = client.post(f"/api/inbox/scans/{job_id}/save-draft")
    assert refused.status_code == 409
    assert "немає чернетки" in refused.json()["detail"]


def test_linking_a_scan_gives_the_receipt_the_photo_and_the_dps_link(client, tmp_path):
    """A receipt imported from XML never sees the photo, while the scan it
    belongs to holds both the decoded ДПС link and the image. Whichever lands
    second, the receipt must end up with everything both of them knew."""
    from app.db import get_db
    from app.models import scanner as sc
    from app.services import scan_jobs

    job_id = _job_with_draft(client)
    url = "https://cabinet.tax.gov.ua/cashregs/check?id=7738620256&fn=4001319262"
    db = next(app.dependency_overrides[get_db]())
    job = db.get(sc.ScanJob, job_id)
    job.raw_ocr = {**(job.raw_ocr or {}), "fiscal_url": url}
    db.commit()

    receipt_id = client.post(f"/api/inbox/scans/{job_id}/save-draft").json()["receipt_id"]
    receipt = client.get(f"/api/receipts/{receipt_id}").json()
    assert receipt["receipt_url"] == url
    assert receipt["photo_url"], "the scanned image should show as the receipt photo"
    # The scan input carries no content type, so the extension has to come from
    # the file's own signature — _scan_job uploads a PNG.
    assert (tmp_path / "photos" / f"{receipt_id}.png").is_file()

    # Nothing already on the receipt is overwritten by a later link.
    other = sc.ScanJob(source_filename="second.jpg", status=sc.ScanStatus.needs_review,
                       raw_ocr={"fiscal_url": "https://cabinet.tax.gov.ua/cashregs/check?id=9&fn=9"})
    db.add(other)
    db.commit()
    scan_jobs.attach_receipt(db, other.id, receipt_id, method="manual")
    assert client.get(f"/api/receipts/{receipt_id}").json()["receipt_url"] == url


def test_a_rescan_recognises_a_receipt_already_filed_from_its_qr(client, monkeypatch):
    """Uploading another photo of a receipt already on file should be settled by
    the ДПС document the QR names — not sent through review as if it were new."""
    from app.db import get_db
    from app.models import scanner as sc
    from app.scanner.fiscal_qr import QR_DECODED, QrScan

    url = ("https://cabinet.tax.gov.ua/cashregs/check"
           "?id=7738620256&date=20260906&time=165327&fn=4001319262&sm=633.50")

    # A receipt is already on file, filed by an earlier scan of that document.
    first = _job_with_draft(client)
    receipt_id = client.post(f"/api/inbox/scans/{first}/save-draft").json()["receipt_id"]
    db = next(app.dependency_overrides[get_db]())
    job = db.get(sc.ScanJob, first)
    job.raw_ocr = {**(job.raw_ocr or {}), "fiscal_url": url}
    db.commit()

    monkeypatch.setattr("app.scanner.pipeline.scan_qr", lambda _image: QrScan(QR_DECODED, [url]))
    body = client.post("/api/inbox/upload",
                       files={"file": ("again.png", _png(400, 1400), "image/png")}).json()

    assert body["receipt_id"] == receipt_id
    assert body["duplicate"] is True
    assert body["needs_review"] is False

    row = next(r for r in client.get("/api/inbox/scans").json() if r["id"] == body["scan_job_id"])
    assert row["status"] == "parsed"
    assert row["duplicate_of"] == first
    assert client.get("/api/receipts").json()["total"] == 1


def test_two_scans_of_one_purchase_are_shown_as_duplicates(client):
    """Both rows link to the same receipt; without saying so they read as two
    separate purchases that merely look alike."""
    from app.db import get_db
    from app.models import scanner as sc
    from app.services import scan_jobs

    first = _job_with_draft(client)
    receipt_id = client.post(f"/api/inbox/scans/{first}/save-draft").json()["receipt_id"]

    db = next(app.dependency_overrides[get_db]())
    row = sc.ScanJob(source_filename="again.jpg", status=sc.ScanStatus.needs_review)
    db.add(row)
    db.commit()
    second = row.id
    scan_jobs.attach_receipt(db, second, receipt_id, method="manual")

    rows = {r["id"]: r for r in client.get("/api/inbox/scans").json()}
    assert rows[first]["duplicate_of"] is None, "the original is not a duplicate of itself"
    assert rows[second]["duplicate_of"] == first
    assert rows[second]["receipt_id"] == receipt_id


def test_accepting_a_draft_of_an_already_filed_purchase_says_so(client):
    """Two photos of one receipt: the second draft merges, and the answer has to
    name the scan that filed it rather than claim a new receipt was made."""
    from app.db import get_db
    from app.models import scanner as sc

    first = _job_with_draft(client)
    receipt_id = client.post(f"/api/inbox/scans/{first}/save-draft").json()["receipt_id"]

    second = _job_with_draft(client)  # same shop, same second, same total
    db = next(app.dependency_overrides[get_db]())
    db.get(sc.ScanJob, second)  # keep the session warm for the assertions below

    body = client.post(f"/api/inbox/scans/{second}/save-draft").json()
    assert body["receipt_id"] == receipt_id
    assert body["duplicate_of"] == first
    assert client.get("/api/receipts").json()["total"] == 1


def test_archived_scans_leave_the_working_list(client):
    job_id = _scan_job(client)

    body = client.post(f"/api/inbox/scans/{job_id}/archive").json()
    assert body["archived_at"] is not None
    assert client.get("/api/inbox/scans").json() == []

    archived = client.get("/api/inbox/scans?include_archived=true").json()
    assert [j["id"] for j in archived] == [job_id]

    # Archiving keeps the record and its image, so it can come back and re-run.
    client.post(f"/api/inbox/scans/{job_id}/archive?archived=false")
    assert [j["id"] for j in client.get("/api/inbox/scans").json()] == [job_id]
    assert client.post(f"/api/inbox/scans/{job_id}/rescan").status_code == 200


def test_archiving_an_unknown_scan_is_a_404(client):
    assert client.post("/api/inbox/scans/nope/archive").status_code == 404


def test_a_rescan_that_finds_nothing_keeps_the_saved_receipt(client):
    """The blank test image parses to nothing. Losing a stored receipt to a
    rerun that came back empty would be strictly worse than doing nothing."""
    from app.db import get_db

    job_id, receipt_id = _parsed_job(client)
    body = client.post(f"/api/inbox/scans/{job_id}/rescan?replace=true").json()

    assert body["receipt_id"] is None
    assert body["kept_receipt_id"] == receipt_id
    assert body["replaced_receipt_id"] is None

    db = next(app.dependency_overrides[get_db]())
    assert db.get(models.Receipt, receipt_id) is not None


def test_scan_records_what_the_photos_qr_did(client):
    _scan_job(client)
    assert client.get("/api/inbox/scans").json()[0]["raw_ocr"]["qr_state"] == "none"


def test_recognised_receipt_keeps_the_scanned_photo(client, tmp_path):
    """A receipt built from a photo should display that photo, like a manually
    added one does — the scan-input copy isn't reachable from the receipt page."""
    from app.api.inbox import _scanner_account, attach_scan_photo
    from app.db import get_db

    db = next(app.dependency_overrides[get_db]())
    account = _scanner_account(db)
    receipt = models.Receipt(account_id=account.id, store_code="scanner", external_id="x",
                             total_amount=1,
                             purchased_at=dt.datetime(2026, 9, 6, tzinfo=dt.timezone.utc))
    db.add(receipt)
    db.commit()

    attach_scan_photo(db, receipt, b"jpeg-bytes", "image/jpeg")
    db.commit()
    assert receipt.photo_filename == f"{receipt.id}.jpg"
    assert (tmp_path / "photos" / receipt.photo_filename).read_bytes() == b"jpeg-bytes"

    # An existing photo is never clobbered by a later rescan.
    attach_scan_photo(db, receipt, b"other", "image/png")
    assert receipt.photo_filename == f"{receipt.id}.jpg"


def test_inbox_config_roundtrip(client):
    assert client.get("/api/inbox/config").json()["receipt_ratio"] == 2.0
    client.put("/api/inbox/config", json={"receipt_ratio": 1.6, "max_file_mb": 10})
    assert client.get("/api/inbox/config").json()["receipt_ratio"] == 1.6


def test_drive_endpoints_report_not_configured(client):
    assert client.get("/api/inbox/drive-tree").status_code == 400
    assert client.get("/api/inbox/drive-config").json()["connected"] is False


def test_drive_root_and_inbox_folder_selection_and_upload(client, monkeypatch):
    """The 'Google Drive · інбокс документів' block: connect (mocked), pick a
    root + inbox folder, then a wide upload lands in the inbox folder on Drive
    and processing moves it to a chosen folder."""
    import httpx

    from app.services import drive

    monkeypatch.setattr(drive, "is_connected", lambda db: True)

    async def _tok(db):
        return "access-token"

    monkeypatch.setattr(drive, "get_access_token", _tok)

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "GET" and request.url.path == "/drive/v3/files":
            parent = request.url.params["q"].split("'")[1]
            return httpx.Response(200, json={"files": [
                {"id": f"{parent}-child", "name": "Документи", "folderColorRgb": "#16A765"},
            ]})
        if request.method == "PATCH" and request.url.path == "/drive/v3/files/inbox-folder":
            seen["color_body"] = request.content.decode()
            return httpx.Response(200, json={"folderColorRgb": "#4986e7"})
        if "/upload/drive/v3/files" in url:
            # crude: capture which parent the multipart metadata names
            seen["upload_body"] = request.content.decode("utf-8", "ignore")
            return httpx.Response(200, json={"id": "uploaded-1"})
        if request.method == "GET" and request.url.path == "/drive/v3/files/uploaded-1":
            return httpx.Response(200, json={"parents": ["inbox-folder"]})
        if request.method == "PATCH" and request.url.path == "/drive/v3/files/uploaded-1":
            seen["moved_to"] = request.url.params.get("addParents")
            return httpx.Response(200, json={"id": "uploaded-1"})
        return httpx.Response(404, json={"error": url})

    monkeypatch.setattr(drive, "_http",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    # pick root + inbox folder (as the Settings UI does)
    r = client.put("/api/inbox/drive/folders", json={
        "root_folder_id": "root-folder", "root_folder_name": "Мої документи",
        "inbox_folder_id": "inbox-folder", "inbox_folder_name": "Необроблені",
    })
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["root_folder_name"] == "Мої документи"
    assert st["inbox_folder_name"] == "Необроблені"

    # folder browser starts under the root folder; folder colour comes through
    tree = client.get("/api/inbox/drive-tree", params={"parent": "root-folder"}).json()
    assert tree == [{"id": "root-folder-child", "name": "Документи", "color": "#16a765"}]

    # set a colour on the inbox folder → Drive PATCH + drive_config cache updated
    st = client.put("/api/inbox/drive/folder-color",
                    json={"folder_id": "inbox-folder", "color": "#4a86e8"}).json()
    assert "#4a86e8" in seen["color_body"]
    assert st["inbox_folder_color"] == "#4986e7"  # snapped value Drive returned

    # a wide (document) upload → stored on Drive, in the inbox folder
    up = client.post("/api/inbox/upload",
                     files={"file": ("doc.png", _color_png(1600, 1000), "image/png")}).json()
    assert up["kind"] == "document" and up["storage"] == "drive"
    assert '"parents": ["inbox-folder"]' in seen["upload_body"]

    doc_id = up["document_id"]
    p = client.post(f"/api/inbox/{doc_id}/process",
                    json={"final_name": "Договір", "target_folder_id": "root-folder-child"})
    assert p.status_code == 200, p.text
    assert seen["moved_to"] == "root-folder-child"

    # clearing a folder
    st = client.put("/api/inbox/drive/folders", json={"clear_inbox": True}).json()
    assert st["inbox_folder_id"] is None


def test_document_branch_never_imports_ai():
    """A personal document must never be reachable by app.services.ai
    (ROADMAP.md Крок 3). Enforced by walking the import graph of the modules the
    document branch actually runs through."""
    import ast

    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    seen: set[str] = set()
    queue = ["services.inbox", "scanner.aspect"]
    while queue:
        mod = queue.pop()
        if mod in seen:
            continue
        seen.add(mod)
        path = root / (mod.replace(".", "/") + ".py")
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert name != "app.services.ai", f"{mod} imports app.services.ai"
                if name.startswith("app."):
                    queue.append(name[len("app."):])


def test_stored_scan_frames_live_on_the_data_volume(tmp_path, monkeypatch):
    """They used to be written inside the package, so every rebuild of the
    container threw away the originals and the journal's thumbnails 404'd."""
    from app.api import inbox
    from app.core.config import BASE_DIR

    assert inbox.SCAN_INPUT_DIR == BASE_DIR / "data" / "scan_inputs"

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "job-1").write_bytes(b"frame")
    monkeypatch.setattr(inbox, "_LEGACY_SCAN_INPUT_DIR", legacy)
    monkeypatch.setattr(inbox, "SCAN_INPUT_DIR", tmp_path / "data" / "scan_inputs")

    assert inbox.migrate_legacy_scan_inputs() == 1
    assert (tmp_path / "data" / "scan_inputs" / "job-1").read_bytes() == b"frame"
    # Idempotent: nothing left to move on the next start.
    assert inbox.migrate_legacy_scan_inputs() == 0
