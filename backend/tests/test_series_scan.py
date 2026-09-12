"""Photos that arrived together: one long receipt, or several separate ones.

The single-photo path is stubbed (no OCR, no model): what is checked here is the
decision — which reading a batch gets, that an uncertain batch is parked as one
row until the user answers, and that "one receipt" glues the frames before
recognising them.
"""
import asyncio
import datetime as dt

import cv2
import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import models  # noqa: F401 — registers receipts/stores tables
from app.models import scanner  # noqa: F401 — registers scan_jobs
from app.models import shopping  # noqa: F401 — shopping auto-tick runs on save
from app.models.scanner import ScanJob, ScanStatus
from app.services import series_scan


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from app.api import inbox

    engine = create_engine(
        f"sqlite:///{tmp_path / 'series.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(inbox, "SCAN_INPUT_DIR", tmp_path / "scan_inputs")
    monkeypatch.setattr(inbox, "scan_input_path",
                        lambda job_id: tmp_path / "scan_inputs" / job_id)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


def _photo(text: str = "CHEK", height: int = 600) -> bytes:
    paper = np.full((height, 400, 3), 245, np.uint8)
    cv2.putText(paper, text, (20, height // 2), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (20, 20, 20), 2)
    ok, buf = cv2.imencode(".png", paper)
    assert ok
    return buf.tobytes()


def _frames(count: int, height: int = 600):
    return [(_photo(f"CHEK {i}", height), "image/png", f"part{i}.png")
            for i in range(1, count + 1)]


def _fiscal_url(document: str) -> str:
    return ("https://cabinet.tax.gov.ua/cashregs/check?"
            f"id={document}&fn=4001118996&date=20260912&time=121046&sm=100.00")


def _qr_photo(url: str) -> bytes:
    code = cv2.QRCodeEncoder.create().encode(url)
    big = cv2.resize(code, None, fx=8, fy=8, interpolation=cv2.INTER_NEAREST)
    canvas = np.full((big.shape[0] + 60, big.shape[1] + 60), 255, np.uint8)
    canvas[30:30 + big.shape[0], 30:30 + big.shape[1]] = big
    ok, buf = cv2.imencode(".png", canvas)
    assert ok
    return buf.tobytes()


def _stub_single_photo(monkeypatch, *, status=ScanStatus.parsed):
    """Stand in for the ordinary single-photo pipeline; records what it was given."""
    handled: list[bytes] = []

    async def fake_handle(session, data, filename, media_type, **kwargs):
        from app.api.inbox import UploadResult

        handled.append(data)
        job = ScanJob(status=status, method="ai", confidence="high",
                      source_filename=filename, raw_ocr={"notes": []},
                      receipt_id=f"receipt-{len(handled)}" if status is ScanStatus.parsed else None)
        session.add(job)
        session.commit()
        session.refresh(job)
        return UploadResult(kind="receipt", scan_job_id=job.id, method="ai",
                            confidence="high", receipt_id=job.receipt_id,
                            needs_review=status is ScanStatus.needs_review)

    monkeypatch.setattr("app.api.inbox._handle_receipt", fake_handle)
    return handled


# --------------------------------------------------------------------------- #
# The decision itself
# --------------------------------------------------------------------------- #
def test_different_qr_codes_mean_different_receipts():
    kind, why = series_scan.decide(["fiscal:1:11", "fiscal:1:22"])
    assert kind == series_scan.SEPARATE
    assert "різних QR" in why


def test_the_same_qr_on_every_frame_means_one_receipt():
    kind, _why = series_scan.decide(["fiscal:1:11", "fiscal:1:11"])
    assert kind == series_scan.ONE


def test_no_qr_at_all_is_a_question_for_the_user():
    kind, why = series_scan.decide([None, None])
    assert kind == series_scan.ASK
    assert "один довгий чек чи різні чеки" in why


def test_a_qr_on_only_some_frames_is_a_question_too():
    """A long receipt carries its QR on the last frame only — but so does a pile
    of receipts of which one was shot twice. Ask instead of guessing."""
    kind, why = series_scan.decide(["fiscal:1:11", None])
    assert kind == series_scan.ASK
    assert "не на всіх кадрах" in why


def test_a_single_photo_is_never_a_question():
    assert series_scan.decide([None])[0] == series_scan.ONE


# --------------------------------------------------------------------------- #
# What the batch does
# --------------------------------------------------------------------------- #
def test_photos_of_different_receipts_become_separate_rows(db, monkeypatch):
    handled = _stub_single_photo(monkeypatch)
    parts = [(_qr_photo(_fiscal_url("111")), "image/png", "a.png"),
             (_qr_photo(_fiscal_url("222")), "image/png", "b.png")]

    outcome = asyncio.run(series_scan.scan_batch(db, parts))
    assert outcome.kind == series_scan.SEPARATE
    assert len(outcome.results) == 2
    assert len(handled) == 2
    # Each frame went down the single-photo path untouched — nothing was glued.
    assert handled[0] == parts[0][0]


def test_an_uncertain_batch_waits_as_one_row(db, monkeypatch):
    """Nothing is recognised until the user says what the batch is: reading it
    the wrong way costs a model call per frame and files receipts to clean up."""
    handled = _stub_single_photo(monkeypatch)

    outcome = asyncio.run(series_scan.scan_batch(db, _frames(3)))
    assert outcome.kind == series_scan.ASK
    assert outcome.results == []
    assert handled == []

    job = db.get(ScanJob, outcome.scan_job_id)
    assert db.query(ScanJob).count() == 1, "one batch, one row"
    assert series_scan.needs_choice(job) is True
    assert job.raw_ocr["parts"] == 3
    # Every frame is kept, so the choice can still be acted on later.
    assert len(series_scan.frames_of(job)) == 3


def test_answering_one_receipt_glues_the_frames_into_one_photo(db, monkeypatch):
    handled = _stub_single_photo(monkeypatch)
    parked = asyncio.run(series_scan.scan_batch(db, _frames(3)))
    job = db.get(ScanJob, parked.scan_job_id)

    outcome = asyncio.run(series_scan.resolve(db, job, series_scan.ONE))
    assert outcome.kind == series_scan.ONE
    assert len(handled) == 1, "one glued image, one recognition"

    glued = cv2.imdecode(np.frombuffer(handled[0], np.uint8), cv2.IMREAD_COLOR)
    assert glued.shape[0] > 600, "the glued image is taller than one frame"
    row = db.get(ScanJob, outcome.results[0].scan_job_id)
    assert row.raw_ocr["parts"] == 3
    assert row.method.startswith("series:3")
    # The row the question was asked on is gone: one batch stays one row.
    assert db.query(ScanJob).count() == 1


def test_answering_separate_receipts_splits_the_batch_into_rows(db, monkeypatch):
    handled = _stub_single_photo(monkeypatch)
    parked = asyncio.run(series_scan.scan_batch(db, _frames(2)))
    job = db.get(ScanJob, parked.scan_job_id)

    outcome = asyncio.run(series_scan.resolve(db, job, series_scan.SEPARATE))
    assert outcome.kind == series_scan.SEPARATE
    assert len(handled) == 2
    assert db.get(ScanJob, job.id) is None, "the batch row gave way to its rows"
    assert db.query(ScanJob).count() == 2


def test_adding_a_photo_re_glues_the_whole_series(db, monkeypatch):
    """"➕ Ще фото" is itself the answer to the question: this frame belongs to
    that receipt, so everything is glued again — the old row does not linger."""
    handled = _stub_single_photo(monkeypatch, status=ScanStatus.needs_review)
    first = asyncio.run(series_scan.scan_batch(db, _frames(2), choice=series_scan.ONE))
    job = db.get(ScanJob, first.scan_job_id)

    again = asyncio.run(series_scan.scan_batch(
        db, [(_photo("CHEK 3"), "image/png", "c.png")], job=job))
    assert len(handled) == 2, "glued once per batch, not once per frame"
    row = db.get(ScanJob, again.results[0].scan_job_id)
    assert row.raw_ocr["parts"] == 3
    assert db.query(ScanJob).count() == 1


def test_an_unanswerable_choice_is_reported(db, monkeypatch):
    _stub_single_photo(monkeypatch)
    parked = asyncio.run(series_scan.scan_batch(db, _frames(2)))
    job = db.get(ScanJob, parked.scan_job_id)
    with pytest.raises(ValueError, match="невідомий вибір"):
        asyncio.run(series_scan.resolve(db, job, "maybe"))

    for index in range(1, 3):
        series_scan.part_path(job.id, index).unlink()
    with pytest.raises(ValueError, match="більше не збережені"):
        asyncio.run(series_scan.resolve(db, job, series_scan.ONE))


def test_rescanning_a_batch_glues_its_frames_again(db, monkeypatch):
    handled = _stub_single_photo(monkeypatch, status=ScanStatus.needs_review)
    first = asyncio.run(series_scan.scan_batch(db, _frames(2), choice=series_scan.ONE))
    job = db.get(ScanJob, first.scan_job_id)

    result = asyncio.run(series_scan.rescan_series(db, job))
    assert len(handled) == 2
    row = db.get(ScanJob, result.scan_job_id)
    assert row.raw_ocr["parts"] == 2
    assert db.query(ScanJob).count() == 1


def test_too_many_frames_are_refused_before_any_work(db, monkeypatch):
    handled = _stub_single_photo(monkeypatch)
    with pytest.raises(ValueError, match="максимум"):
        asyncio.run(series_scan.scan_batch(db, _frames(series_scan.MAX_PARTS + 1)))
    with pytest.raises(ValueError, match="жодного фото"):
        asyncio.run(series_scan.scan_batch(db, []))
    assert handled == []


def test_frames_that_cannot_be_glued_still_produce_a_row(db, monkeypatch):
    """A broken frame must not lose the batch — the first photo is read instead."""
    handled = _stub_single_photo(monkeypatch)
    parts = [(b"not an image", "image/png", "a.png"), (_photo(), "image/png", "b.png")]
    outcome = asyncio.run(series_scan.scan_batch(db, parts, choice=series_scan.ONE))
    row = db.get(ScanJob, outcome.results[0].scan_job_id)
    assert any("склеїти кадри не вдалося" in n for n in row.raw_ocr["notes"])
    assert len(handled) == 1
