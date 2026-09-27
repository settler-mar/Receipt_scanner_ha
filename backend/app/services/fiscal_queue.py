from __future__ import annotations

import datetime as dt
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.models import Receipt
from app.models.scanner import FiscalQueueJob, FiscalQueueStatus, ScanJob
from app.scanner.fiscal_qr import parse_fiscal_url
from app.scanner.qr.fiscal_url import fiscal_identity
from app.services.manual_dedup import ingestion_lock


def enqueue(db: Session, url: str, *, source: str = "admin", chat_id: int | None = None, scan_job_id: str | None = None) -> FiscalQueueJob:
    qr = parse_fiscal_url(url)
    if qr is None or not qr.dedup_key:
        raise ValueError("Некоректне посилання на фіскальний чек ДПС")
    with ingestion_lock():
        job = db.execute(select(FiscalQueueJob).where(FiscalQueueJob.dedup_key == qr.dedup_key)).scalar_one_or_none()
        if job is None:
            job = FiscalQueueJob(url=qr.raw_url, dedup_key=qr.dedup_key, source=source,
                                 telegram_chat_id=str(chat_id) if chat_id is not None else None, scan_job_id=scan_job_id)
            db.add(job)
        elif job.status != FiscalQueueStatus.imported:
            job.url = qr.raw_url
            # Do not re-arm the API attempt for duplicate Telegram updates: that
            # could silently drain the DPS daily quota while the service is blocked.
            if chat_id is not None:
                job.telegram_chat_id = str(chat_id)
            if scan_job_id: job.scan_job_id = scan_job_id
        db.commit(); db.refresh(job)
        return job


def touch_attempt(db: Session, job: FiscalQueueJob) -> None:
    job.attempts += 1
    job.updated_at = dt.datetime.now(dt.timezone.utc)
    db.commit()


# queued/captcha/failed = ДПС still owes us this receipt.
OPEN_STATUSES = (FiscalQueueStatus.queued, FiscalQueueStatus.captcha,
                 FiscalQueueStatus.failed)


def open_jobs(db: Session) -> list[FiscalQueueJob]:
    """Queue rows that still need something from the user or from ДПС.

    A job started from a photo stops being open the moment that photo is read
    some other way (QR ladder, AI, manual draft): the receipt is already in the
    database, and chasing the tax API for a copy is pointless. Those rows used
    to sit in the queue forever and inflate every counter.
    """
    rows = db.execute(
        select(FiscalQueueJob).where(FiscalQueueJob.status.in_(OPEN_STATUSES))
        .order_by(FiscalQueueJob.created_at.desc())
    ).scalars().all()
    return [job for job in rows if receipt_from_elsewhere(db, job) is None]


def receipt_from_elsewhere(db: Session, job: FiscalQueueJob) -> str | None:
    """Receipt id for this queue row that we already have, whatever brought it.

    Checked in order of how certain the match is:
      1. the job's own receipt;
      2. the receipt its scan produced (QR ladder, AI, manual draft);
      3. a receipt carrying the same ДПС document identity — either as its
         `external_id` (`fiscal:<registrar>:<document>`) or inside its stored
         tax-cabinet link, which is how a cabinet XML import is saved;
      4. failing an identity, a receipt at the same minute for the same amount
         as the QR itself states.
    """
    if job.receipt_id:
        return job.receipt_id
    if job.scan_job_id:
        scan = db.get(ScanJob, job.scan_job_id)
        if scan is not None and scan.receipt_id:
            return scan.receipt_id

    qr = parse_fiscal_url(job.url)
    if qr is None:
        return None

    if qr.dedup_key:
        rows = db.execute(select(Receipt).where(or_(
            Receipt.external_id == qr.dedup_key,
            Receipt.receipt_url.contains(qr.document_id or "\u0000"),
        ))).scalars().all()
        for receipt in rows:
            if (receipt.external_id == qr.dedup_key
                    or fiscal_identity(receipt.receipt_url) == qr.dedup_key):
                return receipt.id

    if qr.issued_at and qr.total_amount:
        window = dt.timedelta(minutes=5)
        receipt = db.execute(select(Receipt).where(
            Receipt.purchased_at >= qr.issued_at - window,
            Receipt.purchased_at <= qr.issued_at + window,
            Receipt.total_amount == qr.total_amount,
        )).scalars().first()
        if receipt is not None:
            return receipt.id
    return None


def close_obsolete(db: Session) -> int:
    """Close queue rows whose receipt turned up another way. Returns how many.

    Called from the scheduler tick: it also stops the retry loop from spending
    the ДПС daily quota on receipts we already have.
    """
    closed = 0
    for job in db.execute(
        select(FiscalQueueJob).where(FiscalQueueJob.status.in_(OPEN_STATUSES))
    ).scalars().all():
        receipt_id = receipt_from_elsewhere(db, job)
        if receipt_id is None:
            continue
        job.receipt_id = receipt_id
        job.status = FiscalQueueStatus.imported
        job.error = "Чек уже отримано іншим способом — черга ДПС закрита"
        closed += 1
    if closed:
        db.commit()
    return closed
