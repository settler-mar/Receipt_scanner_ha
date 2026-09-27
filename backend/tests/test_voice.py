"""Stored voice messages: the audio outlives recognition.

Speech-to-text is mocked; what is tested is that the recording is kept whatever
recognition does, can be played back and recognised again later.
"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app
from app.models import models  # noqa: F401 — registers receipts/stores tables
from app.models import scanner  # noqa: F401 — registers voice_messages
from app.models import shopping  # noqa: F401 — registers shopping tables
from app.models.scanner import VoiceMessage, VoiceStatus
from app.services import stt, voice
from app.api.voice import _shopping_item_label


def test_voice_shopping_action_contains_item_details():
    assert _shopping_item_label(SimpleNamespace(
        raw_text="молоко", qty=2, unit="л", note="для кави",
    )) == "молоко — 2 л (для кави)"
    assert _shopping_item_label(SimpleNamespace(
        raw_text="хліб", qty=1, unit=None, note=None,
    )) == "хліб"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'voice.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(voice, "VOICE_DIR", tmp_path / "voice")
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture()
def client(db):
    app.dependency_overrides[get_db] = lambda: db()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_audio_is_stored_before_anything_is_recognised(db):
    with db() as session:
        row = voice.store(session, b"opus-bytes", content_type="audio/ogg",
                          duration_s=7, chat_id=42, message_id=5)
        assert row.status is VoiceStatus.stored
        assert row.size_bytes == len(b"opus-bytes")
        assert row.chat_id == "42"
        assert voice.path(row).suffix == ".ogg"
        assert voice.audio_bytes(row) == b"opus-bytes"


def test_failed_recognition_keeps_the_recording_and_the_reason(db, monkeypatch):
    async def fake(db_, audio, **kwargs):
        raise stt.SttError("Розпізнавання голосу вимкнено")

    monkeypatch.setattr(stt, "transcribe", fake)
    with db() as session:
        row = voice.store(session, b"opus")
        with pytest.raises(stt.SttError):
            asyncio.run(voice.transcribe(session, row))
        assert row.status is VoiceStatus.failed
        assert "вимкнено" in row.error
        assert voice.path(row).exists()


def test_transcription_can_be_run_again_from_the_api(client, db, monkeypatch):
    calls = []

    async def fake(db_, audio, **kwargs):
        calls.append(audio)
        return "додай в список покупок молоко"

    async def fake_apply(db_, text):
        return "список покупок: 1 позицій"

    monkeypatch.setattr(stt, "transcribe", fake)
    monkeypatch.setattr("app.api.voice.apply_transcript", fake_apply)
    with db() as session:
        row_id = voice.store(session, b"opus", duration_s=4).id

    listed = client.get("/api/voice").json()
    assert [r["id"] for r in listed] == [row_id]
    assert listed[0]["status"] == "stored"
    assert listed[0]["audio_url"] == f"/api/voice/{row_id}/audio"

    out = client.post(f"/api/voice/{row_id}/transcribe").json()
    assert out["status"] == "transcribed"
    assert out["transcript"] == "додай в список покупок молоко"
    assert out["applied"] == "список покупок: 1 позицій"
    assert len(calls) == 1

    # Re-reading an existing transcript must not pay for recognition again.
    again = client.post(f"/api/voice/{row_id}/apply").json()
    assert again["applied"] == "список покупок: 1 позицій"
    assert len(calls) == 1


def test_the_same_recording_can_be_retried_in_another_language(client, db, monkeypatch):
    """A phrase said in Russian to a Ukrainian recogniser comes out as nonsense —
    the fix is to run the stored audio again, naming the language."""
    asked: list[str | None] = []

    async def fake(db_, audio, **kwargs):
        asked.append(kwargs.get("language"))
        return "добавь в список молоко"

    async def fake_apply(db_, text):
        return "список покупок: 1 позицій"

    monkeypatch.setattr(stt, "transcribe", fake)
    monkeypatch.setattr("app.api.voice.apply_transcript", fake_apply)
    with db() as session:
        row_id = voice.store(session, b"opus").id

    out = client.post(f"/api/voice/{row_id}/transcribe?language=ru-RU").json()
    assert asked == ["ru-RU"]
    # The row remembers what it was heard as, so the page can offer the rest.
    assert out["language"] == "ru-RU"
    assert [lang["code"] for lang in out["languages"]] == ["uk-UA", "ru-RU"]

    # A language nobody configured never reaches the provider.
    refused = client.post(f"/api/voice/{row_id}/transcribe?language=de-DE")
    assert refused.status_code == 400
    assert asked == ["ru-RU"]


def test_audio_can_be_played_back_and_deleted(client, db):
    with db() as session:
        row_id = voice.store(session, b"opus", content_type="audio/ogg").id

    audio = client.get(f"/api/voice/{row_id}/audio")
    assert audio.status_code == 200
    assert audio.content == b"opus"
    assert audio.headers["content-type"].startswith("audio/ogg")

    assert client.delete(f"/api/voice/{row_id}").status_code == 204
    with db() as session:
        assert session.get(VoiceMessage, row_id) is None
    assert client.get(f"/api/voice/{row_id}/audio").status_code == 404


def test_transcribe_reports_a_disabled_recogniser_as_a_conflict(client, db, monkeypatch):
    async def fake(db_, audio, **kwargs):
        raise stt.SttError("Розпізнавання голосу вимкнено")

    monkeypatch.setattr(stt, "transcribe", fake)
    with db() as session:
        row_id = voice.store(session, b"opus").id
    resp = client.post(f"/api/voice/{row_id}/transcribe")
    assert resp.status_code == 409
    assert "вимкнено" in resp.json()["detail"]


def test_a_recording_can_be_archived_and_brought_back(client, db):
    """Same archive as the scan journal: the audio stays, the row just leaves
    the working list — otherwise a handled message sits in the inbox (and in the
    sidebar counter) forever."""
    with db() as session:
        row_id = voice.store(session, b"opus", content_type="audio/ogg").id

    assert [r["id"] for r in client.get("/api/voice").json()] == [row_id]

    archived = client.post(f"/api/voice/{row_id}/archive").json()
    assert archived["archived_at"] is not None
    assert client.get("/api/voice").json() == []                      # out of the queue
    assert [r["id"] for r in client.get("/api/voice?include_archived=true").json()] == [row_id]
    assert client.get(f"/api/voice/{row_id}/audio").status_code == 200  # audio kept

    back = client.post(f"/api/voice/{row_id}/archive?archived=false").json()
    assert back["archived_at"] is None
    assert [r["id"] for r in client.get("/api/voice").json()] == [row_id]


def test_open_count_ignores_archived_and_handled_recordings(db):
    with db() as session:
        waiting = voice.store(session, b"a", content_type="audio/ogg")
        handled = voice.store(session, b"b", content_type="audio/ogg")
        handled.applied = "список покупок: 2 позиції"
        put_away = voice.store(session, b"c", content_type="audio/ogg")
        voice.set_archived(session, put_away, True)
        session.commit()

        assert voice.open_count(session) == 1
        voice.set_archived(session, waiting, True)
        assert voice.open_count(session) == 0
