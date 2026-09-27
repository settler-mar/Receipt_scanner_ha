"""Unified journal: /api/logs merges per-subsystem histories into one feed."""
import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app
from app.models import models  # noqa: F401 — registers tables
from app.models import scanner  # noqa: F401
from app.models import shopping  # noqa: F401
from app.models.models import (
    Account, AuthType, CollectionRun, DuplicateRun, DuplicateRunStatus,
    RunStatus, Store,
)
from app.models.scanner import ScanJob, ScanStatus
from app.services import event_log


@pytest.fixture()
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'logs.db'}",
                        connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def db(engine):
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(engine):
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _override():
        s = TestingSession()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    yield TestClient(app)
    app.dependency_overrides.clear()


def _seed(db):
    now = dt.datetime.now(dt.timezone.utc)
    store = Store(code="atb", name="АТБ", auth_type=AuthType.manual)
    db.add(store)
    db.flush()
    account = Account(store_id=store.id, label="АТБ Max")
    db.add(account)
    db.flush()
    db.add(CollectionRun(account_id=account.id, trigger="schedule",
                         status=RunStatus.error, started_at=now - dt.timedelta(minutes=5),
                         finished_at=now - dt.timedelta(minutes=4),
                         error_message="Немає токена"))
    db.add(ScanJob(source_filename="cheque.jpg", status=ScanStatus.parsed,
                   method="qr", created_at=now - dt.timedelta(minutes=3)))
    db.add(DuplicateRun(trigger="schedule", status=DuplicateRunStatus.success,
                        started_at=now - dt.timedelta(minutes=2),
                        finished_at=now - dt.timedelta(minutes=2),
                        scanned_receipts=10, candidates_new=1))
    db.commit()
    event_log.record(db, source="shopping", title="Автоматична класифікація",
                     message="Перевірено 2, розкладено 2", details={"by_ai": 2})
    return account


def test_logs_merge_every_source_newest_first(db, client):
    account = _seed(db)
    page = client.get("/api/logs?limit=50").json()

    by_source = {i["source"]: i for i in page["items"]}
    assert {"collector", "scanner", "duplicates", "shopping"} <= set(by_source)
    assert [i["ts"] for i in page["items"]] == sorted(
        (i["ts"] for i in page["items"]), reverse=True)
    assert by_source["collector"]["level"] == "error"
    assert by_source["collector"]["link"] == f"/accounts/{account.id}"
    assert by_source["shopping"]["details"]["by_ai"] == 2
    assert {"code": "shopping", "name": "Список покупок"} in page["sources"]


def test_logs_filter_by_source_level_and_text(db, client):
    _seed(db)

    only = client.get("/api/logs?source=shopping").json()["items"]
    assert only and all(i["source"] == "shopping" for i in only)

    errors = client.get("/api/logs?level=error").json()["items"]
    assert errors and all(i["level"] == "error" for i in errors)

    found = client.get("/api/logs?q=токена").json()["items"]
    assert len(found) == 1 and found[0]["source"] == "collector"
    assert client.get("/api/logs?q=нічого-такого").json()["items"] == []


def test_logs_paginate(db, client):
    _seed(db)
    first = client.get("/api/logs?limit=2").json()
    assert len(first["items"]) == 2 and first["has_more"] is True
    second = client.get("/api/logs?limit=2&offset=2").json()
    assert {i["id"] for i in first["items"]} & {i["id"] for i in second["items"]} == set()


def test_event_log_never_breaks_the_job_it_describes(db, monkeypatch):
    """A journal line must not be able to fail the work that produced it."""
    rolled_back = []
    monkeypatch.setattr(type(db), "add",
                        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("db gone")))
    monkeypatch.setattr(type(db), "rollback", lambda *_a: rolled_back.append(True))

    event_log.record(db, source="system", title="щось пішло не так")

    assert rolled_back == [True]
