from __future__ import annotations

import datetime as dt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.scanner import FiscalQueueJob, FiscalQueueStatus
from app.scanner.fiscal_qr import parse_fiscal_url
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
