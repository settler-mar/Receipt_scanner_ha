"""Photos that arrived together: one long receipt, or several separate ones.

A batch is one thing to decide about, so it lands as **one** inbox row and the
decision is made once, for all its frames:

  1. read the fiscal QR of every frame (locally, no model call);
  2. several different ДПС documents → these are separate receipts, and the batch
     is split into ordinary single-photo rows;
  3. one and the same document on every frame → one receipt;
  4. anything less certain (no QR at all, or only some frames carry one) → ask
     the user which it is; nothing is recognised until they answer.

For one receipt the frames are first glued into a single tall image
(`app/scanner/stitch.py`) and only then recognised — from there on it is an
ordinary photo: one QR pass, one OCR pass, one model call. Every frame stays on
disk beside the row, so the glue can be redone later.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.scanner import ScanJob, ScanStatus
from app.scanner import stitch
from app.scanner.fiscal_qr import fiscal_identity, scan_qr

logger = logging.getLogger(__name__)

# A receipt does not run to twenty photos; the cap keeps one accidental
# multi-select from firing twenty vision calls.
MAX_PARTS = 10

ONE = "one"            # one receipt photographed in parts
SEPARATE = "separate"  # several receipts photographed in one go
ASK = "ask"            # cannot be told from the QR codes alone


@dataclasses.dataclass
class SeriesOutcome:
    """What a batch of photos turned into.

    `kind` is which of the three readings was used; `results` carries one entry
    per receipt (empty while the user is still being asked), and `scan_job_id`
    points at the row holding the batch.
    """

    kind: str
    results: list
    note: str = ""
    scan_job_id: str | None = None

    @property
    def merged(self) -> bool:
        return self.kind == ONE

    @property
    def needs_choice(self) -> bool:
        return self.kind == ASK


# --------------------------------------------------------------------------- #
# Frames on disk
# --------------------------------------------------------------------------- #
def part_path(job_id: str, index: int):
    """Where frame `index` (1-based) of a batch is stored.

    Distinct from the job's own input file, which for a glued series holds the
    combined image — the frames are what it was made of.
    """
    from app.api.inbox import scan_input_path

    return scan_input_path(f"{job_id}-p{index}")


def store_frames(job_id: str, parts: list[tuple[bytes, str, str | None]]) -> None:
    from app.api.inbox import SCAN_INPUT_DIR

    SCAN_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    for index, (data, _media_type, _filename) in enumerate(parts, start=1):
        part_path(job_id, index).write_bytes(data)


def stored_parts(job: ScanJob) -> int:
    return int((job.raw_ocr or {}).get("parts") or 0)


def frames_of(job: ScanJob) -> list[tuple[bytes, str, str | None]]:
    """Every stored frame of this row, in the order they were shot."""
    from app.api.inbox import scan_input_path

    frames: list[tuple[bytes, str, str | None]] = []
    for index in range(1, stored_parts(job) + 1):
        path = part_path(job.id, index)
        if not path.is_file():
            # Rows written before frames had their own names kept the first
            # frame as the job's input and the rest as "<job>-2", "<job>-3".
            path = scan_input_path(job.id if index == 1 else f"{job.id}-{index}")
        if path.is_file():
            frames.append((path.read_bytes(), "image/jpeg", f"кадр {index}"))
    return frames


def is_series(job: ScanJob) -> bool:
    return bool((job.raw_ocr or {}).get("series"))


def needs_choice(job: ScanJob) -> bool:
    return bool((job.raw_ocr or {}).get("pending_choice"))


def open_series(db: Session, job_id: str) -> ScanJob | None:
    """A row that can still take another photo: a batch, or a single photo that
    produced a draft — the second frame of a long receipt usually arrives only
    after the user sees the first one caught just the top of it."""
    job = db.get(ScanJob, job_id)
    if job is None or job.receipt_id is not None:
        return None
    raw = job.raw_ocr or {}
    if raw.get("series") or raw.get("draft") or raw.get("pending_choice"):
        return job
    return None


def latest_series(db: Session, *, within_minutes: int = 30) -> ScanJob | None:
    """The most recent batch still open for more photos."""
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=within_minutes)
    rows = db.execute(
        select(ScanJob).where(ScanJob.receipt_id.is_(None))
        .order_by(ScanJob.created_at.desc()).limit(10)
    ).scalars()
    for job in rows:
        created = job.created_at
        if created is not None and created.tzinfo is None:
            created = created.replace(tzinfo=dt.timezone.utc)
        if created is not None and created < since:
            break
        if is_series(job):
            return job
    return None


# --------------------------------------------------------------------------- #
# Deciding what the batch is
# --------------------------------------------------------------------------- #
def frame_fiscal_codes(parts: list[tuple[bytes, str, str | None]]) -> list[str | None]:
    """The ДПС document each frame names, or None where no QR could be read."""
    codes: list[str | None] = []
    for data, _media_type, _filename in parts:
        found = None
        for payload in scan_qr(data).payloads:
            found = fiscal_identity(payload)
            if found:
                break
        codes.append(found)
    return codes


def distinct_fiscal_codes(parts: list[tuple[bytes, str, str | None]]) -> list[str]:
    seen: list[str] = []
    for code in frame_fiscal_codes(parts):
        if code and code not in seen:
            seen.append(code)
    return seen


def decide(codes: list[str | None]) -> tuple[str, str]:
    """(kind, why) for a batch whose frames read these QR codes."""
    if len(codes) <= 1:
        return ONE, ""
    distinct = {c for c in codes if c}
    if len(distinct) > 1:
        return SEPARATE, f"кадри містять {len(distinct)} різних QR — це різні чеки"
    if distinct and all(codes):
        return ONE, "на всіх кадрах той самий фіскальний QR — це один чек"
    if distinct:
        missing = sum(1 for c in codes if not c)
        return ASK, (f"QR прочитано не на всіх кадрах ({missing} без QR) — "
                     "це один довгий чек чи різні чеки?")
    return ASK, "на кадрах не знайдено QR — це один довгий чек чи різні чеки?"


# --------------------------------------------------------------------------- #
# Doing the work
# --------------------------------------------------------------------------- #
async def scan_batch(
    db: Session,
    parts: list[tuple[bytes, str, str | None]],
    *,
    job: ScanJob | None = None,
    choice: str | None = None,
) -> SeriesOutcome:
    """Photos that arrived together → the receipts they actually are.

    `job` continues an existing row ("add one more photo"), which is itself an
    answer to the question — those frames belong to that receipt. `choice`
    forces the reading instead of deciding from the QR codes.
    """
    if not parts:
        raise ValueError("не передано жодного фото")
    if len(parts) > MAX_PARTS:
        raise ValueError(f"забагато фото в серії (максимум {MAX_PARTS})")

    if job is not None:
        previous = frames_of(job)
        return await _recognise_one(db, previous + list(parts), previous_job=job)

    kind, why = (choice, "") if choice else decide(frame_fiscal_codes(parts))
    if kind == SEPARATE:
        return await _recognise_separately(db, parts, note=why)
    if kind == ASK:
        return _park_for_choice(db, parts, note=why)
    return await _recognise_one(db, parts, note=why)


async def resolve(db: Session, job: ScanJob, choice: str) -> SeriesOutcome:
    """Answer the question a parked batch asked: one receipt, or several."""
    if choice not in (ONE, SEPARATE):
        raise ValueError(f"невідомий вибір {choice!r}")
    frames = frames_of(job)
    if not frames:
        raise ValueError("кадри цієї серії більше не збережені — завантажте фото ще раз")
    if choice == ONE:
        return await _recognise_one(db, frames, previous_job=job)
    outcome = await _recognise_separately(db, frames)
    _drop(db, job)
    return outcome


def _park_for_choice(db: Session, parts, *, note: str) -> SeriesOutcome:
    """Keep the batch as one row and wait for the user to say what it is.

    Nothing is recognised yet: reading it the wrong way costs a model call per
    frame and files receipts that then have to be cleaned up by hand.
    """
    job = ScanJob(
        source_filename=parts[0][2],
        status=ScanStatus.needs_review,
        method="series",
        confidence="none",
        raw_ocr={"series": True, "pending_choice": True, "parts": len(parts),
                 "notes": [note]},
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    store_frames(job.id, parts)
    return SeriesOutcome(kind=ASK, results=[], note=note, scan_job_id=job.id)


async def _recognise_separately(db: Session, parts, *, note: str = "") -> SeriesOutcome:
    """Different receipts: each frame goes down the ordinary single-photo path."""
    from app.api.inbox import _handle_receipt

    results = []
    for data, media_type, filename in parts:
        results.append(await _handle_receipt(db, data, filename, media_type))
    return SeriesOutcome(
        kind=SEPARATE, results=results,
        note=note or f"оброблено як {len(parts)} окремих чеків",
    )


async def _recognise_one(db: Session, parts, *, note: str = "",
                         previous_job: ScanJob | None = None) -> SeriesOutcome:
    """One receipt: glue the frames, then recognise the result as one photo."""
    from app.api.inbox import _handle_receipt

    frames = [data for data, _media_type, _filename in parts]
    notes = [note] if note else []
    try:
        image, seam_notes = stitch.stitch(frames)
        notes += seam_notes
    except stitch.StitchError as exc:
        logger.warning("stitching failed: %s", exc)
        notes.append(f"склеїти кадри не вдалося ({exc}) — розпізнано лише перший кадр")
        image = frames[0]

    result = await _handle_receipt(db, image, parts[0][2], "image/jpeg")
    job = db.get(ScanJob, result.scan_job_id)
    if job is not None:
        store_frames(job.id, parts)
        job.method = f"series:{len(parts)}·{job.method or 'none'}"
        job.raw_ocr = {
            **(job.raw_ocr or {}),
            "series": True,
            "parts": len(parts),
            "notes": [*notes, *(job.raw_ocr or {}).get("notes", [])],
        }
        db.commit()
        result.method = job.method
    if previous_job is not None and previous_job.id != result.scan_job_id:
        # The batch moved to the row holding the glued image; the row it grew out
        # of would otherwise stay behind as a duplicate of the same photos.
        _drop(db, previous_job)
    return SeriesOutcome(kind=ONE, results=[result], note=note,
                         scan_job_id=result.scan_job_id)


async def rescan_series(db: Session, job: ScanJob, *, ai_first: bool = True):
    """Re-run a whole batch: glue its frames again and recognise the result."""
    frames = frames_of(job)
    if not frames:
        raise ValueError("оригінали кадрів не збережено — завантажте фото ще раз")
    outcome = await _recognise_one(db, frames, previous_job=job)
    return outcome.results[0]


def _drop(db: Session, job: ScanJob) -> None:
    """Remove a row and the frames stored for it."""
    for index in range(1, stored_parts(job) + 1):
        part_path(job.id, index).unlink(missing_ok=True)
    from app.services import scan_jobs

    try:
        scan_jobs.delete_job(db, job.id)
    except Exception:  # noqa: BLE001 — a row already gone is not a problem
        logger.debug("could not drop batch row %s", job.id, exc_info=True)
