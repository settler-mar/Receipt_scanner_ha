"""Speech to text for voice messages (CAPTURE_PLAN.md, крок 3).

A dictated "додай чек ..." has to become a string before `services/capture.py`
can read it. Two providers are supported, and the choice is separate from
`cloud.provider` because transcription is a different API from chat:

  * **OpenAI-compatible** — `POST /audio/transcriptions` (OpenAI itself, or a
    self-hosted Whisper server added as a custom provider);
  * **Google Cloud Speech-to-Text** — `POST /v1/speech:recognize` with an API
    key, its own endpoint and its own stored key.

Anthropic and the other chat-only providers have no transcription API at all, so
voice stays off until one of the above is chosen.

Telegram hands voice over as OGG/Opus, which both accept — no ffmpeg in between.
Google's synchronous endpoint caps at one minute of audio, which a Telegram voice
note normally is; longer messages are refused with that reason.
"""
from __future__ import annotations

import base64
import logging
import time

import httpx
from sqlalchemy.orm import Session

from app.services import ai

logger = logging.getLogger(__name__)

# Telegram voice notes are tiny (~1 KB/s), so anything past this is not a voice
# message and is refused before it costs an upload.
MAX_AUDIO_BYTES = 20 * 1024 * 1024
MAX_DURATION_S = 300
# `speech:recognize` is the synchronous endpoint: one minute of audio, sent
# inline. Anything longer needs a GCS upload + long-running operation.
GOOGLE_SYNC_MAX_S = 60
GOOGLE_ENDPOINT = "https://speech.googleapis.com/v1/speech:recognize"
# Telegram encodes voice notes as Opus in an Ogg container at 48 kHz mono.
_GOOGLE_ENCODINGS = {
    "audio/ogg": ("OGG_OPUS", 48000),
    "audio/oga": ("OGG_OPUS", 48000),
    "audio/opus": ("OGG_OPUS", 48000),
    "audio/webm": ("WEBM_OPUS", 48000),
    "audio/wav": ("LINEAR16", 16000),
    "audio/x-wav": ("LINEAR16", 16000),
    "audio/mpeg": ("MP3", 16000),
    "audio/flac": ("FLAC", 16000),
}


class SttError(RuntimeError):
    """Voice could not be transcribed — not configured, too long, or the
    provider refused."""


def stt_provider(cfg: ai.AiConfig) -> str | None:
    """The provider transcription runs on, or None when voice is off."""
    provider = (cfg.speech.provider or "").strip()
    return provider or None


def stt_ready(db: Session, cfg: ai.AiConfig | None = None) -> bool:
    cfg = cfg or ai.load_config(db)
    provider = stt_provider(cfg)
    if not cfg.speech.enabled or not provider:
        return False
    if provider == ai.GOOGLE_STT:
        return bool(ai.get_cloud_key(db, provider))
    if not cfg.speech.model:
        return False
    spec = ai.provider_spec(cfg, provider)
    if spec is None or spec.kind != "openai":
        return False
    return bool(ai.get_cloud_key(db, provider)) or ai.is_custom_provider(cfg, provider)


def language_options(db: Session, cfg: ai.AiConfig | None = None) -> list[str]:
    """Every language dictation may be tried in, primary first.

    The Settings page decides the order; the bot and the inbox turn the tail into
    "try it in another language" buttons, so the same list has to answer both
    "what do we send by default" and "what else can we offer".
    """
    cfg = cfg or ai.load_config(db)
    codes = [(cfg.speech.language or "uk-UA").strip()]
    codes += [(c or "").strip() for c in cfg.speech.alternative_languages]
    out: list[str] = []
    for code in codes:
        if code and code not in out:
            out.append(code)
    return out


def _endpoint(cfg: ai.AiConfig, provider: str) -> str:
    spec = ai.provider_spec(cfg, provider)
    if spec is None:
        raise SttError(f"невідомий провайдер розпізнавання мовлення: {provider!r}")
    if spec.kind != "openai":
        raise SttError(f"{spec.label} не має API транскрипції — оберіть іншого провайдера")
    custom = ai.custom_provider(cfg, provider)
    base = (custom.base_url if custom else spec.base_url) or ""
    if not base:
        raise SttError("для провайдера розпізнавання мовлення не вказано базовий URL")
    return f"{base.rstrip('/')}/audio/transcriptions"


async def transcribe(
    db: Session,
    audio: bytes,
    *,
    filename: str = "voice.ogg",
    media_type: str = "audio/ogg",
    duration_s: int | None = None,
    language: str | None = None,
) -> str:
    """Voice bytes → the spoken text. Raises `SttError` with a message meant for
    the user; every attempt is metered and logged like any other model call."""
    cfg = ai.load_config(db)
    provider = stt_provider(cfg)
    if not cfg.speech.enabled or not provider:
        raise SttError("Розпізнавання голосу вимкнено — увімкніть його в Налаштуваннях → AI.")
    if not audio:
        raise SttError("Порожнє аудіо.")
    if len(audio) > MAX_AUDIO_BYTES:
        raise SttError("Аудіо завелике — надішліть коротше повідомлення.")
    if duration_s and duration_s > MAX_DURATION_S:
        raise SttError(f"Голосове довше за {MAX_DURATION_S // 60} хв — надішліть коротше.")
    if ai._cloud_over_budget(db, cfg):
        raise SttError("Місячний бюджет токенів вичерпано.")

    key = ai.get_cloud_key(db, provider)
    # A language asked for explicitly is a retry after the default came out
    # wrong, so it is used on its own: leaving the configured alternatives in
    # would let Google fall back to exactly the language being retried away from.
    forced = bool(language)
    language = language or cfg.speech.language or "uk-UA"
    alternatives = [] if forced else [
        c for c in cfg.speech.alternative_languages if c and c != language]

    t0 = time.monotonic()
    try:
        if provider == ai.GOOGLE_STT:
            if not key:
                raise SttError("Для Google Cloud Speech-to-Text не збережено ключ API.")
            if duration_s and duration_s > GOOGLE_SYNC_MAX_S:
                raise SttError(
                    f"Google розпізнає синхронно до {GOOGLE_SYNC_MAX_S} с — "
                    "надішліть коротше повідомлення."
                )
            text = await _google_transcribe(
                cfg, key, audio, media_type=media_type, language=language,
                alternatives=alternatives)
        else:
            if not key and not ai.is_custom_provider(cfg, provider):
                raise SttError("Для провайдера розпізнавання мовлення не збережено ключ.")
            text = await _openai_transcribe(
                cfg, provider, key, audio, filename=filename,
                media_type=media_type, language=language.split("-")[0])
    except SttError as exc:
        _log(db, cfg, ok=False, dur=time.monotonic() - t0, size=len(audio),
             language=language, error=str(exc))
        raise
    except Exception as exc:  # noqa: BLE001 — network/parse
        _log(db, cfg, ok=False, dur=time.monotonic() - t0, size=len(audio),
             language=language, error=str(exc))
        logger.warning("speech-to-text failed", exc_info=True)
        raise SttError(f"Не вдалося розпізнати голос: {exc}") from exc

    _log(db, cfg, ok=True, dur=time.monotonic() - t0, size=len(audio),
         language=language, response=text)
    if not text:
        raise SttError("У голосовому не розпізнано жодного слова.")
    return text


async def _openai_transcribe(
    cfg: ai.AiConfig, provider: str, key: str | None, audio: bytes, *,
    filename: str, media_type: str, language: str,
) -> str:
    data = {"model": cfg.speech.model, "response_format": "json"}
    if language:
        data["language"] = language
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            _endpoint(cfg, provider),
            headers={"Authorization": f"Bearer {key}"} if key else {},
            data=data,
            files={"file": (filename, audio, media_type)},
        )
    _raise_for_status(resp)
    return str(resp.json().get("text") or "").strip()


async def _google_transcribe(
    cfg: ai.AiConfig, key: str, audio: bytes, *, media_type: str, language: str,
    alternatives: list[str] | None = None,
) -> str:
    """Google Cloud Speech-to-Text v1, synchronous, audio inline.

    The container is declared explicitly: `speech:recognize` needs the encoding
    and sample rate for Opus, and Telegram always sends 48 kHz mono Ogg/Opus.
    """
    encoding, sample_rate = _GOOGLE_ENCODINGS.get(
        (media_type or "").split(";")[0].strip().lower(), ("OGG_OPUS", 48000))
    config: dict = {
        "encoding": encoding,
        "sampleRateHertz": sample_rate,
        "audioChannelCount": 1,
        "languageCode": language,
        "enableAutomaticPunctuation": True,
        # Product names and store names are what this app cares about most.
        "profanityFilter": False,
    }
    if alternatives:
        config["alternativeLanguageCodes"] = alternatives[:3]
    # `speech.model` carries Google's recognition model here ("latest_long" etc.);
    # a leftover OpenAI model id would be rejected, so it is only sent when it
    # looks like one of Google's.
    model = (cfg.speech.model or "").strip()
    if model in {"latest_long", "latest_short", "default", "command_and_search",
                 "phone_call", "video", "medical_dictation", "medical_conversation"}:
        config["model"] = model

    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            GOOGLE_ENDPOINT, params={"key": key},
            json={"config": config, "audio": {"content": base64.b64encode(audio).decode()}},
        )
    _raise_for_status(resp)
    results = resp.json().get("results") or []
    # One alternative per result segment; joining them gives the whole message.
    parts = [
        str((r.get("alternatives") or [{}])[0].get("transcript") or "").strip()
        for r in results
    ]
    return " ".join(p for p in parts if p).strip()


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    detail = resp.text
    try:
        detail = resp.json().get("error", {}).get("message") or detail
    except Exception:  # noqa: BLE001 — non-JSON error body
        pass
    raise SttError(f"{resp.status_code} {detail[:300]}")


async def check_key(db: Session, provider: str, key: str | None = None) -> dict:
    """"Тест" button for the speech provider: is the key accepted at all?

    Never sends a chat request. The chat check pings a *chat* model, and the
    speech provider's chat catalogue has nothing to do with transcription — a
    retired chat model id would report "ключ не працює" for a perfectly good key.
    So: Google is probed with an operations listing, everything else with the
    provider's own `/models` listing. Both need exactly the key being tested.
    """
    key = (key or "").strip() or ai.get_cloud_key(db, provider)
    cfg = ai.load_config(db)

    if provider == ai.GOOGLE_STT:
        if not key:
            return {"ok": False, "detail": "ключ не задано"}
        return await _probe(
            "https://speech.googleapis.com/v1/operations", params={"key": key},
            ok_detail="ключ приймається, Speech-to-Text API увімкнено")

    spec = ai.provider_spec(cfg, provider)
    if spec is None or spec.kind != "openai":
        return {"ok": False, "detail": f"{provider}: немає API транскрипції"}
    custom = ai.custom_provider(cfg, provider)
    base = (custom.base_url if custom else spec.base_url) or ""
    if not base:
        return {"ok": False, "detail": "не вказано базовий URL провайдера"}
    if not key and not custom:
        return {"ok": False, "detail": "ключ не задано"}
    return await _probe(
        f"{base.rstrip('/')}/models",
        headers={"Authorization": f"Bearer {key}"} if key else {},
        ok_detail=f"{spec.label}: ключ робочий")


async def _probe(url: str, *, params: dict | None = None, headers: dict | None = None,
                 ok_detail: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, params=params, headers=headers)
    except Exception as exc:  # noqa: BLE001 — network
        return {"ok": False, "detail": str(exc)[:300]}
    if resp.status_code < 400:
        return {"ok": True, "detail": ok_detail}
    detail = resp.text
    try:
        detail = resp.json().get("error", {}).get("message") or detail
    except Exception:  # noqa: BLE001 — non-JSON error body
        pass
    return {"ok": False, "detail": f"{resp.status_code} {detail[:300]}"}


def _log(db: Session, cfg: ai.AiConfig, *, ok: bool, dur: float, size: int,
         language: str = "", error: str = "", response: str = "") -> None:
    ai._record_usage(db, backend="cloud", ok=ok)
    ai.log_event(
        db, task="stt", backend="cloud", model=cfg.speech.model,
        duration_ms=int(dur * 1000), ok=ok, error=error,
        prompt=f"голосове ({size} B{', ' + language if language else ''})",
        response=response,
    )
