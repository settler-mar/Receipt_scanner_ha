"""Stored voice messages: keep the audio, recognise it separately.

Recognition can be off, unconfigured, over budget or simply wrong, and a dictated
purchase is often said once and never repeated. So every voice note is written to
disk before anything is attempted with it, and transcription is a step that can be
run again later — from the web inbox or from the chat it arrived in.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import BASE_DIR
from app.models.scanner import VoiceMessage, VoiceStatus
from app.services import stt

logger = logging.getLogger(__name__)

VOICE_DIR = BASE_DIR / "data" / "voice"

# Telegram's own container for a voice note; anything else keeps its extension.
_EXTENSIONS = {
    "audio/ogg": ".ogg", "audio/opus": ".ogg", "audio/oga": ".ogg",
    "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/webm": ".webm",
}


def _extension(content_type: str | None, filename: str | None) -> str:
    if filename and "." in Path(filename).name:
        return Path(filename).suffix
    return _EXTENSIONS.get((content_type or "").split(";")[0].strip().lower(), ".ogg")


def store(
    db: Session,
    data: bytes,
    *,
    content_type: str | None = "audio/ogg",
    duration_s: int | None = None,
    source: str = "telegram",
    chat_id: int | str | None = None,
    message_id: int | None = None,
    filename: str | None = None,
) -> VoiceMessage:
    """Write the audio to `data/voice/` and record it. Never raises for a missing
    recogniser — storing is deliberately independent of transcription."""
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4()}{_extension(content_type, filename)}"
    (VOICE_DIR / name).write_bytes(data)
    row = VoiceMessage(
        source=source,
        chat_id=str(chat_id) if chat_id is not None else None,
        message_id=message_id,
        local_filename=name,
        content_type=content_type,
        duration_s=int(duration_s) if duration_s else None,
        size_bytes=len(data),
        status=VoiceStatus.stored,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get(db: Session, voice_id: str) -> VoiceMessage | None:
    return db.get(VoiceMessage, voice_id)


def recent(db: Session, limit: int = 50, *, include_archived: bool = False) -> list[VoiceMessage]:
    query = select(VoiceMessage)
    if not include_archived:
        query = query.where(VoiceMessage.archived_at.is_(None))
    return list(db.execute(
        query.order_by(VoiceMessage.created_at.desc()).limit(limit)
    ).scalars())


def set_archived(db: Session, row: VoiceMessage, archived: bool) -> VoiceMessage:
    """Put a handled recording away (or bring it back). The audio is kept either
    way, so an archived message can still be played and recognised again."""
    row.archived_at = dt.datetime.now(dt.timezone.utc) if archived else None
    db.commit()
    db.refresh(row)
    return row


def open_count(db: Session) -> int:
    """Recordings still waiting for the user: not archived and not yet acted on."""
    return db.execute(
        select(func.count()).select_from(VoiceMessage).where(
            VoiceMessage.archived_at.is_(None),
            VoiceMessage.applied.is_(None),
        )
    ).scalar_one()


def path(row: VoiceMessage) -> Path:
    return VOICE_DIR / row.local_filename


def audio_bytes(row: VoiceMessage) -> bytes:
    file = path(row)
    if not file.exists():
        raise FileNotFoundError("аудіо цього повідомлення більше немає на диску")
    return file.read_bytes()


def delete(db: Session, row: VoiceMessage) -> None:
    path(row).unlink(missing_ok=True)
    db.delete(row)
    db.commit()


async def transcribe(db: Session, row: VoiceMessage, *, language: str | None = None) -> str:
    """Run speech-to-text on a stored message and remember the outcome.

    `language` overrides the configured one — that is the "спробувати іншою
    мовою" retry, for a Ukrainian recogniser that turned a Russian sentence into
    nonsense (or the other way round). The code used is kept on the row so the
    next retry can offer the languages that have not been tried yet.

    Raises `stt.SttError` with a message meant for the user; the failure is kept
    on the row so the list shows why nothing came out and the retry button has
    something to explain.
    """
    # Large voice notes can make a blocking filesystem read visible to every
    # request on the event loop.
    data = await asyncio.to_thread(audio_bytes, row)
    used = (language or "").strip() or (stt.language_options(db) or ["uk-UA"])[0]
    try:
        text = await stt.transcribe(
            db, data, filename=row.local_filename,
            media_type=row.content_type or "audio/ogg", duration_s=row.duration_s,
            language=language,
        )
    except stt.SttError as exc:
        row.status = VoiceStatus.failed
        row.error = str(exc)
        row.language = used
        db.commit()
        raise
    row.language = used
    row.transcript = text
    row.status = VoiceStatus.transcribed
    row.error = None
    row.transcribed_at = dt.datetime.now(dt.timezone.utc)
    db.commit()
    db.refresh(row)
    return text


def note_applied(db: Session, row: VoiceMessage, what: str | None) -> None:
    """Record, in words, what the transcript was turned into."""
    row.applied = what
    db.commit()
