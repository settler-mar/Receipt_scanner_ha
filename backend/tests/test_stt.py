"""Voice messages → text (CAPTURE_PLAN.md, крок 3).

The provider is never called for real: what matters here is that voice stays off
until it is configured, that oversized or overlong audio is refused before it
costs an upload, and that the multipart request carries the right model and file.
"""
import asyncio
import base64

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import models  # noqa: F401 — registers app_settings
from app.services import ai, stt


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'stt.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


def _configure(db, *, provider="openai", model="gpt-4o-transcribe", key="sk-test"):
    cfg = ai.load_config(db)
    cfg.speech.enabled = True
    cfg.speech.provider = provider
    cfg.speech.model = model
    ai.save_config(db, cfg)
    if key:
        ai.set_cloud_key(db, provider, key)
    return ai.load_config(db)


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class _FakeClient:
    """Captures the one multipart POST transcription makes."""

    def __init__(self, response, sink):
        self._response = response
        self._sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, data=None, files=None, json=None, params=None):
        self._sink.update(url=url, headers=headers or {}, data=data or {},
                          files=files or {}, json=json or {}, params=params or {})
        return self._response

    async def get(self, url, params=None, headers=None):
        self._sink.update(url=url, params=params or {}, headers=headers or {})
        return self._response


def _patch_client(monkeypatch, response):
    sink: dict = {}
    monkeypatch.setattr(stt.httpx, "AsyncClient",
                        lambda *a, **kw: _FakeClient(response, sink))
    return sink


def test_voice_is_off_until_a_provider_is_chosen(db):
    assert stt.stt_ready(db) is False
    with pytest.raises(stt.SttError, match="вимкнено"):
        asyncio.run(stt.transcribe(db, b"ogg"))


def test_anthropic_cannot_be_used_for_transcription(db):
    _configure(db, provider="anthropic", key="sk-ant")
    # Chat-only wire protocol — there is no /audio/transcriptions to call.
    assert stt.stt_ready(db) is False
    with pytest.raises(stt.SttError, match="транскрипції"):
        asyncio.run(stt.transcribe(db, b"ogg"))


def test_ready_once_provider_model_and_key_are_set(db):
    _configure(db)
    assert stt.stt_ready(db) is True


def test_oversized_and_overlong_audio_are_refused(db):
    _configure(db)
    with pytest.raises(stt.SttError, match="завелике"):
        asyncio.run(stt.transcribe(db, b"x" * (stt.MAX_AUDIO_BYTES + 1)))
    with pytest.raises(stt.SttError, match="коротше"):
        asyncio.run(stt.transcribe(db, b"ogg", duration_s=stt.MAX_DURATION_S + 1))


def test_transcription_posts_the_audio_and_returns_the_text(db, monkeypatch):
    _configure(db)
    sink = _patch_client(monkeypatch, _FakeResponse(payload={"text": " додай чек у бабули "}))

    out = asyncio.run(stt.transcribe(db, b"opus-bytes", filename="voice.ogg",
                                     media_type="audio/ogg", duration_s=7))
    assert out == "додай чек у бабули"
    assert sink["url"] == "https://api.openai.com/v1/audio/transcriptions"
    assert sink["headers"]["Authorization"] == "Bearer sk-test"
    assert sink["data"]["model"] == "gpt-4o-transcribe"
    assert sink["files"]["file"] == ("voice.ogg", b"opus-bytes", "audio/ogg")


def test_language_options_list_the_primary_first(db):
    _configure(db)
    cfg = ai.load_config(db)
    cfg.speech.language = "ru-RU"
    cfg.speech.alternative_languages = ["uk-UA", "ru-RU", "", "en-US"]
    ai.save_config(db, cfg)
    # The primary never repeats among the retries, and blanks are dropped.
    assert stt.language_options(db) == ["ru-RU", "uk-UA", "en-US"]


def test_openai_gets_the_language_it_was_asked_for(db, monkeypatch):
    """The "іншою мовою" retry: the recording is the same, the language is not."""
    _configure(db)
    sink = _patch_client(monkeypatch, _FakeResponse(payload={"text": "добавь чек"}))

    out = asyncio.run(stt.transcribe(db, b"opus", language="ru-RU"))
    assert out == "добавь чек"
    # OpenAI takes ISO-639-1, not the full BCP-47 tag.
    assert sink["data"]["language"] == "ru"


def test_provider_error_is_reported_and_logged(db, monkeypatch):
    _configure(db)
    _patch_client(monkeypatch, _FakeResponse(
        status_code=401, payload={"error": {"message": "Incorrect API key"}}))

    with pytest.raises(stt.SttError, match="Incorrect API key"):
        asyncio.run(stt.transcribe(db, b"opus"))
    # The failure is visible in the AI activity log, like any other model call.
    entry = ai.recent_log(db)[0]
    assert entry["task"] == "stt" and entry["ok"] is False


def test_silence_is_reported_rather_than_returned_empty(db, monkeypatch):
    _configure(db)
    _patch_client(monkeypatch, _FakeResponse(payload={"text": "   "}))
    with pytest.raises(stt.SttError, match="жодного слова"):
        asyncio.run(stt.transcribe(db, b"opus"))


# --------------------------------------------------------------------------- #
# Google Cloud Speech-to-Text
# --------------------------------------------------------------------------- #
def _configure_google(db, key="AIzaTestKey", model="latest_long"):
    cfg = ai.load_config(db)
    cfg.speech.enabled = True
    cfg.speech.provider = ai.GOOGLE_STT
    cfg.speech.model = model
    cfg.speech.language = "uk-UA"
    cfg.speech.alternative_languages = ["ru-RU"]
    ai.save_config(db, cfg)
    if key:
        ai.set_cloud_key(db, ai.GOOGLE_STT, key)
    return ai.load_config(db)


def test_google_needs_its_own_key(db):
    _configure_google(db, key=None)
    assert stt.stt_ready(db) is False
    with pytest.raises(stt.SttError, match="не збережено ключ"):
        asyncio.run(stt.transcribe(db, b"ogg"))


def test_google_request_describes_telegram_opus(db, monkeypatch):
    _configure_google(db)
    assert stt.stt_ready(db) is True
    sink = _patch_client(monkeypatch, _FakeResponse(payload={"results": [
        {"alternatives": [{"transcript": "додай чек"}]},
        {"alternatives": [{"transcript": "стрижка 600"}]},
    ]}))

    out = asyncio.run(stt.transcribe(db, b"opus", media_type="audio/ogg", duration_s=8))
    # Segments come back one per утterance; the message is their concatenation.
    assert out == "додай чек стрижка 600"

    config = sink["json"]["config"]
    assert sink["url"] == stt.GOOGLE_ENDPOINT
    assert sink["params"] == {"key": "AIzaTestKey"}
    assert (config["encoding"], config["sampleRateHertz"]) == ("OGG_OPUS", 48000)
    assert config["languageCode"] == "uk-UA"
    assert config["alternativeLanguageCodes"] == ["ru-RU"]
    assert config["model"] == "latest_long"
    # Audio travels inline, base64-encoded.
    assert base64.b64decode(sink["json"]["audio"]["content"]) == b"opus"


def test_an_explicit_language_drops_googles_alternatives(db, monkeypatch):
    """A retry names the language precisely; leaving the configured alternatives
    in would let Google pick the very language being retried away from."""
    _configure_google(db)
    sink = _patch_client(monkeypatch, _FakeResponse(payload={"results": [
        {"alternatives": [{"transcript": "добавь молоко"}]},
    ]}))

    out = asyncio.run(stt.transcribe(db, b"opus", language="ru-RU"))
    assert out == "добавь молоко"
    config = sink["json"]["config"]
    assert config["languageCode"] == "ru-RU"
    assert "alternativeLanguageCodes" not in config


def test_a_leftover_openai_model_never_reaches_google(db, monkeypatch):
    """Switching provider leaves "gpt-4o-transcribe" behind and Google would 400
    on it — it is snapped on save, and dropped even if it slips through."""
    _configure_google(db, model="gpt-4o-transcribe")
    assert ai.load_config(db).speech.model == "latest_long"

    sink = _patch_client(monkeypatch, _FakeResponse(payload={"results": [
        {"alternatives": [{"transcript": "молоко"}]},
    ]}))
    cfg = ai.load_config(db)
    cfg.speech.model = "gpt-4o-transcribe"  # as if written straight to the DB
    out = asyncio.run(stt._google_transcribe(
        cfg, "AIzaTestKey", b"opus", media_type="audio/ogg", language="uk-UA"))
    assert out == "молоко"
    assert "model" not in sink["json"]["config"]


def test_google_refuses_audio_longer_than_the_sync_limit(db):
    _configure_google(db)
    with pytest.raises(stt.SttError, match="синхронно"):
        asyncio.run(stt.transcribe(db, b"opus", duration_s=stt.GOOGLE_SYNC_MAX_S + 1))


def test_google_error_body_reaches_the_user(db, monkeypatch):
    _configure_google(db)
    _patch_client(monkeypatch, _FakeResponse(status_code=403, payload={"error": {
        "message": "Cloud Speech-to-Text API has not been used in project 1 before or it is disabled.",
    }}))
    with pytest.raises(stt.SttError, match="has not been used"):
        asyncio.run(stt.transcribe(db, b"opus"))


def test_google_key_check_probes_without_sending_audio(db, monkeypatch):
    _configure_google(db)
    sink = _patch_client(monkeypatch, _FakeResponse(payload={"operations": []}))
    out = asyncio.run(stt.check_key(db, ai.GOOGLE_STT))
    assert out["ok"] is True
    assert sink["url"].endswith("/v1/operations")
    assert sink["params"] == {"key": "AIzaTestKey"}


def test_google_key_check_reports_a_disabled_api(db, monkeypatch):
    _configure_google(db)
    _patch_client(monkeypatch, _FakeResponse(status_code=403, payload={"error": {
        "message": "Cloud Speech-to-Text API has not been used in project 1 before",
    }}))
    out = asyncio.run(stt.check_key(db, ai.GOOGLE_STT))
    assert out["ok"] is False and "has not been used" in out["detail"]


def test_groq_transcribes_over_the_openai_endpoint(db, monkeypatch):
    """Groq's free Whisper is the same protocol as OpenAI's — only the host and
    the model differ."""
    _configure(db, provider="groq", model="whisper-large-v3-turbo", key="gsk-test")
    assert stt.stt_ready(db) is True
    sink = _patch_client(monkeypatch, _FakeResponse(payload={"text": "додай чек у бабули"}))

    out = asyncio.run(stt.transcribe(db, b"opus", filename="voice.ogg", duration_s=9))
    assert out == "додай чек у бабули"
    assert sink["url"] == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert sink["headers"]["Authorization"] == "Bearer gsk-test"
    assert sink["data"]["model"] == "whisper-large-v3-turbo"
    # Whisper takes the bare language code, not the BCP-47 tag Google needs.
    assert sink["data"]["language"] == "uk"


def test_switching_speech_provider_snaps_the_model(db):
    """A model id from the previous provider is rejected by the new one, so the
    config is corrected on load/save rather than failing at recognition time."""
    cfg = ai.load_config(db)
    cfg.speech.provider = "groq"
    cfg.speech.model = "gpt-4o-transcribe"
    ai.save_config(db, cfg)
    assert ai.load_config(db).speech.model == "whisper-large-v3-turbo"

    cfg = ai.load_config(db)
    cfg.speech.provider = ai.GOOGLE_STT
    ai.save_config(db, cfg)
    assert ai.load_config(db).speech.model == "latest_long"


def test_a_self_hosted_endpoint_keeps_whatever_model_it_was_given(db):
    cfg = ai.load_config(db)
    cfg.providers.append(ai.ProviderConfig(
        id="custom_openai", label="Local Whisper", base_url="http://localhost:8000/v1", custom=True))
    cfg.speech.provider = "custom_openai"
    cfg.speech.model = "Systran/faster-whisper-small"
    ai.save_config(db, cfg)
    assert ai.load_config(db).speech.model == "Systran/faster-whisper-small"
