"""Stored voice messages: listen to one, recognise it again, delete it.

The bot keeps every dictated message as audio even when speech-to-text is off
(app/services/voice.py). These routes are what the inbox page uses to play one
back, run recognition when it is configured, and act on the text.
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.time import as_utc
from app.db import get_db
from app.models.scanner import VoiceMessage
from app.services import ai, stt, voice

router = APIRouter(prefix="/api/voice", tags=["voice"])


class LanguageOut(BaseModel):
    code: str
    label: str


class VoiceOut(BaseModel):
    id: str
    source: str
    status: str
    duration_s: int | None
    size_bytes: int | None
    content_type: str | None
    transcript: str | None
    language: str | None
    error: str | None
    applied: str | None
    created_at: dt.datetime
    transcribed_at: dt.datetime | None
    audio_url: str
    # False when speech-to-text is not configured — the page then says so instead
    # of offering a button that can only fail.
    stt_ready: bool
    # Languages this recording can be recognised in, primary first — the page
    # turns the ones already tried into "спробувати ще раз" buttons.
    languages: list[LanguageOut]


def out(row: VoiceMessage, *, ready: bool, languages: list[LanguageOut]) -> VoiceOut:
    return VoiceOut(
        id=row.id, source=row.source,
        status=row.status.value if hasattr(row.status, "value") else str(row.status),
        duration_s=row.duration_s, size_bytes=row.size_bytes,
        content_type=row.content_type, transcript=row.transcript,
        language=row.language, error=row.error,
        applied=row.applied, created_at=as_utc(row.created_at),
        transcribed_at=as_utc(row.transcribed_at) if row.transcribed_at else None,
        audio_url=f"/api/voice/{row.id}/audio", stt_ready=ready,
        languages=languages,
    )


def _languages(db: Session) -> list[LanguageOut]:
    """Configured dictation languages, primary first."""
    return [LanguageOut(code=c, label=ai.speech_language_label(c))
            for c in stt.language_options(db)]


def _row(db: Session, voice_id: str) -> VoiceMessage:
    row = voice.get(db, voice_id)
    if row is None:
        raise HTTPException(404, "Голосове повідомлення не знайдено")
    return row


@router.get("", response_model=list[VoiceOut])
def list_voice(limit: int = 50, db: Session = Depends(get_db)):
    ready = stt.stt_ready(db)
    languages = _languages(db)
    return [out(row, ready=ready, languages=languages) for row in voice.recent(db, limit)]


@router.get("/{voice_id}/audio")
def audio(voice_id: str, db: Session = Depends(get_db)):
    row = _row(db, voice_id)
    path = voice.path(row)
    if not path.exists():
        raise HTTPException(404, "Аудіо цього повідомлення більше немає на диску")
    return FileResponse(path, media_type=row.content_type or "audio/ogg")


@router.post("/{voice_id}/transcribe", response_model=VoiceOut)
async def transcribe(voice_id: str, apply: bool = True, language: str | None = None,
                     db: Session = Depends(get_db)):
    """Recognise the audio and, unless `apply=false`, act on what was said —
    the same reading the bot does (receipt draft / refuelling / shopping list).

    `language` (BCP-47) forces one of the configured dictation languages instead
    of the primary one — the retry for a phrase said in the other language."""
    row = _row(db, voice_id)
    language = (language or "").strip() or None
    if language and language not in stt.language_options(db):
        raise HTTPException(400, f"Мова {language} не налаштована для розпізнавання")
    try:
        text = await voice.transcribe(db, row, language=language)
    except stt.SttError as exc:
        raise HTTPException(409, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    if apply:
        voice.note_applied(db, row, await apply_transcript(db, text))
    return out(row, ready=stt.stt_ready(db), languages=_languages(db))


@router.post("/{voice_id}/apply", response_model=VoiceOut)
async def apply_again(voice_id: str, db: Session = Depends(get_db)):
    """Read an already recognised transcript again — useful after fixing the AI
    settings, without paying for transcription a second time."""
    row = _row(db, voice_id)
    if not row.transcript:
        raise HTTPException(409, "Це повідомлення ще не розпізнане")
    voice.note_applied(db, row, await apply_transcript(db, row.transcript))
    return out(row, ready=stt.stt_ready(db), languages=_languages(db))


async def apply_transcript(db: Session, text: str) -> str:
    """Run the captured phrase through the same path the bot uses and describe
    the outcome in one line."""
    from app.services import ai, capture

    try:
        draft = await capture.parse_capture(db, text)
    except ai.AiConfigError as exc:
        return f"AI недоступний: {exc}"
    if draft.intent == "receipt":
        job = capture.create_receipt_job(db, draft, method="voice")
        return f"чернетка чека ({len(draft.receipt.items)} позицій) — {job.id}"
    if draft.intent == "fuel":
        try:
            res = capture.record_fuel(db, draft)
        except ValueError as exc:
            return f"заправка не записана: {exc}"
        return f"заправка: {res['store_name']} · {res['liters']} л · {res['total_amount']} грн"
    if draft.intent == "shopping":
        rows = capture.add_shopping_items(db, draft)
        return f"список покупок: {len(rows)} позицій"
    return "не схоже ні на чек, ні на список покупок"


@router.delete("/{voice_id}", status_code=204)
def delete_voice(voice_id: str, db: Session = Depends(get_db)):
    voice.delete(db, _row(db, voice_id))
