"""A browser CAPTCHA session lives only inside the running process.

After a restart the queue row still carries the id of a session that no longer
exists; advertising it makes the Settings page poll a screen that can never come
back ("Браузерна сесія завершена" on every tick).
"""
import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app
from app.models import models  # noqa: F401 — registers receipts/stores tables
from app.models.scanner import FiscalQueueJob, FiscalQueueStatus
from app.services import fiscal_browser

JOB_ID = "job-1"
DEAD_SESSION = "0lHTbXtOetJgm6KnOo9hCfnPxsmL5Up3"


@pytest.fixture()
def client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'fq.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(FiscalQueueJob(
            id=JOB_ID, url="https://cabinet.tax.gov.ua/cashregs/check?id=1&fn=2",
            dedup_key="1:2",
            source="telegram", status=FiscalQueueStatus.captcha,
            session_id=DEAD_SESSION, created_at=dt.datetime.now(dt.timezone.utc),
            updated_at=dt.datetime.now(dt.timezone.utc),
        ))
        db.commit()
    app.dependency_overrides[get_db] = lambda: factory()
    try:
        yield TestClient(app), factory
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_a_session_from_a_previous_run_is_not_advertised(client):
    api, _ = client
    assert not fiscal_browser.is_live(DEAD_SESSION)
    job = api.get("/api/fiscal-queue").json()[0]
    # The row is still a CAPTCHA job — it just has no screen to attach to.
    assert job["status"] == "captcha"
    assert job["session_id"] is None


def test_polling_a_dead_session_clears_it_from_the_job(client):
    api, factory = client
    resp = api.get(f"/api/fiscal-queue/{JOB_ID}/browser/state?session_id={DEAD_SESSION}")
    assert resp.status_code == 410
    assert "сесія завершена" in resp.json()["detail"]
    with factory() as db:
        assert db.get(FiscalQueueJob, JOB_ID).session_id is None
