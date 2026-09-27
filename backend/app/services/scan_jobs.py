"""Actions on an already-finished scan: drop it, or run it through the pipeline
again (optionally forcing AI vision).

Shared by the inbox API and the Telegram bot so both offer the same operations
with the same rules — a scan that already produced a receipt is never silently
re-run or deleted from under the user.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from sqlalchemy.orm import Session

from app.models.scanner import ScanJob, ScanStatus

logger = logging.getLogger(__name__)


class ScanJobError(RuntimeError):
    """A rescan/delete the caller asked for cannot be carried out."""


def run_rescan_batch(job_ids: list[str], *, ai_first: bool = False) -> None:
    """Run a bulk rescan in a worker thread with an isolated DB session."""
    from app.db import SessionLocal
    from app.services import series_scan

    async def _run() -> None:
        with SessionLocal() as db:
            for job_id in job_ids:
                try:
                    job = db.get(ScanJob, job_id)
                    if job is None:
                        continue
                    if series_scan.is_series(job) and not job.receipt_id:
                        await series_scan.rescan_series(db, job, ai_first=ai_first)
                    else:
                        await rescan_job(db, job_id, ai_first=ai_first)
                except Exception:  # noqa: BLE001 — one row must not stop the batch
                    logger.exception("bulk rescan failed for scan %s", job_id)

    asyncio.run(_run())


def get_job(db: Session, job_id: str) -> ScanJob:
    job = db.get(ScanJob, job_id)
    if job is None:
        raise ScanJobError("Сканування не знайдено")
    return job


def live_receipt_id(db: Session, job: ScanJob) -> str | None:
    """The job's receipt, but only if that receipt still exists.

    `ScanJob.receipt_id` carries no foreign key, so deleting a receipt by hand
    leaves the id dangling. Every rule that turns on "this scan has a receipt"
    has to ask the receipts table, not the stale column, or the journal ends up
    guarding and linking to a row that is long gone.
    """
    from app.models.models import Receipt

    if not job.receipt_id:
        return None
    return job.receipt_id if db.get(Receipt, job.receipt_id) is not None else None


def delete_job(db: Session, job_id: str, *, with_receipt: bool = False) -> str | None:
    """Remove the scan-journal row and its stored input image.

    A scan that produced a receipt takes `with_receipt=True`, which deletes that
    receipt too — otherwise clearing a bad scan out of the journal would leave
    the wrong receipt behind, which is the part the user actually wanted gone.
    Returns the id of the receipt that went with it, if any.
    """
    job = get_job(db, job_id)
    receipt_id = live_receipt_id(db, job)
    if receipt_id and not with_receipt:
        raise ScanJobError("Це сканування має збережений чек — підтвердіть видалення разом із ним")
    _delete_row(db, job)
    db.commit()
    if receipt_id:
        _delete_receipt(db, receipt_id)
    return receipt_id


def normalized_from_draft(draft: dict, job_id: str):
    """Turn the low-confidence draft a scan left behind into a real receipt."""
    from app.adapters.base import NormalizedItem, NormalizedReceipt
    from app.core.time import as_utc

    purchased_at = None
    raw_purchased = draft.get("purchased_at")
    if raw_purchased:
        try:
            purchased_at = as_utc(dt.datetime.fromisoformat(str(raw_purchased)))
        except ValueError:
            purchased_at = None

    items = [
        NormalizedItem(
            name=str(row.get("name") or "Позиція"),
            total_price=float(row.get("total_price") or 0),
            quantity=float(row.get("quantity") or 1),
            unit_price=(float(row["unit_price"]) if row.get("unit_price") is not None else None),
            discount=float(row.get("discount") or 0),
        )
        for row in (draft.get("items") or [])
    ]
    total = draft.get("total_amount")
    return NormalizedReceipt(
        # Keyed to the scan, so accepting the same draft twice is idempotent.
        external_id=f"scan-review:{job_id}",
        purchased_at=purchased_at or dt.datetime.now(dt.timezone.utc),
        total_amount=(float(total) if total is not None else sum(i.total_price for i in items)),
        shop_name=draft.get("shop_name"),
        shop_address=draft.get("shop_address"),
        total_discount=(float(draft["total_discount"])
                        if draft.get("total_discount") is not None else None),
        items=items,
        raw={"source": "manual_review", "scan_job_id": job_id},
    )


def save_draft(db: Session, job_id: str):
    """Accept a `needs_review` draft as it stands and file it as a receipt.

    Reviewing means judging whether the draft is right; the fixing afterwards is
    the receipt page's job, which already edits shop, date, totals and lines. So
    this only has to get the draft into a receipt the user can then open.

    Returns (receipt, duplicate).
    """
    from sqlalchemy import select

    from app.api.inbox import _persist_receipt, _scanner_account
    from app.models.models import Receipt

    job = get_job(db, job_id)
    if live_receipt_id(db, job):
        raise ScanJobError("Цей чек уже збережено")
    draft = (job.raw_ocr or {}).get("draft")
    if not draft:
        raise ScanJobError("Для цього сканування немає чернетки")

    record_event(db, job, "draft", detail="чернетку прийнято вручну")
    nr = normalized_from_draft(draft, job_id)
    account = _scanner_account(db)
    duplicate = db.execute(select(Receipt.id).where(
        Receipt.account_id == account.id, Receipt.external_id == nr.external_id,
    )).scalar_one_or_none() is not None
    saved = _persist_receipt(db, account, nr)
    duplicate = duplicate or saved.external_id != nr.external_id
    attach_receipt(db, job_id, saved.id, method=f"{job.method or 'generic'} · вручну")
    return saved, duplicate


def enrich_receipt_from_scan(db: Session, job: ScanJob, receipt) -> bool:
    """Move onto the receipt what only the scan knows about it.

    Several receipts arrive by a route that never sees the photo — an imported
    ДПС XML, an accepted draft — while the scan they belong to holds the decoded
    ДПС link and the image itself. Whichever of the two sources lands second,
    the receipt should end up with everything both of them knew.

    Only fills gaps; never overwrites what the receipt already has.
    """
    from app.api.inbox import attach_scan_photo, scan_input_path

    changed = False
    fiscal_url = (job.raw_ocr or {}).get("fiscal_url")
    if fiscal_url and not receipt.receipt_url:
        receipt.receipt_url = str(fiscal_url)
        changed = True

    path = scan_input_path(job.id)
    if not receipt.photo_filename and path.is_file():
        data = path.read_bytes()
        attach_scan_photo(db, receipt, data, _image_type(data))
        changed = bool(receipt.photo_filename) or changed
    return changed


def _image_type(data: bytes) -> str:
    """Scan inputs are stored without their content type, and the photo is served
    by extension, so read the format out of the file's own signature."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypheim"):
        return "image/heic"
    return "image/jpeg"


def attach_receipt(db: Session, job_id: str, receipt_id: str, *, method: str) -> ScanJob | None:
    """Point a scan row at the receipt some other route produced for it.

    Importing a ДПС XML (or asking the API) creates the receipt outside the scan
    pipeline. Without this the journal row stays on `needs_review` with no link,
    so the user is told the receipt was saved while the screen keeps showing the
    photo as unresolved — and re-imports look like nothing happened.
    """
    from app.models.models import Receipt

    job = db.get(ScanJob, job_id)
    if job is None:
        return None
    job.receipt_id = receipt_id
    job.status = ScanStatus.parsed
    job.method = method
    job.error = None
    receipt = db.get(Receipt, receipt_id)
    if receipt is not None:
        enrich_receipt_from_scan(db, job, receipt)
    db.commit()
    record_event(db, job, "receipt", outcome="ok", actor="user",
                 detail=f"чек прикріплено ({method})")
    db.refresh(job)
    return job


def set_archived(db: Session, job_id: str, archived: bool) -> ScanJob:
    """Put a finished scan away (or bring it back) without losing the record.

    The stored image is kept either way, so an archived row can still be
    re-run — this only decides whether it shows in the working list.
    """
    job = get_job(db, job_id)
    job.archived_at = dt.datetime.now(dt.timezone.utc) if archived else None
    db.commit()
    record_event(db, job, "archive", detail="в архів" if archived else "з архіву")
    db.refresh(job)
    return job


def _delete_row(db: Session, job: ScanJob) -> None:
    from app.api.inbox import scan_input_path

    try:
        scan_input_path(job.id).unlink(missing_ok=True)
    except OSError:  # noqa: BLE001 — the journal row matters more than the file
        logger.debug("could not remove scan input for %s", job.id, exc_info=True)
    db.delete(job)


def _known_fiscal(job: ScanJob):
    """The fiscal QR an earlier pass on this image already decoded, if any.

    Decoding is the fragile step: a code that reads on one attempt may sit right
    at the edge of what the decoder manages. Once a URL has been recovered it is
    a fact about the photo, so a rerun is given it outright — that way it always
    starts from the authoritative ДПС path and can only gain on the last try.
    """
    from app.scanner.fiscal_qr import parse_fiscal_url

    url = (job.raw_ocr or {}).get("fiscal_url")
    return parse_fiscal_url(str(url)) if url else None


#: How many journal entries one scan keeps. A photo re-run a dozen times is
#: still one receipt, and the oldest attempts stop being interesting once the
#: newer ones exist.
EVENT_CAP = 60


def record_event(db: Session, job: "ScanJob | str | None", kind: str, *,
                 detail: str = "", outcome: str = "", ms: int = 0,
                 tokens_in: int = 0, tokens_out: int = 0, actor: str = "user") -> None:
    """Append one line to a scan's own history, with the time it happened.

    Everything that touched this receipt goes here, not just the pipeline:
    pressing "recognise with AI", importing a ДПС XML by hand, accepting a
    draft. Without the manual half the log answers "what did the machine do"
    while the user's question is "what happened to my receipt" — and an import
    that produced the receipt was invisible in it.

    Never raises: a journal entry must not be able to fail an import.
    """
    if isinstance(job, str):
        job = db.get(ScanJob, job)
    if job is None:
        return
    entry = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "actor": actor, "kind": kind, "outcome": outcome,
        "detail": detail[:300], "ms": int(ms),
        "tokens_in": int(tokens_in), "tokens_out": int(tokens_out),
    }
    raw = dict(job.raw_ocr or {})
    raw["events"] = [*(raw.get("events") or []), entry][-EVENT_CAP:]
    job.raw_ocr = raw          # reassigned, not mutated: the column is plain JSON
    try:
        db.commit()
    except Exception:  # noqa: BLE001 — logging must never break the action itself
        logger.warning("could not record scan event %s for %s", kind, job.id, exc_info=True)
        db.rollback()


def recognised_by(job: ScanJob) -> str:
    """Who read this photo, in the words the user is shown.

    "QR" and "AI: Google Gemini / gemini-…" mean very different things for how
    far a result can be trusted, so both UIs say which one it was instead of
    showing the internal method code.
    """
    method = (job.method or "").split(" ")[0]
    if method.startswith("series:"):
        method = method.split("·", 1)[-1]
    if method.startswith("template:"):
        return f"шаблон {method.split(':', 1)[1]}"
    if method == "ai":
        step = used_ai_step(job)
        return f"AI: {step[0]} / {step[1]}" if step else "AI"
    return {"qr": "QR", "tax_cabinet": "ДПС кабінет",
            "generic": "локальний розбір"}.get(method, "")


def used_ai_step(job: ScanJob) -> tuple[str, str] | None:
    """The (service, model) pair that last read this photo, if AI did.

    What "спробувати сильнішу AI" escalates from: the next step is looked up by
    this pair in the route as it stands now, so the user's own ordering decides
    what "stronger" means.
    """
    ai = (job.raw_ocr or {}).get("ai") or {}
    service, model = str(ai.get("service") or ""), str(ai.get("model") or "")
    return (service, model) if service and model else None


def _known_qr(job: ScanJob):
    """The QR verdict an earlier pass reached on this exact image.

    Handed back to the pipeline when the user asks for a stronger model: the
    ladder already ran over these bytes and can only reach the same answer, at
    the cost of seconds the user is sitting through.
    """
    from app.scanner.fiscal_qr import QR_DECODED, QrScan

    raw = job.raw_ocr or {}
    state = str(raw.get("qr_state") or "")
    url = raw.get("fiscal_url")
    if not state:
        return None
    return QrScan(state, [str(url)] if url and state == QR_DECODED else [])


async def rescan_job(db: Session, job_id: str, *, ai_first: bool = False,
                     replace: bool = False, escalate: bool = False):
    """Re-run the recognition pipeline over the job's stored image.

    Returns the fresh `UploadResult`. The run reuses the same journal row: one
    photo is one row whatever it took to read it, its event history accumulates
    instead of restarting, and — the reason this changed — the action buttons
    already sitting in a Telegram thread keep pointing at a row that exists. A
    rescan that minted a new id left those buttons answering "Сканування не
    знайдено" on the second press.

    The fiscal QR leads every run — it is stage 1 of the pipeline — and a URL an
    earlier pass already decoded is handed back in, so a rerun starts from the
    best QR result any attempt has produced rather than re-deriving it.

    `escalate` is the user asking for the next AI step up: the route resumes
    after the service+model already spent, and neither the QR ladder nor the
    step already used is run again.

    A job that already produced a receipt needs `replace=True` — an accidental
    click must not silently rewrite saved data. Even then the old receipt is
    dropped only once the new run has a better answer to put in its place: a
    rescan that comes back with nothing must not destroy what was already there.
    """
    from app.api.inbox import _handle_receipt, scan_input_path

    job = get_job(db, job_id)
    previous_receipt_id = live_receipt_id(db, job)
    if previous_receipt_id and not replace:
        raise ScanJobError("Чек уже збережено — щоб пересканувати, підтвердіть заміну")
    path = scan_input_path(job_id)
    if not path.is_file():
        raise ScanJobError("Оригінал зображення не збережено — завантажте фото ще раз")

    record_event(db, job, "rescan",
                 detail="сильніша AI" if escalate else ("через AI" if ai_first else "повторно"))
    result = await _handle_receipt(
        db, path.read_bytes(), job.source_filename, "image/jpeg",
        ai_first=ai_first or escalate,
        fiscal_override=_known_fiscal(job),
        ai_after=used_ai_step(job) if escalate else None,
        qr_scan=_known_qr(job) if escalate else None,
        job=job,
    )
    if result.scan_job_id != job_id:
        # Defensive: the run is handed this row, so it should come back on it.
        _delete_row(db, get_job(db, job_id))
        db.commit()

    if previous_receipt_id and result.receipt_id and result.receipt_id != previous_receipt_id:
        _delete_receipt(db, previous_receipt_id)
        result.replaced_receipt_id = previous_receipt_id
    elif previous_receipt_id and not result.receipt_id:
        # Nothing better came out — keep the saved receipt and say so, rather
        # than leaving the user with neither a receipt nor a warning.
        result.kept_receipt_id = previous_receipt_id
    return result


def _delete_receipt(db: Session, receipt_id: str) -> None:
    from app.api.receipts import PHOTO_DIR
    from app.models.models import Receipt

    receipt = db.get(Receipt, receipt_id)
    if receipt is None:
        return
    if receipt.photo_filename:
        (PHOTO_DIR / receipt.photo_filename).unlink(missing_ok=True)
    db.delete(receipt)
    db.commit()
