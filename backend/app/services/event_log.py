"""Writer for the unified operational journal (`app_events`).

Read side lives in `api/logs.py`, which merges this table with the histories
other subsystems already keep. Write here whenever something happens in the
background that the user would otherwise never see — a scheduled job doing
work, an automatic action taken on their data, a maintenance failure.

Never raises: a journal entry must not be able to break the job it describes.
"""
from __future__ import annotations

import logging

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.models import AppEvent

logger = logging.getLogger(__name__)

INFO, WARNING, ERROR = "info", "warning", "error"

# Keep the table from growing without bound; trimmed opportunistically on write.
_MAX_ROWS = 5000
_TRIM_EVERY = 200
_writes_since_trim = 0


def record(db: Session, *, source: str, title: str, message: str = "",
           level: str = INFO, details: dict | None = None) -> None:
    """Append one journal line and commit it. Safe to call from any job."""
    global _writes_since_trim
    try:
        db.add(AppEvent(source=source, level=level, title=title,
                        message=message or None, details=details or {}))
        db.commit()
        _writes_since_trim += 1
        if _writes_since_trim >= _TRIM_EVERY:
            _writes_since_trim = 0
            _trim(db)
    except Exception:  # noqa: BLE001 — logging must never break the caller
        db.rollback()
        logger.warning("Could not write app event %s/%s", source, title, exc_info=True)


def _trim(db: Session) -> None:
    ids = db.execute(
        select(AppEvent.id).order_by(AppEvent.created_at.desc()).offset(_MAX_ROWS)
    ).scalars().all()
    if ids:
        db.execute(delete(AppEvent).where(AppEvent.id.in_(ids)))
        db.commit()
