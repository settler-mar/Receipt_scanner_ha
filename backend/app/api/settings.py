from __future__ import annotations

import asyncio
import datetime as dt

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.models import AppSetting
from app.schemas.schemas import SettingOut, SettingUpdate
from app.services import ai
from app.services import drive as drive_svc
from app.services import mcp_config as mcp_cfg
from app.services import tax_cabinet_settings as tax_settings
from app.services import telegram as telegram_svc

router = APIRouter(prefix="/api/settings", tags=["settings"])

# Keys owned by dedicated, validated endpoints below — the generic key/value
# routes must not touch them (ai_cloud_key / drive_oauth* hold encrypted secrets;
# ai_config / ai_usage / mcp_config have a fixed shape).
_RESERVED = {
    ai.CONFIG_KEY, ai.CLOUD_KEY_KEY, ai.USAGE_KEY, ai.LOG_KEY,
    drive_svc.TOKEN_KEY, drive_svc.CLIENT_KEY,
    mcp_cfg.CONFIG_KEY,
    tax_settings.TOKEN_KEY,
    telegram_svc.CONFIG_KEY, telegram_svc.TOKEN_KEY, telegram_svc.LEGACY_AUTH_KEY,
}


@router.get("", response_model=list[SettingOut])
def list_settings(db: Session = Depends(get_db)):
    rows = db.execute(select(AppSetting)).scalars().all()
    return [SettingOut(key=row.key, value=row.value) for row in rows if row.key not in _RESERVED]


# --------------------------------------------------------------------------- #
# AI configuration (see app/services/ai.py, ROADMAP.md § 0)
# --------------------------------------------------------------------------- #
class AiSettingsOut(BaseModel):
    config: ai.AiConfig
    # Catalogue of services that can be switched on (block 1 of the AI settings),
    # with the protocol facts the page needs: [{id, label, kind, custom,
    # usage_url, fallback_models}].
    providers: list[dict]
    stt_providers: list[dict]               # [{id, label, kind, models}] — voice
    # Dictation languages the page offers: [{code, label}] — the user picks which
    # is primary and which stay as the "recognise it in another language" retry.
    speech_languages: list[dict]
    cloud_key_hints: dict[str, str | None]  # provider -> "••••1234" | None
    usage: dict


class CloudKeyIn(BaseModel):
    provider: str
    api_key: str | None = None


class CloudKeyTestIn(BaseModel):
    provider: str
    api_key: str | None = None   # None → test the stored key
    model: str | None = None
    workspace_id: str | None = None   # None → use the saved config value
    base_url: str | None = None       # custom endpoints; None → the saved value
    # "chat" pings a chat model; "stt" checks the key against the transcription
    # provider instead — a speech key must not be judged by a chat catalogue.
    purpose: str = "chat"


@router.get("/ai", response_model=AiSettingsOut)
def get_ai_settings(db: Session = Depends(get_db)):
    cfg = ai.load_config(db)
    return AiSettingsOut(
        config=cfg,
        providers=ai.providers_meta(cfg),
        stt_providers=[
            # `models` is what the page offers for that provider; empty means the
            # endpoint can run anything (a self-hosted Whisper server).
            {**meta, "models": ai.STT_MODEL_CHOICES.get(meta["id"], [])}
            for meta in ai.stt_providers_meta(cfg)
        ],
        speech_languages=[{"code": code, "label": label}
                          for code, label in ai.SPEECH_LANGUAGES.items()],
        cloud_key_hints=ai.cloud_key_hints(db, cfg),
        usage=ai.load_usage(db),
    )


@router.put("/ai", response_model=AiSettingsOut)
def put_ai_settings(config: ai.AiConfig, db: Session = Depends(get_db)):
    """Both AI blocks are saved together: the provider list (which services are
    on, with which models) and the routing (which service+model each task tries,
    in order)."""
    known = {p.id for p in config.providers}
    for task, targets in config.routing.items():
        for target in targets:
            if target.provider != ai.LOCAL and target.provider not in known:
                raise HTTPException(
                    status_code=400,
                    detail=f"маршрут {task!r} посилається на невідомий сервіс {target.provider!r}",
                )
    speech = (config.speech.provider or "").strip()
    if speech and not ai.known_provider(config, speech):
        raise HTTPException(status_code=400, detail=f"unknown speech provider {speech!r}")
    old_ids = {p.id for p in ai.load_config(db).providers}
    ai.save_config(db, config)
    # A custom endpoint the user deleted must not leave its key behind.
    for removed_id in old_ids - {p.id for p in config.providers}:
        ai.set_cloud_key(db, removed_id, None)
    return get_ai_settings(db)


@router.put("/ai/cloud-key", response_model=AiSettingsOut)
def put_ai_cloud_key(payload: CloudKeyIn, db: Session = Depends(get_db)):
    cfg = ai.load_config(db)
    # Speech providers keep their own key under the same store, so the id may be
    # one that has no chat side at all (Google Cloud Speech-to-Text).
    if not ai.known_provider(cfg, payload.provider):
        raise HTTPException(status_code=400, detail=f"unknown provider {payload.provider!r}")
    ai.set_cloud_key(db, payload.provider, (payload.api_key or "").strip() or None)
    return get_ai_settings(db)


@router.post("/ai/test-key")
async def test_ai_cloud_key(payload: CloudKeyTestIn, db: Session = Depends(get_db)):
    """"Тест" button next to the API-key field — validates a key (the one being
    typed, or the stored one) before it's committed."""
    cfg = ai.load_config(db)
    if not ai.known_provider(cfg, payload.provider):
        raise HTTPException(status_code=400, detail=f"unknown provider {payload.provider!r}")
    # A speech key is tested against its transcription/listing endpoint: pinging a
    # chat model would report a broken key whenever that provider's chat
    # catalogue has moved on, which has nothing to do with transcription.
    if payload.purpose == "stt" or payload.provider == ai.GOOGLE_STT:
        from app.services import stt

        return await stt.check_key(db, payload.provider, payload.api_key)
    key = (payload.api_key or "").strip() or ai.get_cloud_key(db, payload.provider)
    conf = cfg.provider(payload.provider)
    if not key and not (conf is not None and conf.custom):
        return {"ok": False, "detail": "ключ не задано"}
    workspace = payload.workspace_id if payload.workspace_id is not None else (
        conf.workspace_id if conf else "")
    base_url = payload.base_url if payload.base_url is not None else (conf.base_url if conf else "")
    model = payload.model or (conf.models[0] if conf and conf.models else None)
    return await ai.check_provider_key(payload.provider, key, model, workspace or None, base_url)


@router.get("/ai/models")
async def list_ai_models(db: Session = Depends(get_db)):
    """Model catalogues for the provider block — one list per configured
    provider (enabled or not, so models can be ticked while switching a service
    on), the transcription lists per speech provider, plus whatever Ollama has
    locally. `live` names the providers whose list really came from their API;
    the rest are the static fallback sets.

    Also self-heals the stored config: a model the provider has retired is
    dropped from that provider's active list and from any route pointing at it,
    so the background classification tick stops erroring. Only live catalogues
    heal — a fallback list must never wipe a hand-added model id. `config` in
    the response reflects any such fix; the frontend applies it."""
    cfg = ai.load_config(db)
    local, (providers, live) = await asyncio.gather(
        ai.list_local_models(cfg),
        ai.list_all_models(db, cfg),
    )
    live_catalogues = {pid: providers[pid] for pid in live}
    stt = ai.list_stt_models(cfg, live_catalogues)
    if ai.heal_models(cfg, {pid: [m["id"] for m in models]
                            for pid, models in live_catalogues.items()}):
        ai.save_config(db, cfg)
    return {"local": local, "providers": providers, "live": sorted(live),
            "stt": stt, "config": cfg}


@router.get("/ai/models/{provider}")
async def list_one_provider_models(provider: str, db: Session = Depends(get_db)):
    """Catalogue of a single service — what its own "оновити моделі" button
    calls. Same shape as one entry of `/ai/models`, so the page can drop it into
    the list it already has without re-asking every other provider (and without
    paying their timeouts). Heals only this provider, and only from a live list.
    """
    cfg = ai.load_config(db)
    if provider == ai.GOOGLE_STT:
        # Speech-only service: no chat catalogue, and its recognisers are a
        # fixed set rather than an API listing.
        return {"provider": provider, "models": [], "live": False,
                "stt": ai.list_stt_models(cfg).get(provider, []), "config": cfg}
    if ai.provider_spec(cfg, provider) is None:
        raise HTTPException(status_code=404, detail=f"unknown provider {provider!r}")
    models, live = await ai.list_provider_catalogue(db, cfg, provider)
    stt = ai.list_stt_models(cfg, {provider: models} if live else {}).get(provider, [])
    if live and ai.heal_models(cfg, {provider: [m["id"] for m in models]}):
        ai.save_config(db, cfg)
    return {"provider": provider, "models": models, "live": live, "stt": stt, "config": cfg}


@router.get("/ai/log")
def get_ai_log(db: Session = Depends(get_db)):
    """Recent AI activity — every model call and classification batch, newest
    first. Lets you see what happened when 'tokens spent but no result'."""
    return {"entries": ai.recent_log(db)}


@router.post("/ai/check")
async def check_ai_backends(db: Session = Depends(get_db)):
    """Connectivity of the local backend and of every enabled provider."""
    return await ai.check_all(db, ai.load_config(db))


# --------------------------------------------------------------------------- #
# MCP server (see app/mcp_server.py, ROADMAP.md Крок 4)
# --------------------------------------------------------------------------- #
class McpSettingsOut(BaseModel):
    config: mcp_cfg.McpConfig
    tools: list[dict]        # [{name, kind, label, access, available}]
    mount_path: str = "/mcp"


def _mcp_out(cfg: mcp_cfg.McpConfig) -> McpSettingsOut:
    return McpSettingsOut(config=cfg, tools=mcp_cfg.tools_meta(cfg))


@router.get("/mcp", response_model=McpSettingsOut)
def get_mcp_settings(db: Session = Depends(get_db)):
    return _mcp_out(mcp_cfg.load_config(db))


@router.put("/mcp", response_model=McpSettingsOut)
def put_mcp_settings(config: mcp_cfg.McpConfig, db: Session = Depends(get_db)):
    return _mcp_out(mcp_cfg.save_config(db, config))


# --------------------------------------------------------------------------- #
# ДПС fiscal-check API token
# --------------------------------------------------------------------------- #
class TaxCabinetSettingsOut(BaseModel):
    configured: bool
    token_hint: str | None = None
    api_guard: dict = {}


class TaxCabinetTokenIn(BaseModel):
    token: str | None = None
    clear: bool = False


def _tax_settings_out(db: Session) -> TaxCabinetSettingsOut:
    from app.scanner.fiscal_check import api_guard_status
    hint = tax_settings.token_hint(db)
    return TaxCabinetSettingsOut(configured=hint is not None, token_hint=hint, api_guard=api_guard_status())


@router.get("/tax-cabinet", response_model=TaxCabinetSettingsOut)
def get_tax_cabinet_settings(db: Session = Depends(get_db)):
    return _tax_settings_out(db)


@router.put("/tax-cabinet", response_model=TaxCabinetSettingsOut)
def put_tax_cabinet_settings(payload: TaxCabinetTokenIn, db: Session = Depends(get_db)):
    # An empty input means "keep the existing secret", matching the AI/Drive
    # password fields. Deletion must be explicit so a browser refresh cannot
    # accidentally erase the token.
    if payload.clear:
        tax_settings.set_token(db, None)
    elif payload.token is not None and payload.token.strip():
        tax_settings.set_token(db, payload.token)
    return _tax_settings_out(db)


# --------------------------------------------------------------------------- #
# Telegram bot
# --------------------------------------------------------------------------- #
class TelegramSettingsIn(BaseModel):
    enabled: bool = False
    bot_token: str | None = None
    clear_token: bool = False
    public_url: str | None = None
    # None = leave as is (older clients don't send it).
    capture_enabled: bool | None = None


def _telegram_out(db: Session, username: str | None = None) -> telegram_svc.TelegramSettingsOut:
    return telegram_svc.TelegramSettingsOut(
        config=telegram_svc.load_config(db), token_hint=telegram_svc.token_hint(db),
        bot_username=username,
    )


@router.get("/telegram", response_model=telegram_svc.TelegramSettingsOut)
def get_telegram_settings(db: Session = Depends(get_db)):
    return _telegram_out(db)


@router.put("/telegram", response_model=telegram_svc.TelegramSettingsOut)
async def put_telegram_settings(payload: TelegramSettingsIn, db: Session = Depends(get_db)):
    # Remove the secret from installations that used the old /auth CODE flow.
    telegram_svc._set_secret(db, telegram_svc.LEGACY_AUTH_KEY, None)
    token = (payload.bot_token or "").strip()
    username = None
    if token:
        ok, username, detail = await telegram_svc.check_token(token)
        if not ok:
            raise HTTPException(status_code=400, detail=f"Токен Telegram не пройшов перевірку: {detail}")
        telegram_svc._set_secret(db, telegram_svc.TOKEN_KEY, token)
    elif payload.clear_token:
        telegram_svc._set_secret(db, telegram_svc.TOKEN_KEY, None)
    cfg = telegram_svc.load_config(db)
    if payload.enabled:
        if telegram_svc._secret(db, telegram_svc.TOKEN_KEY) is None:
            raise HTTPException(status_code=400, detail="Спочатку задайте токен Telegram-бота")
    if payload.public_url is not None:
        public_url = payload.public_url.strip().rstrip("/")
        if public_url and not public_url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="Адреса має починатися з http:// або https://")
        cfg.public_url = public_url
    if payload.capture_enabled is not None:
        cfg.capture_enabled = payload.capture_enabled
    cfg.enabled = payload.enabled
    telegram_svc.save_config(db, cfg)
    return _telegram_out(db, username)


@router.delete("/telegram/chats/{chat_id}", response_model=telegram_svc.TelegramSettingsOut)
def revoke_telegram_chat(chat_id: int, db: Session = Depends(get_db)):
    cfg = telegram_svc.load_config(db)
    cfg.authorized_chat_ids = [value for value in cfg.authorized_chat_ids if value != chat_id]
    cfg.authorized_chats = [value for value in cfg.authorized_chats if value.chat_id != chat_id]
    telegram_svc.save_config(db, cfg)
    return _telegram_out(db)


@router.post("/telegram/chats/{chat_id}/approve", response_model=telegram_svc.TelegramSettingsOut)
async def approve_telegram_chat(chat_id: int, db: Session = Depends(get_db)):
    cfg = telegram_svc.load_config(db)
    pending = next((row for row in cfg.pending_chats if row.chat_id == chat_id), None)
    if pending is None:
        raise HTTPException(status_code=404, detail="Заявку не знайдено")
    cfg.pending_chats = [row for row in cfg.pending_chats if row.chat_id != chat_id]
    cfg.authorized_chat_ids.append(chat_id)
    cfg.authorized_chats.append(telegram_svc.AuthorizedChat(
        chat_id=chat_id, username=pending.username, display_name=pending.display_name,
        approved_at=dt.datetime.now(dt.timezone.utc).isoformat(),
    ))
    telegram_svc.save_config(db, cfg)
    token = telegram_svc._secret(db, telegram_svc.TOKEN_KEY)
    if token:
        try:
            await telegram_svc._send(token, chat_id, "✅ Доступ дозволено. Тепер можна надсилати чеки та команди.")
        except Exception:  # approval remains valid even if Telegram is temporarily unavailable
            pass
    return _telegram_out(db)


@router.delete("/telegram/chats/{chat_id}/request", response_model=telegram_svc.TelegramSettingsOut)
async def reject_telegram_chat(chat_id: int, db: Session = Depends(get_db)):
    cfg = telegram_svc.load_config(db)
    cfg.pending_chats = [row for row in cfg.pending_chats if row.chat_id != chat_id]
    telegram_svc.save_config(db, cfg)
    token = telegram_svc._secret(db, telegram_svc.TOKEN_KEY)
    if token:
        try:
            await telegram_svc._send(token, chat_id, "Доступ до бота не підтверджено.")
        except Exception:
            pass
    return _telegram_out(db)


# --------------------------------------------------------------------------- #
# Generic key/value settings (dashboard fuel block etc.)
# --------------------------------------------------------------------------- #
@router.get("/{key}", response_model=SettingOut)
def get_setting(key: str, db: Session = Depends(get_db)):
    """Missing keys are not a 404 — a setting that was never saved just reads
    back as `value: null`, so the frontend can treat "unset" and "cleared" the
    same way."""
    if key in _RESERVED:
        raise HTTPException(status_code=400, detail="This key has a dedicated /api/settings/* endpoint")
    row = db.get(AppSetting, key)
    return SettingOut(key=key, value=row.value if row is not None else None)


@router.put("/{key}", response_model=SettingOut)
def put_setting(key: str, payload: SettingUpdate, db: Session = Depends(get_db)):
    if key in _RESERVED:
        raise HTTPException(status_code=400, detail="This key has a dedicated /api/settings/* endpoint")
    row = db.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value=payload.value)
        db.add(row)
    else:
        row.value = payload.value
    db.commit()
    return SettingOut(key=key, value=payload.value)
