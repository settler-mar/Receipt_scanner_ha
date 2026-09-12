"""Group actions over the scan journal.

Rows are independent: a batch must report each row separately instead of dying
on the first one that cannot be archived, rescanned or deleted.
"""
import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import inbox
from app.db import Base, get_db
from app.main import app
from app.models import models  # noqa: F401 — registers receipts/stores tables
from app.models import scanner  # noqa: F401 — registers scan_jobs
from app.models.scanner import ScanJob, ScanStatus


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'bulk.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(inbox, "SCAN_INPUT_DIR", tmp_path / "scan_inputs")
    monkeypatch.setattr(inbox, "scan_input_path",
                        lambda job_id: tmp_path / "scan_inputs" / job_id)
    app.dependency_overrides[get_db] = lambda: factory()
    try:
        yield TestClient(app), factory
    finally:
        app.dependency_overrides.pop(get_db, None)


def _jobs(factory, count=3, **kwargs):
    ids = []
    with factory() as db:
        for index in range(count):
            job = ScanJob(id=f"job-{index}", status=ScanStatus.needs_review,
                          created_at=dt.datetime.now(dt.timezone.utc), **kwargs)
            db.add(job)
            ids.append(job.id)
        db.commit()
    return ids


def test_archiving_several_scans_at_once(client):
    api, factory = client
    ids = _jobs(factory)
    out = api.post("/api/inbox/scans/bulk", json={"ids": ids, "action": "archive"}).json()
    assert (out["done"], out["failed"]) == (3, 0)
    with factory() as db:
        assert all(db.get(ScanJob, i).archived_at is not None for i in ids)

    api.post("/api/inbox/scans/bulk", json={"ids": ids, "action": "unarchive"})
    with factory() as db:
        assert all(db.get(ScanJob, i).archived_at is None for i in ids)


def test_deleting_several_scans_at_once(client):
    api, factory = client
    ids = _jobs(factory)
    out = api.post("/api/inbox/scans/bulk", json={"ids": ids, "action": "delete"}).json()
    assert out["done"] == 3
    with factory() as db:
        assert db.query(ScanJob).count() == 0


def test_a_row_that_fails_does_not_stop_the_others(client):
    api, factory = client
    ids = _jobs(factory, 2)
    out = api.post("/api/inbox/scans/bulk",
                   json={"ids": [*ids, "job-missing"], "action": "archive"}).json()
    assert (out["done"], out["failed"]) == (2, 1)
    failed = [r for r in out["results"] if not r["ok"]]
    assert failed[0]["id"] == "job-missing"
    assert "не знайдено" in failed[0]["detail"]


def test_a_scan_with_a_receipt_is_not_deleted_by_accident(client):
    """Deleting a whole selection must not silently take saved receipts with it."""
    api, factory = client
    with factory() as db:
        from app.models.models import Account, AuthType, Receipt, Store

        store = Store(id="s-1", code="scanner", name="Сканер", auth_type=AuthType.manual)
        account = Account(id="acc-1", store_id=store.id, label="scanner")
        db.add_all([store, account, Receipt(
            id="r-1", account_id=account.id, store_code="scanner", external_id="e-1",
            total_amount=10, purchased_at=dt.datetime.now(dt.timezone.utc),
        ), ScanJob(id="job-saved", status=ScanStatus.parsed, receipt_id="r-1")])
        db.commit()
    out = api.post("/api/inbox/scans/bulk",
                   json={"ids": ["job-saved"], "action": "delete"}).json()
    assert out["failed"] == 1
    assert "підтвердіть" in out["results"][0]["detail"]
    with factory() as db:
        assert db.get(ScanJob, "job-saved") is not None


def test_bulk_ai_rescan_runs_every_selected_row(client, monkeypatch):
    api, factory = client
    ids = _jobs(factory, 2)
    seen = []

    async def fake_rescan(db, job_id, *, ai_first=False, replace=False):
        seen.append((job_id, ai_first))
        from app.api.inbox import UploadResult

        return UploadResult(kind="receipt", scan_job_id=job_id, method="ai")

    monkeypatch.setattr(inbox.scan_jobs, "rescan_job", fake_rescan)
    out = api.post("/api/inbox/scans/bulk", json={"ids": ids, "action": "rescan_ai"}).json()
    assert out["done"] == 2
    assert seen == [(ids[0], True), (ids[1], True)]


def test_a_series_row_rescans_all_of_its_frames(client, monkeypatch):
    api, factory = client
    with factory() as db:
        db.add(ScanJob(id="job-series", status=ScanStatus.needs_review,
                       raw_ocr={"series": True, "parts": 2}))
        db.commit()
    called = {}

    async def fake_series(db, job, *, ai_first=False):
        called["job"] = job.id
        from app.api.inbox import UploadResult

        return UploadResult(kind="receipt", scan_job_id=job.id, method="series:2")

    monkeypatch.setattr("app.services.series_scan.rescan_series", fake_series)
    out = api.post("/api/inbox/scans/bulk",
                   json={"ids": ["job-series"], "action": "rescan_ai"}).json()
    assert out["done"] == 1
    assert called["job"] == "job-series"


def test_the_batch_is_bounded_and_validated(client):
    api, _ = client
    assert api.post("/api/inbox/scans/bulk",
                    json={"ids": [], "action": "archive"}).status_code == 400
    assert api.post("/api/inbox/scans/bulk",
                    json={"ids": ["a"], "action": "explode"}).status_code == 400
    too_many = [f"id-{i}" for i in range(inbox.BULK_MAX + 1)]
    resp = api.post("/api/inbox/scans/bulk", json={"ids": too_many, "action": "archive"})
    assert resp.status_code == 400 and str(inbox.BULK_MAX) in resp.json()["detail"]
