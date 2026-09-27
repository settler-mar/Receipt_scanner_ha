from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.models import Store
from app.schemas.raw_responses import (
    RawResponseCleanup,
    RawResponseCleanupResult,
    RawResponseSettings,
    RawResponseStoreSummary,
    RawResponseSummary,
)
from app.services import raw_responses

router = APIRouter(prefix="/api/raw-responses", tags=["raw-responses"])


def _summary(db: Session) -> RawResponseSummary:
    result = raw_responses.summary()
    names = {row.code: row.name for row in db.execute(select(Store)).scalars().all()}
    return RawResponseSummary(
        total_count=result["total_count"],
        total_bytes=result["total_bytes"],
        stores=[RawResponseStoreSummary(**row, store_name=names.get(row["store_code"])) for row in result["stores"]],
    )


@router.get("/summary", response_model=RawResponseSummary)
def get_summary(db: Session = Depends(get_db)):
    return _summary(db)


@router.get("/settings", response_model=RawResponseSettings)
def get_settings(db: Session = Depends(get_db)):
    return raw_responses.load_settings(db)


@router.put("/settings", response_model=RawResponseSettings)
def put_settings(payload: RawResponseSettings, db: Session = Depends(get_db)):
    return raw_responses.save_settings(db, payload.retention_days, payload.auto_cleanup)


@router.post("/cleanup", response_model=RawResponseCleanupResult)
def cleanup(payload: RawResponseCleanup, db: Session = Depends(get_db)):
    removed = (
        raw_responses.cleanup_all()
        if payload.mode == "all"
        else raw_responses.cleanup_older_than(raw_responses.load_settings(db)["retention_days"])
    )
    return RawResponseCleanupResult(removed=removed)
