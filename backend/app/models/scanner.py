"""Scan-journal + document-inbox tables (ROADMAP.md Крок 3).

Own module (not app/models/models.py) so it can be built while Крок 1/2 are
still in flight on that file — same rationale as app/models/shopping.py. Must be
imported before `Base.metadata.create_all` / Alembic autogenerate — see
app/main.py.
"""
from __future__ import annotations

import datetime as dt
import enum
import uuid

from sqlalchemy import JSON, DateTime, Enum, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class ScanStatus(str, enum.Enum):
    pending = "pending"            # accepted, pipeline not finished
    parsed = "parsed"             # reconciled and a Receipt was created
    needs_review = "needs_review"  # a low-confidence draft awaits the user
    failed = "failed"            # nothing usable came out


class ScanJob(Base):
    """One uploaded receipt photo and how the pipeline handled it — the log that
    backs the "share of scans that reached AI" statistic (ROADMAP.md "Готово,
    коли")."""

    __tablename__ = "scan_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    source_filename: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[ScanStatus] = mapped_column(
        Enum(ScanStatus), default=ScanStatus.pending, index=True
    )
    # "qr" | "tax_cabinet" | "template:<code>" | "generic" | "ai" | "none"
    method: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    confidence: Mapped[str | None] = mapped_column(String, nullable=True)  # high | low | none
    # FK-less on purpose (same reasoning as shopping.py) — a deleted receipt just
    # leaves a dangling id that reads back as "receipt gone".
    receipt_id: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_ocr: Mapped[dict] = mapped_column(JSON, default=dict)  # notes + ocr excerpt + fiscal url
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # Set when the user is done with this row and doesn't want it in the working
    # list any more. The record and its stored image stay, so it can still be
    # re-run later; null = still showing in the inbox.
    archived_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class ScanSample(Base):
    """Raw OCR layout of a receipt that only made it through the generic or AI
    stage — kept so a proper template can be authored later and that chain stops
    costing AI calls (ROADMAP.md Крок 3, "Бонус")."""

    __tablename__ = "scan_samples"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    method: Mapped[str] = mapped_column(String)          # generic | ai
    store_guess: Mapped[str | None] = mapped_column(String, nullable=True)
    ocr_text: Mapped[str | None] = mapped_column(String, nullable=True)
    structured: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class FiscalQueueStatus(str, enum.Enum):
    queued = "queued"
    processing = "processing"
    captcha = "captcha"
    imported = "imported"
    failed = "failed"


class FiscalQueueJob(Base):
    """Durable history/queue for fiscal URLs that DPS could not return at once."""

    __tablename__ = "fiscal_queue_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    url: Mapped[str] = mapped_column(String)
    dedup_key: Mapped[str] = mapped_column(String, unique=True, index=True)
    source: Mapped[str] = mapped_column(String, default="admin")
    telegram_chat_id: Mapped[str | None] = mapped_column(String, nullable=True)
    scan_job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[FiscalQueueStatus] = mapped_column(
        Enum(FiscalQueueStatus), default=FiscalQueueStatus.queued, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    receipt_id: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class InboxStatus(str, enum.Enum):
    unprocessed = "unprocessed"
    done = "done"


class InboxDocument(Base):
    """A wide (non-receipt) upload waiting to be named and filed. The bytes live
    on Google Drive (`drive_config.inbox_folder_id`) when Drive is configured,
    otherwise in a local holding folder — `storage` says which. This branch must
    never touch app.services.ai (ROADMAP.md Крок 3)."""

    __tablename__ = "inbox_documents"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    storage: Mapped[str] = mapped_column(String, default="local")  # local | drive
    drive_file_id: Mapped[str | None] = mapped_column(String, nullable=True)
    local_filename: Mapped[str | None] = mapped_column(String, nullable=True)
    original_filename: Mapped[str | None] = mapped_column(String, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[InboxStatus] = mapped_column(
        Enum(InboxStatus), default=InboxStatus.unprocessed, index=True
    )
    target_folder_id: Mapped[str | None] = mapped_column(String, nullable=True)
    final_name: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    processed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VoiceStatus(str, enum.Enum):
    stored = "stored"            # audio kept, not recognised (yet or at all)
    transcribed = "transcribed"  # speech-to-text produced a text
    failed = "failed"            # recognition was attempted and did not work


class VoiceMessage(Base):
    """A dictated message, kept as audio regardless of what recognition did.

    Speech-to-text can be switched off, unconfigured or simply wrong, and a voice
    note is often the only record of a purchase — so the bytes are stored first
    and recognition is a separate, repeatable step (listen to it, run it again).
    """

    __tablename__ = "voice_messages"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    source: Mapped[str] = mapped_column(String, default="telegram")
    chat_id: Mapped[str | None] = mapped_column(String, nullable=True)
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    local_filename: Mapped[str] = mapped_column(String)
    content_type: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[VoiceStatus] = mapped_column(
        Enum(VoiceStatus), default=VoiceStatus.stored, index=True
    )
    transcript: Mapped[str | None] = mapped_column(String, nullable=True)
    # BCP-47 code the transcript came out of ("uk-UA"), so the list can say what
    # was heard as what and offer the other language as a retry.
    language: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    # What the transcript was turned into, in words ("список покупок: 3 позиції") —
    # enough to see from the list whether the message was already acted on.
    applied: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    transcribed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Same idea as ScanJob.archived_at: the audio and the record stay, the row
    # just leaves the working list once the user is done with it.
    archived_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
