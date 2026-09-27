"""Failures must stay visible.

Two regressions this pins down, both seen live on the Укрнафта account:
  * a raw 401 from an adapter (curl_cffi's `raise_for_status`, not AdapterError)
    left the account "active" with an empty last_error, so the UI said all good;
  * a later successful manual run wiped `last_error`, hiding the fact that every
    scheduled run had been failing for days.
"""
import datetime as dt

import pytest
from curl_cffi.requests import Response as CurlResponse
from curl_cffi.requests.exceptions import RequestException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app
from app.models import models  # noqa: F401 — registers tables
from app.models.models import (
    Account, AccountStatus, AuthType, CollectionRun, RunStatus, Store,
)
from app.models import scanner as scanner_models  # noqa: F401 — registers tables
from app.models import shopping as shopping_models  # noqa: F401
from app.services.collector import is_auth_failure, is_transient_network_failure


def _curl_error(status: int) -> Exception:
    response = CurlResponse()
    response.status_code = status
    response.ok = False   # curl_cffi keeps `ok` as a plain attribute
    try:
        response.raise_for_status()
    except RequestException as exc:  # what the adapters actually raise
        return exc
    raise AssertionError(f"status {status} did not raise")


@pytest.mark.parametrize("status, expected", [(401, True), (403, True), (500, False)])
def test_raw_http_errors_are_classified(status, expected):
    assert is_auth_failure(_curl_error(status)) is expected


def test_network_failures_are_not_auth_failures():
    assert is_auth_failure(RequestException("Could not resolve host: example", 6)) is False
    assert is_auth_failure(TimeoutError("Operation timed out")) is False


def test_message_only_errors_fall_back_to_text():
    assert is_auth_failure(RuntimeError("HTTP Error 401: ")) is True
    assert is_auth_failure(RuntimeError("Unauthorized")) is True
    assert is_auth_failure(RuntimeError("щось інше зламалось")) is False


def test_dns_and_connection_errors_are_classified_as_transient_network():
    # 2026-09-27: a resolver hiccup on the host took down 24 unrelated store
    # accounts at once with this exact shape — must retry, not needs_reauth.
    assert is_transient_network_failure(
        RequestException("Failed to perform, curl: (6) Could not resolve host: wallet-prod.pc-service.ua", 6)
    ) is True
    assert is_transient_network_failure(OSError("[Errno -3] Temporary failure in name resolution")) is True
    assert is_transient_network_failure(RuntimeError("Connection closed while reading from the driver")) is True
    assert is_transient_network_failure(RuntimeError("Unauthorized")) is False
    assert is_transient_network_failure(RuntimeError("щось інше зламалось")) is False


@pytest.fixture()
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'health.db'}",
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


def test_recent_failures_survive_a_later_success(db, client):
    now = dt.datetime.now(dt.timezone.utc)
    store = Store(code="ukrnafta", name="Укрнафта", auth_type=AuthType.phone_otp,
                  network_type="azs")
    db.add(store)
    db.flush()
    account = Account(store_id=store.id, label="Укрнафта Максим")
    db.add(account)
    db.flush()
    for hours in (2, 9):
        db.add(CollectionRun(account_id=account.id, trigger="schedule",
                             status=RunStatus.error,
                             started_at=now - dt.timedelta(hours=hours),
                             finished_at=now - dt.timedelta(hours=hours),
                             error_message="HTTP Error 401: "))
    # …and then the user pressed "Зібрати зараз" and it worked.
    db.add(CollectionRun(account_id=account.id, trigger="manual",
                         status=RunStatus.success, started_at=now, finished_at=now,
                         receipts_found=14, receipts_new=2))
    db.commit()

    row = client.get("/api/accounts").json()[0]
    assert row["last_error"] is None          # wiped by the successful run…
    assert row["failed_runs_24h"] == 2        # …but the failures are still reported
    assert "401" in row["last_failed_error"]
    assert row["last_collected_at"] is not None
    assert row["store"]["store_class"] == "fuel"


def test_a_fixed_integration_stops_being_a_problem(db, client):
    """The badge counts what is broken *now*: a failure followed by a successful
    run is history, not a problem."""
    now = dt.datetime.now(dt.timezone.utc)
    store = Store(code="ukrnafta", name="Укрнафта", auth_type=AuthType.phone_otp)
    db.add(store)
    db.flush()
    account = Account(store_id=store.id, label="Укрнафта Максим")
    db.add(account)
    db.flush()
    db.add(CollectionRun(account_id=account.id, trigger="schedule",
                         status=RunStatus.error, started_at=now - dt.timedelta(hours=2),
                         finished_at=now - dt.timedelta(hours=2),
                         error_message="HTTP Error 401: "))
    db.commit()

    problems = client.get("/api/logs/problems").json()
    assert problems["total"] == 1 and problems["accounts"] == 1
    assert problems["items"][0]["link"] == f"/accounts/{account.id}"
    assert {"to": "/accounts", "count": 1, "tone": "danger",
            "hint": "Інтеграцій зі збоєм"} in client.get("/api/nav/badges").json()["items"]

    # The user logs in again and collects successfully.
    db.add(CollectionRun(account_id=account.id, trigger="manual",
                         status=RunStatus.success, started_at=now, finished_at=now,
                         receipts_found=14, receipts_new=2))
    db.commit()

    assert client.get("/api/logs/problems").json()["total"] == 0
    assert client.get("/api/nav/badges").json()["items"] == []   # no badge at all
    # …while the journal still remembers the failure.
    assert any("401" in (i["message"] or "")
               for i in client.get("/api/logs?source=collector").json()["items"])


def test_reset_error_acknowledges_latest_failed_run(db, client):
    now = dt.datetime.now(dt.timezone.utc)
    store = Store(code="ukrnafta", name="Укрнафта", auth_type=AuthType.phone_otp)
    db.add(store)
    db.flush()
    account = Account(store_id=store.id, label="Укрнафта Максим", last_error="DNS failure")
    db.add(account)
    db.flush()
    db.add(CollectionRun(account_id=account.id, trigger="schedule",
                         status=RunStatus.error, started_at=now - dt.timedelta(hours=1),
                         finished_at=now - dt.timedelta(hours=1),
                         error_message="DNS failure"))
    db.commit()

    assert client.get("/api/logs/problems").json()["accounts"] == 1
    response = client.post(f"/api/accounts/{account.id}/reset-error")
    assert response.status_code == 200
    assert response.json()["last_error"] is None
    assert client.get("/api/logs/problems").json()["accounts"] == 0


def test_an_account_waiting_for_login_is_a_problem_without_any_run(db, client):
    store = Store(code="metro", name="METRO", auth_type=AuthType.device_uuid)
    db.add(store)
    db.flush()
    db.add(Account(store_id=store.id, label="METRO",
                   status=AccountStatus.needs_reauth))
    db.commit()

    problems = client.get("/api/logs/problems").json()
    assert problems["accounts"] == 1
    assert "вхід" in problems["items"][0]["message"]


def test_manual_only_stores_are_never_counted(db, client):
    """Сканер (OCR) cannot collect anything, so its ancient failed run is not a
    problem the user can act on."""
    now = dt.datetime.now(dt.timezone.utc)
    store = Store(code="scanner", name="Сканер (OCR)", auth_type=AuthType.manual)
    db.add(store)
    db.flush()
    account = Account(store_id=store.id, label="Ручні чеки")
    db.add(account)
    db.flush()
    db.add(CollectionRun(account_id=account.id, trigger="schedule",
                         status=RunStatus.error, started_at=now - dt.timedelta(days=20),
                         finished_at=now - dt.timedelta(days=20),
                         error_message="Немає токена"))
    db.commit()

    assert client.get("/api/logs/problems").json()["total"] == 0


def test_inbox_badge_counts_exactly_what_that_page_shows(db, client):
    """The badge must match the page it points at: everything the inbox shows —
    scans in progress, documents, recordings and the ДПС queue — and nothing
    else. There is deliberately no badge for the journal."""
    from app.models.scanner import (
        FiscalQueueJob, FiscalQueueStatus, InboxDocument, InboxStatus, ScanJob,
        ScanStatus, VoiceMessage,
    )

    now = dt.datetime.now(dt.timezone.utc)
    db.add(ScanJob(source_filename="blurry.jpg", status=ScanStatus.failed))
    db.add(ScanJob(source_filename="done.jpg", status=ScanStatus.parsed))
    db.add(ScanJob(source_filename="old.jpg", status=ScanStatus.failed, archived_at=now))
    db.add(InboxDocument(storage="local", status=InboxStatus.unprocessed))
    db.add(VoiceMessage(local_filename="a.ogg"))                       # waiting
    db.add(VoiceMessage(local_filename="b.ogg", applied="список покупок"))  # handled
    db.add(VoiceMessage(local_filename="c.ogg", archived_at=now))      # archived
    db.add(FiscalQueueJob(url="https://cabinet.tax.gov.ua/x", dedup_key="x",
                          status=FiscalQueueStatus.captcha))
    db.commit()

    badges = {b["to"]: b for b in client.get("/api/nav/badges").json()["items"]}
    # failed scan + unprocessed document + unhandled recording + open ДПС job
    assert badges["/inbox"]["count"] == 4
    assert badges["/inbox"]["tone"] == "warning"
    assert "/logs" not in badges
    assert "/settings/scanner" not in badges


def test_a_fiscal_job_closes_when_the_receipt_arrives_another_way(db, client):
    """A photo read by the QR ladder or AI leaves its ДПС job behind. Chasing the
    tax API for a receipt already in the database is pointless, so such a row is
    neither a problem nor part of any counter — and the scheduler closes it."""
    from app.models.scanner import FiscalQueueJob, FiscalQueueStatus, ScanJob, ScanStatus
    from app.services import fiscal_queue

    scan = ScanJob(source_filename="qr.jpg", status=ScanStatus.parsed, method="qr",
                   receipt_id="receipt-1")
    db.add(scan)
    db.flush()
    stuck = FiscalQueueJob(url="https://cabinet.tax.gov.ua/a", dedup_key="a",
                           status=FiscalQueueStatus.captcha)
    done_elsewhere = FiscalQueueJob(url="https://cabinet.tax.gov.ua/b", dedup_key="b",
                                    status=FiscalQueueStatus.queued, scan_job_id=scan.id)
    db.add_all([stuck, done_elsewhere])
    db.commit()

    assert [j.id for j in fiscal_queue.open_jobs(db)] == [stuck.id]
    problems = client.get("/api/logs/problems").json()["items"]
    assert [p["id"] for p in problems] == [f"fiscal:{stuck.id}"]
    assert problems[0]["link"] == "/inbox"        # where the queue is now shown

    # …and the obsolete row is closed for good, so retries stop as well.
    assert fiscal_queue.close_obsolete(db) == 1
    db.refresh(done_elsewhere)
    assert done_elsewhere.status == FiscalQueueStatus.imported
    assert done_elsewhere.receipt_id == "receipt-1"
    assert client.get("/api/fiscal-queue?open_only=true").json()[0]["id"] == stuck.id


def test_a_queue_row_closes_when_the_receipt_is_already_in_the_database(db):
    """The row is about a ДПС *document*, so any receipt carrying that identity
    closes it — even one imported as cabinet XML (which is stored under its own
    external_id but keeps the tax-cabinet link)."""
    from app.models.models import Account, AuthType, Receipt, Store
    from app.models.scanner import FiscalQueueJob, FiscalQueueStatus
    from app.services import fiscal_queue

    store = Store(code="scanner", name="Сканер (OCR)", auth_type=AuthType.manual)
    db.add(store)
    db.flush()
    account = Account(store_id=store.id, label="Ручні чеки")
    db.add(account)
    db.flush()

    url = ("https://cabinet.tax.gov.ua/cashregs/check"
           "?id=7814443012&date=20260916&time=121513&fn=4001404596&sm=686.57")
    job = FiscalQueueJob(url=url, dedup_key="fiscal:4001404596:7814443012",
                         status=FiscalQueueStatus.queued)
    db.add(job)
    db.commit()
    assert [j.id for j in fiscal_queue.open_jobs(db)] == [job.id]

    # Same document, imported earlier from the cabinet XML.
    db.add(Receipt(account_id=account.id, store_code="scanner",
                   external_id="cabinet-check:d6e74738", receipt_url=url,
                   purchased_at=dt.datetime(2026, 9, 16, 9, 15, 13, tzinfo=dt.timezone.utc),
                   total_amount=686.57))
    db.commit()

    assert fiscal_queue.open_jobs(db) == []
    assert fiscal_queue.close_obsolete(db) == 1
    db.refresh(job)
    assert job.status == FiscalQueueStatus.imported and job.receipt_id is not None


def test_a_queue_row_closes_on_the_same_minute_and_amount(db):
    """No shared identity (a photo-read receipt keeps its own external_id), but
    the QR itself states time and amount — that is enough."""
    from app.models.models import Account, AuthType, Receipt, Store
    from app.models.scanner import FiscalQueueJob, FiscalQueueStatus
    from app.services import fiscal_queue
    from app.core.time import store_local_to_utc

    store = Store(code="scanner", name="Сканер (OCR)", auth_type=AuthType.manual)
    db.add(store)
    db.flush()
    account = Account(store_id=store.id, label="Ручні чеки")
    db.add(account)
    db.flush()
    job = FiscalQueueJob(
        url="https://cabinet.tax.gov.ua/cashregs/check?id=555&date=20260912&time=1951&sm=834",
        dedup_key="555", status=FiscalQueueStatus.queued)
    db.add(job)
    db.commit()
    assert len(fiscal_queue.open_jobs(db)) == 1

    db.add(Receipt(account_id=account.id, store_code="scanner", external_id="scan:abc",
                   purchased_at=store_local_to_utc(dt.datetime(2026, 9, 12, 19, 51)),
                   total_amount=834))
    db.commit()

    assert fiscal_queue.open_jobs(db) == []
