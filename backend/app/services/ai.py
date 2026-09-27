"""Single entry point for every LLM call in the project.

Design goals (see ROADMAP.md § 0.1):
  * auditability — no personal document ever reaches an external model, so every
    outbound model call is funnelled through this one module;
  * token thrift — batching / permanent caching of results is done by callers
    (product_category / product_alias tables); this module only meters usage and
    enforces a soft monthly budget;
  * backend-agnostic routing — each task type has an ordered list of backends to
    try. "local" = an Ollama-compatible HTTP endpoint. "cloud" = one of several
    hosted providers (Anthropic / OpenAI / Google Gemini / xAI / DeepSeek /
    Mistral), picked in the config; all the non-Anthropic ones speak the OpenAI
    chat-completions wire format, so they share one HTTP path.

Configuration is NOT read from the environment — it lives in `app_settings`
(keys `ai_config`, `ai_cloud_key`, `ai_usage`) and is edited on the in-app
Settings page. The only env value involved is RS_SECRET_KEY, used to encrypt the
cloud API keys at rest (via app.core.security), same as loyalty tokens.
"""
from __future__ import annotations

import asyncio
import base64
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import re
import time
from urllib.parse import urlparse
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.core.security import decrypt, encrypt
from app.models.models import AppSetting
from app.services import prompt_catalog

logger = logging.getLogger(__name__)

TaskName = Literal["classify", "normalize", "ocr", "intent"]

CONFIG_KEY = "ai_config"
CLOUD_KEY_KEY = "ai_cloud_key"
USAGE_KEY = "ai_usage"
LOG_KEY = "ai_log"          # ring buffer of recent AI calls (see log_event / recent_log)
LOG_MAX = 60


# --------------------------------------------------------------------------- #
# Cloud providers
# --------------------------------------------------------------------------- #
class ProviderSpec(BaseModel):
    label: str
    kind: Literal["anthropic", "openai"]   # wire protocol
    base_url: str | None = None            # for kind == "openai"
    fallback_models: list[str]             # shown in dropdowns when /models is unreachable
    # Prefixes a model id for this provider is expected to start with. Used to
    # catch a stale model left over from a previous provider (e.g. "claude-*"
    # still configured after switching to OpenAI → OpenAI 404 model_not_found).
    model_prefixes: list[str] = []

    def owns_model(self, model: str) -> bool:
        return bool(model) and any(model.startswith(p) for p in self.model_prefixes)


# Order here is the order shown in the Settings dropdown.
PROVIDERS: dict[str, ProviderSpec] = {
    "anthropic": ProviderSpec(
        label="Anthropic Claude", kind="anthropic",
        fallback_models=["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-4-8"],
        model_prefixes=["claude-"],
    ),
    "openai": ProviderSpec(
        label="OpenAI (ChatGPT)", kind="openai", base_url="https://api.openai.com/v1",
        fallback_models=["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1"],
        model_prefixes=["gpt-", "o1", "o3", "o4", "chatgpt"],
    ),
    "google": ProviderSpec(
        label="Google Gemini", kind="openai",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        fallback_models=["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"],
        model_prefixes=["gemini-", "gemma"],
    ),
    "xai": ProviderSpec(
        label="xAI Grok", kind="openai", base_url="https://api.x.ai/v1",
        fallback_models=["grok-2-latest", "grok-2-vision-latest", "grok-beta"],
        model_prefixes=["grok-"],
    ),
    "deepseek": ProviderSpec(
        label="DeepSeek", kind="openai", base_url="https://api.deepseek.com/v1",
        fallback_models=["deepseek-chat", "deepseek-reasoner"],
        model_prefixes=["deepseek-"],
    ),
    "groq": ProviderSpec(
        label="Groq", kind="openai", base_url="https://api.groq.com/openai/v1",
        fallback_models=["llama-3.3-70b-versatile", "llama-3.1-8b-instant",
                         "openai/gpt-oss-120b", "whisper-large-v3-turbo"],
        model_prefixes=["llama-", "llama3", "meta-llama/", "mixtral-", "gemma",
                        "whisper-", "qwen", "moonshotai/", "deepseek-r1",
                        "openai/gpt-oss", "groq/"],
    ),
    "mistral": ProviderSpec(
        label="Mistral", kind="openai", base_url="https://api.mistral.ai/v1",
        fallback_models=["mistral-small-latest", "mistral-large-latest", "pixtral-large-latest"],
        model_prefixes=["mistral-", "pixtral-", "codestral-", "ministral-", "magistral-",
                        "open-mistral", "open-mixtral"],
    ),
}
ProviderName = str

# Where each provider reports what has actually been spent on its side. Our own
# meter (ai_usage) only counts calls this app made; a shared key, another app or
# a free-tier quota is only visible in the provider's own console — so every
# model dropdown links out to it. Best-effort entry points: consoles move, and a
# console root is a better landing place than a stale deep link.
PROVIDER_USAGE_URLS: dict[str, str] = {
    "anthropic": "https://console.anthropic.com/settings/usage",
    "openai": "https://platform.openai.com/usage",
    "google": "https://console.cloud.google.com/apis/api/generativelanguage.googleapis.com/metrics",
    "xai": "https://console.x.ai/",
    "deepseek": "https://platform.deepseek.com/usage",
    "groq": "https://console.groq.com/",
    "mistral": "https://console.mistral.ai/usage/",
}

# Speech-to-text does not ride the chat API. Two shapes are supported:
#   * an OpenAI-compatible POST /audio/transcriptions (OpenAI itself, or a
#     self-hosted Whisper server configured as a custom provider);
#   * Google Cloud Speech-to-Text, which has its own endpoint and its own key.
# This id is only ever used for STT — never as `cloud.provider`.
GOOGLE_STT = "google_speech"
STT_PROVIDER_LABELS: dict[str, str] = {GOOGLE_STT: "Google Cloud Speech-to-Text"}
PROVIDER_USAGE_URLS[GOOGLE_STT] = (
    "https://console.cloud.google.com/apis/api/speech.googleapis.com/metrics"
)

# What to transcribe with, per speech provider. A model id left behind after
# switching providers ("gpt-4o-transcribe" on Groq) is rejected by the new one,
# so `normalize_speech_model` snaps it to the entry here.
DEFAULT_STT_MODELS: dict[str, str] = {
    "openai": "gpt-4o-transcribe",
    "groq": "whisper-large-v3-turbo",
    GOOGLE_STT: "latest_long",
}
# What the Settings page offers per speech provider (first entry = default).
STT_MODEL_CHOICES: dict[str, list[str]] = {
    "openai": ["gpt-4o-transcribe", "gpt-4o-mini-transcribe", "whisper-1"],
    "groq": ["whisper-large-v3-turbo", "whisper-large-v3"],
    GOOGLE_STT: ["latest_long", "latest_short", "default", "command_and_search",
                 "phone_call", "video"],
}
# Dictation languages the app offers, in the order the Settings page lists them.
# The user picks which of them is primary (first tried) and which stay as the
# "try it in another language" fallbacks — Ukrainian and Russian are mixed freely
# in a spoken shopping list, and a recogniser locked to one mangles the other.
SPEECH_LANGUAGES: dict[str, str] = {
    "uk-UA": "українська",
    "ru-RU": "російська",
    "en-US": "англійська",
}


def speech_language_label(code: str) -> str:
    return SPEECH_LANGUAGES.get(code, code)


# Substrings a model id must contain to be plausible for that provider; a custom
# endpoint can run anything, so it is not listed and never snapped.
_STT_MODEL_MARKERS: dict[str, tuple[str, ...]] = {
    "openai": ("gpt-", "whisper"),
    "groq": ("whisper",),
    GOOGLE_STT: ("latest_", "default", "command_and_search", "phone_call", "video",
                 "medical_"),
}


# --------------------------------------------------------------------------- #
# Config models
# --------------------------------------------------------------------------- #
# Settings are split in two blocks, mirroring the Settings page:
#   1. providers  — which services are switched on, their keys and the models
#                   the user marked active for each of them;
#   2. routing    — per task, an ordered list of (service, model) attempts.
# The local Ollama backend takes part in routing under this pseudo-provider id.
LOCAL = "local"

TASKS: tuple[str, ...] = ("classify", "normalize", "ocr", "intent")
# Tasks that need a vision-capable model. The local Ollama path here is
# text-only, so a local target is skipped for them.
VISION_TASKS = frozenset({"ocr"})


class LocalBackendConfig(BaseModel):
    enabled: bool = False
    url: str = "http://localhost:11434"
    model: str = "qwen2.5:3b-instruct"
    # Ollama's default context is only ~4K — a classify batch (taxonomy + ~30
    # product names + JSON reply) can exceed it and get silently truncated.
    num_ctx: int = 8192


class ProviderConfig(BaseModel):
    """One AI service the user has configured (Settings block 1).

    Several providers can be active at once — each keeps its own key (stored
    encrypted under `ai_cloud_key`, one entry per id) and its own list of
    *active* models, which is what the routing block may point a task at.
    """
    id: str
    label: str = ""
    enabled: bool = False
    # Custom OpenAI-compatible endpoints only, without the trailing
    # /chat/completions; builtin providers take the URL from their ProviderSpec.
    base_url: str = ""
    # Where this endpoint shows spend/quota, if anywhere. Builtin providers get
    # it from PROVIDER_USAGE_URLS; a custom one is filled in by the user.
    usage_url: str = ""
    models: list[str] = Field(default_factory=list)
    # Anthropic only: identity-linked / admin API keys must name the workspace
    # the request acts in ("anthropic-workspace-id is required …" → 400).
    workspace_id: str = ""
    custom: bool = False


class RouteTarget(BaseModel):
    """One attempt in a task's priority list: which service, which model."""
    provider: str          # LOCAL or a ProviderConfig.id
    model: str = ""        # empty → the provider's first active model


class SpeechConfig(BaseModel):
    """Speech-to-text. Transcription rides an OpenAI-compatible
    /audio/transcriptions endpoint (which Anthropic has none of) or Google Cloud
    Speech-to-Text, so it keeps its own provider — and therefore its own key —
    instead of following the chat routing."""
    enabled: bool = False
    provider: str = ""
    model: str = "gpt-4o-transcribe"
    # BCP-47 for Google ("uk-UA"); the OpenAI path uses the language part alone.
    # The language every recording is tried in first.
    language: str = "uk-UA"
    # The rest, in order. Google is told about them up front (it picks per
    # message); every provider offers them as "розпізнати іншою мовою" — the
    # retry the bot and the inbox put under a transcript that came out wrong.
    alternative_languages: list[str] = Field(default_factory=lambda: ["ru-RU"])


def _default_providers() -> list[ProviderConfig]:
    """Every builtin service, switched off. Turning one on and giving it a key
    is the whole of Settings block 1."""
    return [
        ProviderConfig(id=name, label=spec.label, usage_url=PROVIDER_USAGE_URLS.get(name, ""))
        for name, spec in PROVIDERS.items()
    ]


def _default_routing() -> dict[str, list[RouteTarget]]:
    return {task: [] for task in TASKS}


class AiConfig(BaseModel):
    local: LocalBackendConfig = Field(default_factory=LocalBackendConfig)
    providers: list[ProviderConfig] = Field(default_factory=_default_providers)
    speech: SpeechConfig = Field(default_factory=SpeechConfig)
    # Ordered attempts per task. The first target that is enabled, configured
    # and answers wins; the rest stay as fallbacks.
    routing: dict[str, list[RouteTarget]] = Field(default_factory=_default_routing)
    # Soft cap on cloud tokens (in + out) per calendar month. When exceeded,
    # every non-local target is skipped until the month rolls over. 0 disables it.
    monthly_token_budget: int = 2_000_000

    @model_validator(mode="after")
    def _normalize(self) -> "AiConfig":
        _normalize_providers(self)
        for task in TASKS:
            if task not in self.routing:
                self.routing[task] = []
        return self

    def route(self, task: str) -> list[RouteTarget]:
        return self.routing.get(task) or []

    def provider(self, provider_id: str) -> ProviderConfig | None:
        return next((p for p in self.providers if p.id == provider_id), None)


class AiConfigError(RuntimeError):
    """No usable backend for a task (nothing enabled / configured / all failed)."""


def _normalize_providers(cfg: AiConfig) -> None:
    seen: set[str] = set()
    out: list[ProviderConfig] = []
    for i, provider in enumerate(cfg.providers or []):
        provider.id = (provider.id or f"custom_openai_{i + 1}").strip()
        if provider.id == LOCAL:
            continue                      # the local backend is not a provider entry
        base_id = provider.id
        suffix = 2
        while provider.id in seen:
            provider.id = f"{base_id}_{suffix}"
            suffix += 1
        seen.add(provider.id)
        spec = PROVIDERS.get(provider.id)
        provider.custom = spec is None
        provider.label = (provider.label or (spec.label if spec else "Custom OpenAI-compatible")).strip()
        provider.base_url = (provider.base_url or "").strip()
        provider.usage_url = (provider.usage_url or PROVIDER_USAGE_URLS.get(provider.id, "")).strip()
        provider.models = list(dict.fromkeys(m.strip() for m in provider.models if m and m.strip()))
        out.append(provider)
    for name, spec in PROVIDERS.items():   # a builtin added after this blob was saved
        if name not in seen:
            out.append(ProviderConfig(id=name, label=spec.label,
                                      usage_url=PROVIDER_USAGE_URLS.get(name, "")))
    cfg.providers = out


# --------------------------------------------------------------------------- #
# Legacy config migration
# --------------------------------------------------------------------------- #
# Configs written before the providers/routing split carried a single active
# cloud provider (`cloud.provider` + model_text / model_vision) and a routing of
# backend *kinds* (["local", "cloud"]). Convert once, on read.
def _migrate_legacy(raw: dict[str, Any]) -> dict[str, Any]:
    cloud = raw.get("cloud") or {}
    active = str(cloud.get("provider") or "anthropic")
    text_model = str(cloud.get("model_text") or "")
    vision_model = str(cloud.get("model_vision") or "")

    providers: list[dict[str, Any]] = []
    for name, spec in PROVIDERS.items():
        providers.append({
            "id": name, "label": spec.label, "enabled": bool(cloud.get("enabled")) and name == active,
            "usage_url": PROVIDER_USAGE_URLS.get(name, ""),
            "models": [m for m in dict.fromkeys([text_model, vision_model])
                       if m and spec.owns_model(m)],
            "workspace_id": cloud.get("anthropic_workspace_id") or "" if name == "anthropic" else "",
        })
    for legacy in cloud.get("custom_providers") or []:
        pid = str(legacy.get("id") or "custom_openai")
        is_active = pid == active
        providers.append({
            "id": pid, "label": legacy.get("label") or "Custom OpenAI-compatible",
            "enabled": bool(cloud.get("enabled")) and is_active,
            "base_url": legacy.get("base_url") or cloud.get("custom_base_url") or "",
            "usage_url": legacy.get("usage_url") or "",
            "models": [m for m in dict.fromkeys(
                ([text_model, vision_model] if is_active else []) + list(legacy.get("fallback_models") or [])
            ) if m],
            "custom": True,
        })

    def targets(order: list[str], task: str) -> list[dict[str, str]]:
        model = vision_model if task in VISION_TASKS else text_model
        out = []
        for kind in order:
            if kind == "local":
                out.append({"provider": LOCAL, "model": raw.get("local", {}).get("model", "")})
            elif kind == "cloud" and cloud.get("enabled"):
                out.append({"provider": active, "model": model})
        return out

    legacy_routing = raw.get("routing") or {}
    routing = {task: targets(legacy_routing.get(task) or _LEGACY_ROUTING.get(task, []), task)
               for task in TASKS}

    return {
        "local": raw.get("local") or {},
        "providers": providers,
        "speech": {
            "enabled": bool(cloud.get("enabled")) and bool(cloud.get("stt_provider")),
            "provider": cloud.get("stt_provider") or "",
            "model": cloud.get("model_stt") or "gpt-4o-transcribe",
            "language": cloud.get("stt_language") or "uk-UA",
            "alternative_languages": cloud.get("stt_alternative_languages") or [],
        },
        "routing": routing,
        "monthly_token_budget": raw.get("monthly_token_budget", 2_000_000),
    }


_LEGACY_ROUTING: dict[str, list[str]] = {
    "classify": ["local", "cloud"],
    "normalize": ["local", "cloud"],
    "ocr": ["cloud"],
    "intent": ["cloud", "local"],
}


# --------------------------------------------------------------------------- #
# Config persistence (app_settings)
# --------------------------------------------------------------------------- #
def load_config(db: Session) -> AiConfig:
    row = db.get(AppSetting, CONFIG_KEY)
    if row is None or not isinstance(row.value, dict) or not row.value:
        return AiConfig()
    raw = dict(row.value)
    if "providers" not in raw:
        raw = _migrate_legacy(raw)
    try:
        return normalize_speech_model(AiConfig.model_validate(raw))
    except Exception:  # tolerate a partially-written / older-shaped blob
        logger.warning("ai_config in app_settings is malformed, using defaults", exc_info=True)
        return AiConfig()


def save_config(db: Session, cfg: AiConfig) -> AiConfig:
    normalize_speech_model(cfg)
    normalize_speech_languages(cfg)
    _normalize_providers(cfg)
    row = db.get(AppSetting, CONFIG_KEY)
    payload = cfg.model_dump()
    if row is None:
        db.add(AppSetting(key=CONFIG_KEY, value=payload))
    else:
        row.value = payload
    db.commit()
    return cfg


def default_stt_model(provider: str) -> str:
    return DEFAULT_STT_MODELS.get(provider, "")


def normalize_speech_model(cfg: AiConfig) -> AiConfig:
    """Keep the transcription model in step with the speech provider: a model id
    left behind after switching providers is rejected by the new one."""
    provider = (cfg.speech.provider or "").strip()
    markers = _STT_MODEL_MARKERS.get(provider)
    if not markers:
        return cfg
    model = (cfg.speech.model or "").lower()
    if not any(marker in model for marker in markers):
        cfg.speech.model = DEFAULT_STT_MODELS[provider]
    return cfg


def normalize_speech_languages(cfg: AiConfig) -> AiConfig:
    """Primary language first, no blanks, no repeats.

    The order is the whole setting: the primary one is what every recording is
    tried in, the rest are what the "іншою мовою" retry offers. Leaving the
    primary in the alternatives would offer a retry that changes nothing.
    """
    primary = (cfg.speech.language or "").strip() or "uk-UA"
    cfg.speech.language = primary
    rest: list[str] = []
    for code in cfg.speech.alternative_languages:
        code = (code or "").strip()
        if code and code != primary and code not in rest:
            rest.append(code)
    cfg.speech.alternative_languages = rest
    return cfg


# --------------------------------------------------------------------------- #
# Provider helpers
# --------------------------------------------------------------------------- #
def custom_provider(cfg: AiConfig, provider: str) -> ProviderConfig | None:
    conf = cfg.provider(provider)
    return conf if conf is not None and conf.custom else None


def is_custom_provider(cfg: AiConfig, provider: str) -> bool:
    return custom_provider(cfg, provider) is not None


def provider_spec(cfg: AiConfig, provider: str) -> ProviderSpec | None:
    """Wire protocol + endpoint for a provider id, builtin or custom."""
    spec = PROVIDERS.get(provider)
    if spec is not None:
        return spec
    conf = custom_provider(cfg, provider)
    if conf is None:
        return None
    return ProviderSpec(
        label=conf.label,
        kind="openai",
        base_url=conf.base_url,
        fallback_models=list(conf.models) or ["free:auto"],
    )


def known_provider(cfg: AiConfig, provider: str) -> bool:
    """Any id a key may be stored under: a chat provider, a custom endpoint, or
    a speech-to-text provider that has no chat side at all."""
    return (provider in PROVIDERS or provider in STT_PROVIDER_LABELS
            or cfg.provider(provider) is not None)


def _custom_base_url(value: str) -> str:
    value = (value or "").strip().rstrip("/")
    if not value:
        raise AiConfigError("Custom OpenAI-compatible provider has no base URL")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AiConfigError("Custom OpenAI-compatible base URL must be an http(s) URL")
    return value


def _openai_base_url(cfg: AiConfig, provider: str, spec: ProviderSpec) -> str:
    conf = cfg.provider(provider)
    if conf is not None and conf.custom:
        return _custom_base_url(conf.base_url)
    # A builtin provider may still be pointed at a proxy by filling in base_url.
    if conf is not None and conf.base_url:
        return _custom_base_url(conf.base_url)
    if not spec.base_url:
        raise AiConfigError(f"{spec.label} has no base URL")
    return spec.base_url


def target_model(cfg: AiConfig, target: RouteTarget) -> str:
    """The model a route target actually calls: its own, or the first model the
    provider has marked active."""
    if target.model:
        return target.model
    if target.provider == LOCAL:
        return cfg.local.model
    conf = cfg.provider(target.provider)
    return conf.models[0] if conf and conf.models else ""


def provider_ready(db: Session, cfg: AiConfig, provider: str) -> str | None:
    """None when `provider` can be called; otherwise why it cannot."""
    if provider == LOCAL:
        return None if cfg.local.enabled else "локальний бекенд вимкнено"
    conf = cfg.provider(provider)
    if conf is None:
        return f"невідомий провайдер {provider}"
    if not conf.enabled:
        return "вимкнено"
    if conf.custom and not conf.base_url.strip():
        return "не задано base URL"
    if not get_cloud_key(db, provider) and not conf.custom:
        return "не задано API-ключ"
    return None


# --------------------------------------------------------------------------- #
# Cloud API keys
# --------------------------------------------------------------------------- #
# Stored one-per-provider so switching provider doesn't wipe the others:
# ai_cloud_key.value == {"anthropic": {"enc": "..."}, "openai": {...}}
def _keys_blob(db: Session) -> dict[str, Any]:
    row = db.get(AppSetting, CLOUD_KEY_KEY)
    return dict(row.value) if row is not None and isinstance(row.value, dict) else {}


def get_cloud_key(db: Session, provider: str) -> str | None:
    entry = _keys_blob(db).get(provider)
    if not entry or not entry.get("enc"):
        return None
    try:
        return decrypt(entry["enc"])
    except Exception:
        logger.warning("stored cloud key for %s could not be decrypted", provider, exc_info=True)
        return None


def set_cloud_key(db: Session, provider: str, raw: str | None) -> None:
    cfg = load_config(db)
    if raw and not known_provider(cfg, provider):
        raise AiConfigError(f"unknown provider {provider!r}")
    blob = _keys_blob(db)
    if raw:
        blob[provider] = {"enc": encrypt(raw)}
    else:
        blob.pop(provider, None)
    row = db.get(AppSetting, CLOUD_KEY_KEY)
    if row is None:
        db.add(AppSetting(key=CLOUD_KEY_KEY, value=blob))
    else:
        row.value = blob
    db.commit()


def _mask(key: str | None) -> str | None:
    if not key:
        return None
    return f"••••{key[-4:]}" if len(key) >= 4 else "••••"


def cloud_key_hints(db: Session, cfg: AiConfig | None = None) -> dict[str, str | None]:
    """Masked tail of the stored key for every provider (None where unset).

    Covers speech-to-text providers too — Google Cloud STT keeps its own key and
    the Settings page has to show whether it is set."""
    ids = {p["id"] for p in providers_meta(cfg)} | {p["id"] for p in stt_providers_meta(cfg)}
    return {pid: _mask(get_cloud_key(db, pid)) for pid in ids}


# --------------------------------------------------------------------------- #
# Usage metering
# --------------------------------------------------------------------------- #
def _current_month() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")


def _empty_usage() -> dict[str, Any]:
    return {"month": _current_month(),
            "cloud_tokens_in": 0, "cloud_tokens_out": 0, "cloud_cached_tokens": 0,
            "cloud_calls": 0, "cloud_errors": 0,
            "local_tokens_in": 0, "local_tokens_out": 0, "local_calls": 0, "local_errors": 0}


def load_usage(db: Session) -> dict[str, Any]:
    row = db.get(AppSetting, USAGE_KEY)
    fresh = _empty_usage()
    if row is None or not isinstance(row.value, dict) or row.value.get("month") != fresh["month"]:
        return fresh
    return {**fresh, **row.value}  # tolerate an older-shaped blob missing keys


def _record_usage(db: Session, *, backend: str, tokens_in: int = 0, tokens_out: int = 0,
                  cached_tokens: int = 0, ok: bool = True) -> None:
    usage = load_usage(db)
    p = "cloud" if backend == "cloud" else "local"
    usage[f"{p}_calls"] += 1
    usage[f"{p}_tokens_in"] += tokens_in
    usage[f"{p}_tokens_out"] += tokens_out
    if p == "cloud":
        usage["cloud_cached_tokens"] += cached_tokens
    if not ok:
        usage[f"{p}_errors"] += 1
    row = db.get(AppSetting, USAGE_KEY)
    if row is None:
        db.add(AppSetting(key=USAGE_KEY, value=usage))
    else:
        row.value = usage
    db.commit()


_LOG_DEFAULTS = {"ts": "", "task": "", "backend": "", "model": "", "tokens_in": 0,
                 "tokens_out": 0, "cached_tokens": 0, "duration_ms": 0, "ok": True, "error": "", "note": "",
                 "prompt": "", "response": ""}


def recent_log(db: Session) -> list[dict[str, Any]]:
    row = db.get(AppSetting, LOG_KEY)
    entries = row.value.get("entries", []) if row and isinstance(row.value, dict) else []
    return [{**_LOG_DEFAULTS, **e} for e in reversed(entries)]  # newest first, keys filled


_PROMPT_CAP = 2000
_RESPONSE_CAP = 4000


def log_event(db: Session, *, task: str, backend: str = "", model: str = "",
              tokens_in: int = 0, tokens_out: int = 0, cached_tokens: int = 0,
              duration_ms: int = 0,
              ok: bool = True, error: str = "", note: str = "",
              prompt: str = "", response: str = "") -> None:
    """Append one line to the AI activity log (ring buffer in app_settings).
    Every model call and every classification batch lands here — with what was
    sent (`prompt`) and what came back (`response`) — so the Settings page can
    show exactly what happened."""
    entry = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "task": task, "backend": backend, "model": model,
        "tokens_in": int(tokens_in), "tokens_out": int(tokens_out),
        "cached_tokens": int(cached_tokens),
        "duration_ms": int(duration_ms), "ok": bool(ok),
        "error": (error or "")[:800], "note": (note or "")[:300],
        "prompt": (prompt or "")[:_PROMPT_CAP], "response": (response or "")[:_RESPONSE_CAP],
    }
    row = db.get(AppSetting, LOG_KEY)
    entries = row.value.get("entries", []) if row and isinstance(row.value, dict) else []
    entries = (entries + [entry])[-LOG_MAX:]
    if row is None:
        db.add(AppSetting(key=LOG_KEY, value={"entries": entries}))
    else:
        row.value = {"entries": entries}
    db.commit()


def _cloud_over_budget(db: Session, cfg: AiConfig) -> bool:
    if cfg.monthly_token_budget <= 0:
        return False
    usage = load_usage(db)
    return usage["cloud_tokens_in"] + usage["cloud_tokens_out"] >= cfg.monthly_token_budget


# --------------------------------------------------------------------------- #
# Backend calls
# --------------------------------------------------------------------------- #
_JSON_SYSTEM = (
    "You are a data-labelling assistant for a receipt-tracking app. "
    "Reply with a single valid JSON value and nothing else — no prose, no code fences."
)


async def _local_json(cfg: AiConfig, system: str, user: str, *,
                      model: str = "") -> tuple[Any, int, int, str]:
    """Call an Ollama-compatible /api/chat endpoint, parse a JSON reply.
    Returns (parsed, prompt_tokens, completion_tokens, raw_reply_text)."""
    payload = {
        "model": model or cfg.local.model,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0, "num_ctx": cfg.local.num_ctx, "num_predict": 4096},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(f"{cfg.local.url.rstrip('/')}/api/chat", json=payload)
        if resp.status_code >= 400:
            raise RuntimeError(f"{resp.status_code} {resp.text[:300]}")
        body = resp.json()
    raw = body["message"]["content"]
    return (json.loads(raw), int(body.get("prompt_eval_count", 0)),
            int(body.get("eval_count", 0)), raw)


def _anthropic_client(key: str, workspace_id: str | None = None):
    from anthropic import AsyncAnthropic  # lazy — only needed for this provider

    headers = {"anthropic-workspace-id": workspace_id} if workspace_id else None
    return AsyncAnthropic(api_key=key, default_headers=headers)


async def _anthropic_call(
    key: str, *, model: str, system: str, content: Any, max_tokens: int,
    workspace_id: str | None = None,
) -> tuple[str, int, int, int]:
    client = _anthropic_client(key, workspace_id)
    msg = await client.messages.create(
        model=model, max_tokens=max_tokens,
        system=[{"type": "text", "text": system,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": content}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    cached = int(getattr(msg.usage, "cache_read_input_tokens", 0) or 0)
    created = int(getattr(msg.usage, "cache_creation_input_tokens", 0) or 0)
    total_input = int(msg.usage.input_tokens) + cached + created
    return text, total_input, int(msg.usage.output_tokens), cached


def _http(timeout: float = 120) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout)


#: Endpoints that rejected `max_tokens` / `temperature`, learned from their own
#: 400 and remembered for the life of the process: the newer OpenAI reasoning
#: models renamed the first and fixed the second, and there is no way to know
#: which family a model id belongs to for a custom endpoint. Keyed
#: "<base url>|<model>" so one provider's quirk never silences another's.
_RENAMED_TOKEN_FIELD: set[str] = set()
_NO_TEMPERATURE: set[str] = set()

#: A provider that says "try again in N seconds" is worth waiting for; one that
#: says nothing, or names a longer wait, is not — the task's route has a next
#: step, and sitting on a daily quota that resets tomorrow helps nobody.
RETRY_WAIT_CAP_S = 20.0


def _error_detail(resp: httpx.Response) -> str:
    """The provider's own message ("The model 'X' does not exist…") rather than
    a bare "404 Not Found"."""
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — not every error body is JSON
        return resp.text
    if isinstance(body, list):          # Google's OpenAI-compatible errors
        body = body[0] if body else {}
    if isinstance(body, dict):
        message = (body.get("error") or {}).get("message")
        if message:
            return str(message)
    return resp.text


def _unsupported_parameter(detail: str) -> str | None:
    """The request parameter a 400 is complaining about, if it names one.

    Seen in the AI journal: "Unsupported parameter: 'max_tokens' is not
    supported with this model. Use 'max_completion_tokens' instead." Every call
    to that model failed outright, which is worth one adapted retry rather than
    an unusable provider.
    """
    match = re.search(r"[Uu]nsupported (?:parameter|value): '([a-z_]+)'", detail)
    return match.group(1) if match else None


def _retry_wait(status: int, body: str) -> float | None:
    """Seconds to wait before one retry, or None to give up on this target.

    503 is a model under load and clears in seconds. 429 is either a per-minute
    limit (the provider names a `retryDelay`) or an exhausted plan quota, which
    no amount of waiting fixes — so only the first is retried. Reads the whole
    response body, not the error message: `retryDelay` sits beside it in
    `details`, which is why this takes the raw text.
    """
    if status == 503:
        return 2.0
    if status == 429:
        match = re.search(r'"?retry[-_]?[Dd]elay"?[":\s]+"?(\d+(?:\.\d+)?)s', body)
        if match:
            wait = float(match.group(1))
            return wait if wait <= RETRY_WAIT_CAP_S else None
    return None


async def _openai_call(
    base_url: str, key: str | None, *, model: str, messages: list[dict], max_tokens: int
) -> tuple[str, int, int, int]:
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    target = f"{base_url}|{model}"

    def build() -> dict:
        payload: dict[str, Any] = {"model": model, "messages": messages}
        # OpenAI caches matching prompt prefixes automatically. A deterministic
        # key improves routing for pre-5.6 models and separates each workflow's
        # cache accounting. Third-party compatible endpoints may reject it.
        if base_url.rstrip("/") == "https://api.openai.com/v1":
            stable_prefix = next(
                (m.get("content", "") for m in messages if m.get("role") == "system"), ""
            )
            if isinstance(stable_prefix, str) and stable_prefix:
                digest = hashlib.sha256(stable_prefix.encode("utf-8")).hexdigest()[:24]
                payload["prompt_cache_key"] = f"receipt-scanner-{digest}"
        payload["max_completion_tokens" if target in _RENAMED_TOKEN_FIELD
                else "max_tokens"] = max_tokens
        if target not in _NO_TEMPERATURE:
            payload["temperature"] = 0
        return payload

    status, detail = 0, ""
    async with _http() as client:
        # Three attempts at most, and only for a reason: each retry is either the
        # provider telling us to wait, or a payload it told us how to fix.
        for _attempt in range(3):
            resp = await client.post(url, headers=headers, json=build())
            if resp.status_code < 400:
                body = resp.json()
                text = body["choices"][0]["message"]["content"] or ""
                usage = body.get("usage") or {}
                details = usage.get("prompt_tokens_details") or {}
                return (text, int(usage.get("prompt_tokens", 0)),
                        int(usage.get("completion_tokens", 0)),
                        int(details.get("cached_tokens", 0) or 0))
            status, detail = resp.status_code, _error_detail(resp)
            parameter = _unsupported_parameter(detail) if status == 400 else None
            if parameter == "max_tokens" and target not in _RENAMED_TOKEN_FIELD:
                logger.info("%s wants max_completion_tokens; adapting", target)
                _RENAMED_TOKEN_FIELD.add(target)
                continue
            if parameter == "temperature" and target not in _NO_TEMPERATURE:
                logger.info("%s rejects an explicit temperature; dropping it", target)
                _NO_TEMPERATURE.add(target)
                continue
            wait = _retry_wait(status, resp.text)
            if wait is None:
                break
            logger.info("%s answered %d; one retry in %.1fs", target, status, wait)
            await asyncio.sleep(wait)
    raise RuntimeError(f"{status} {detail}")


def _image_data_uri(image: bytes, media_type: str) -> str:
    return f"data:{media_type};base64,{base64.b64encode(image).decode()}"


def _cloud_context(db: Session, cfg: AiConfig, provider: str) -> tuple[ProviderSpec, str | None, ProviderConfig | None]:
    spec = provider_spec(cfg, provider)
    if spec is None:
        raise AiConfigError(f"unknown provider {provider!r}")
    conf = cfg.provider(provider)
    key = get_cloud_key(db, provider)
    if not key and not (conf is not None and conf.custom):
        raise AiConfigError(f"{spec.label} has no API key")
    return spec, key, conf


def _provider_result(value: tuple) -> tuple[str, int, int, int]:
    """Normalize provider results while keeping older adapters/test doubles valid."""
    text, tokens_in, tokens_out = value[:3]
    cached_tokens = value[3] if len(value) > 3 else 0
    return text, int(tokens_in), int(tokens_out), int(cached_tokens)


async def _cloud_text(
    db: Session, cfg: AiConfig, *, provider: str, model: str, system: str, user: str,
    max_tokens: int,
) -> tuple[str, int, int, str, int]:
    spec, key, conf = _cloud_context(db, cfg, provider)
    if spec.kind == "anthropic":
        text, tin, tout, cached = _provider_result(await _anthropic_call(
            key, model=model, system=system, content=user, max_tokens=max_tokens,
            workspace_id=(conf.workspace_id if conf else "") or None))
    else:
        text, tin, tout, cached = _provider_result(await _openai_call(
            _openai_base_url(cfg, provider, spec), key, model=model, max_tokens=max_tokens,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}]))
    return text, tin, tout, text, cached


async def _cloud_vision(
    db: Session, cfg: AiConfig, *, provider: str, model: str, system: str, prompt: str,
    image: bytes, media_type: str, max_tokens: int, extra_images: list[bytes] | None = None,
) -> tuple[str, int, int, str, int]:
    """One model call over one photo — or over every frame of one receipt.

    A receipt taller than the camera frame is shot in parts, and the parts are
    one document: sent together in a single message they are read as one, with
    the overlap between frames resolved by the model. Sent one call each they
    would come back as several receipts, which is the thing to avoid.
    """
    spec, key, conf = _cloud_context(db, cfg, provider)
    frames = [image, *(extra_images or [])]
    if spec.kind == "anthropic":
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                         "data": base64.b64encode(frame).decode()}}
            for frame in frames
        ]
        content.append({"type": "text", "text": prompt})
        text, tin, tout, cached = _provider_result(await _anthropic_call(
            key, model=model, system=system, content=content, max_tokens=max_tokens,
            workspace_id=(conf.workspace_id if conf else "") or None))
        return text, tin, tout, text, cached
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": [
            {"type": "text", "text": prompt},
            *({"type": "image_url", "image_url": {"url": _image_data_uri(frame, media_type)}}
              for frame in frames),
        ]},
    ]
    text, tin, tout, cached = _provider_result(await _openai_call(
        _openai_base_url(cfg, provider, spec), key, model=model, messages=messages,
        max_tokens=max_tokens))
    return text, tin, tout, text, cached


def _extract_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json").strip()
    return json.loads(text)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
class CategoryGuess(BaseModel):
    category_path: str          # e.g. "продукти/овочі"; "" when the model can't tell
    confidence: float = 0.0


def build_classify_prompt(
    names: list[str],
    taxonomy: list[str],
    contexts: dict[str, dict[str, Any]] | None = None,
    db: Session | None = None,
) -> tuple[str, str]:
    """(system, user) prompt for product-category classification. Shared by the
    live API path and the manual "bring your own AI" path (api/categories.py).

    Automatic classification can attach receipt context (most importantly the
    shop name) without changing the product name used as the response/cache key.
    """
    products: list[str] | list[dict[str, Any]]
    if contexts:
        products = [
            {"name": name, **contexts.get(name, {})}
            for name in names
        ]
        products_label = "Products with receipt context"
    else:
        products = names
        products_label = "Products"
    system = (
        f"{prompt_catalog.get(db, 'json_response_rules')}\n\n"
        f"{prompt_catalog.get(db, 'language_variants')}\n\n"
        f"{prompt_catalog.get(db, 'classify_rules')}\n\n"
        f"Allowed category paths:\n{json.dumps(taxonomy, ensure_ascii=False)}\n\n"
        'Reply as {"<product>": {"category_path": "<path>", "confidence": <0..1>}, ...}.'
    )
    user = (
        f"{products_label}:\n{json.dumps(products, ensure_ascii=False)}\n\n"
        "Classify every product above."
    )
    return system, user


def _numbered(names: list[str], start: int = 1) -> dict[str, str]:
    return {str(i): n for i, n in enumerate(names, start)}


def build_manual_prompt(names: list[str], taxonomy: list[str], *, start: int = 1,
                        db: Session | None = None) -> str:
    """One-message manual prompt. Products are keyed by a **number** so the answer
    stays valid JSON even when a name contains quotes, and short so chatbots don't
    truncate it — the app maps the numbers back to names on apply."""
    return (
        f"{prompt_catalog.get(db, 'json_response_rules')}\n\n"
        f"{prompt_catalog.get(db, 'language_variants')}\n\n"
        f"{prompt_catalog.get(db, 'classify_rules')}\n\n"
        f"Allowed category paths:\n{json.dumps(taxonomy, ensure_ascii=False)}\n\n"
        f"Products (id: name):\n{json.dumps(_numbered(names, start), ensure_ascii=False, indent=0)}\n\n"
        'Reply with the SAME numeric ids as keys: '
        '{"<id>": {"category_path": "<path>", "confidence": <0..1>}, ...} and nothing else.'
    )


def build_classify_preamble(taxonomy: list[str], db: Session | None = None) -> str:
    """The shared instruction block for the batched manual flow: system rules +
    task + the allowed taxonomy, but no products. The user pastes this once, then
    feeds the numbered product lists (see `build_classify_batch`) one at a time."""
    return (
        f"{prompt_catalog.get(db, 'json_response_rules')}\n\n"
        f"{prompt_catalog.get(db, 'language_variants')}\n\n"
        f"{prompt_catalog.get(db, 'classify_rules')}\n\n"
        "I will send you several batches of products, one message at a time. Each "
        'batch is a JSON object {"<id>": "<product name>", ...}. For every product '
        "assign the single best-fitting category path from the allowed list below; "
        "if none fits, use an empty string. Reply to each batch with the SAME "
        'numeric ids as keys: {"<id>": {"category_path": "<path>", '
        '"confidence": <0..1>}, ...} and nothing else.\n\n'
        f"Allowed category paths:\n{json.dumps(taxonomy, ensure_ascii=False)}"
    )


def build_classify_batch(names: list[str], *, start: int = 1) -> str:
    """One batch payload for the flow started by `build_classify_preamble` — the
    numbered product list to paste as the next message."""
    return json.dumps(_numbered(names, start), ensure_ascii=False, indent=0)


async def classify_products(
    db: Session,
    names: list[str],
    taxonomy: list[str],
    contexts: dict[str, dict[str, Any]] | None = None,
) -> dict[str, CategoryGuess]:
    """Map each distinct product name to a category path from `taxonomy`.

    Callers must pass already-normalised, de-duplicated names and cache the
    result themselves (product_category table) — this never sees the same name
    twice in practice.
    """
    names = [n for n in dict.fromkeys(names) if n]
    if not names:
        return {}
    cfg = load_config(db)
    system, user = build_classify_prompt(names, taxonomy, contexts, db)
    preview = f"{len(names)} назв: " + ", ".join(names[:12]) + ("…" if len(names) > 12 else "")
    raw = await run_text(db, cfg, "classify", system=system, user=user, max_tokens=4096,
                         prompt_preview=preview)
    data = raw if isinstance(raw, dict) else _extract_json(raw)
    out: dict[str, CategoryGuess] = {}
    for name in names:
        entry = data.get(name) or {}
        try:
            out[name] = CategoryGuess.model_validate(entry)
        except Exception:
            out[name] = CategoryGuess(category_path="", confidence=0.0)
    return out


async def normalize_query(db: Session, phrase: str, store_code: str) -> str:
    """Turn a free-text shopping-list entry ("молоко") into a catalogue search
    query suited to one store. Callers cache this in product_alias."""
    phrase = phrase.strip()
    if not phrase:
        return ""
    cfg = load_config(db)
    user = (
        f"Store code: {store_code}. Shopping-list entry: {phrase!r}. "
        "Return the best short catalogue search query (Ukrainian, 1–3 words, no brand "
        'unless the entry named one) as {"query": "<text>"}.'
    )
    system = (f"{prompt_catalog.get(db, 'json_response_rules')}\n\n"
              f"{prompt_catalog.get(db, 'language_variants')}\n\n"
              f"{prompt_catalog.get(db, 'normalize_rules')}")
    raw = await run_text(db, cfg, "normalize", system=system, user=user, max_tokens=256,
                         prompt_preview=f'"{phrase}" · магазин {store_code}')
    data = raw if isinstance(raw, dict) else _extract_json(raw)
    return str(data.get("query") or phrase).strip()


@dataclasses.dataclass
class ReceiptOcr:
    """A vision reading of a receipt photo, and the route step that produced it."""
    data: dict | None
    service: str = ""
    model: str = ""
    steps: list[str] = dataclasses.field(default_factory=list)
    attempts: list[dict] = dataclasses.field(default_factory=list)
    error: str = ""


def _ocr_objection(raw: Any) -> str | None:
    """Why this reply is objectively unusable, or None if it is worth keeping.

    The bar is deliberately low and entirely factual (ROADMAP §2.2): a reply
    that is not JSON, or that carries neither items nor a total, or whose lines
    contradict its own total beyond any plausible discount, has not read the
    receipt — and the next model in the route is worth its cost. Everything
    softer (a collapsed screenshot, a shop name the model was unsure of, its own
    `complete` flag) is the pipeline's business to judge, not grounds for
    spending a second model.
    """
    data = raw if isinstance(raw, dict) else None
    if data is None:
        try:
            data = _extract_json(raw)
        except Exception:  # noqa: BLE001 — a reply that is not JSON is not a receipt
            return "відповідь не є структурованим JSON"
    if not isinstance(data, dict):
        return "відповідь не є структурованим JSON"

    items = [row for row in (data.get("items") or []) if isinstance(row, dict)]
    try:
        total = float(data.get("total_amount"))
    except (TypeError, ValueError):
        total = 0.0
    if not items and total <= 0:
        return "модель не повернула ні товарів, ні суми"
    if not items:
        return "модель не повернула жодного товару"
    if total <= 0:
        return "модель не повернула підсумкової суми"

    lines = 0.0
    for row in items:
        try:
            lines += float(row.get("total_price"))
        except (TypeError, ValueError):
            continue
    # Discounts, deposits and rounding routinely move the sum of the lines a
    # little; a fifth of the receipt is not a discount, it is a misread.
    if lines and abs(lines - total) > max(0.2 * total, 5.0):
        return f"сума рядків {lines:.2f} не сходиться з підсумком {total:.2f}"
    return None


async def ocr_receipt(db: Session, image: bytes, media_type: str = "image/jpeg", *,
                      start_after: tuple[str, str] | None = None,
                      extra_images: list[bytes] | None = None) -> ReceiptOcr:
    """Vision OCR of a narrow paper receipt → a raw structured dict.

    Only reached from the receipt branch of the upload router (never for personal
    documents — see ROADMAP.md § 3). Walks the "OCR чеків (фото)" route and stops
    at the first step whose answer is usable; `start_after` resumes the walk past
    a step already spent, which is what "спробувати сильнішу AI" does.

    `extra_images` are further frames of the *same* receipt — one call, one
    receipt, whatever number of photos it took to capture it.
    """
    cfg = load_config(db)
    system = (
        f"{prompt_catalog.get(db, 'json_response_rules')}\n\n"
        f"{prompt_catalog.get(db, 'language_variants')}\n\n"
        f"{prompt_catalog.get(db, 'ocr_rules')}\n\n"
        f"{prompt_catalog.get(db, 'receipt_amount_rules')}\n\n"
        "You transcribe photographed retail receipts and screenshots containing a receipt QR. "
        "Inspect the entire image, including the header above the item list. Copy the visible "
        "merchant, shop, fuel-station or location header exactly into shop_name (for example "
        "'AZK 17018, BEREZHANY'); do not omit it merely because it is outside the paper-like "
        "receipt card. Never invent a date or time: use null when it is not visibly printed; "
        "fiscal QR metadata will be merged separately. If the UI says 'show more', 'показати "
        "більше', or otherwise visibly collapses/hides receipt lines, set complete=false; never "
        "stretch the one visible item's price to equal the receipt total. For a full fiscal "
        "check set complete=true and transcribe every item. Reply with one JSON object: "
        '{"complete" (boolean), "seller_name", "shop_name", "shop_address", '
        '"payment_type" (cash/card/null), "purchased_at" (ISO 8601 or null), "total_amount" (number), '
        '"currency", "total_discount" (number|null), "delivery_amount" (number|null), '
        '"extra_charges":[{"name","amount"}], '
        '"items":[{"name","quantity","unit_price","total_price","discount" (number|null)}]}. '
    )
    frames = 1 + len(extra_images or [])
    if frames > 1:
        # Without this the model answers per picture and the caller gets three
        # short receipts instead of one long one.
        system += (
            f" The {frames} images are parts of ONE receipt, shot in order and overlapping: "
            "merge them into a single receipt, do not repeat an item that appears in two "
            "images, keep the original item names exactly as printed, never translate them, "
            "and use null for anything you cannot read."
        )
    prompt = ("Transcribe this receipt." if frames == 1
              else f"Transcribe this receipt, photographed in {frames} overlapping parts.")
    try:
        answer = await run_vision(db, cfg, "ocr", system=system, prompt=prompt,
                                  image=image, media_type=media_type, max_tokens=4096,
                                  prompt_preview=(f"фото чека ({len(image)} B)" if frames == 1
                                                  else f"{frames} фото одного чека"),
                                  accept=_ocr_objection, start_after=start_after,
                                  extra_images=extra_images)
    except AiConfigError as exc:
        logger.warning("receipt OCR has no usable backend: %s", exc)
        return ReceiptOcr(None, error=str(exc))
    raw = answer.value
    try:
        data = raw if isinstance(raw, dict) else _extract_json(raw)
    except Exception:  # noqa: BLE001 — _ocr_objection already rejects these
        logger.warning("receipt OCR returned unparsable output", exc_info=True)
        return ReceiptOcr(None, service=answer.provider, model=answer.model,
                          steps=answer.steps, attempts=answer.attempts)
    return ReceiptOcr(data, service=answer.provider, model=answer.model,
                      steps=[*answer.steps, f"{answer.provider}/{answer.model}"],
                      attempts=answer.attempts)


def ocr_route_steps(cfg: AiConfig) -> list[tuple[str, str]]:
    """Every (service, model) the "OCR чеків (фото)" route names, in order."""
    return [(t.provider, target_model(cfg, t)) for t in cfg.route("ocr")
            if target_model(cfg, t)]


def next_ocr_step(db: Session, after: tuple[str, str] | None,
                  cfg: "AiConfig | None" = None) -> tuple[str, str, str] | None:
    """The step after `after` in the current OCR route: (service, model, label).

    Looked up by the service+model pair rather than by a stored position, so a
    route the user has since reordered still escalates in the order they see.
    Returns None when `after` was the last usable step — the caller then hides
    the "stronger AI" action instead of offering a step that cannot run.
    """
    cfg = cfg if cfg is not None else load_config(db)
    steps = ocr_route_steps(cfg)
    start = 0
    if after is not None and after in steps:
        start = steps.index(after) + 1
    for provider, model in steps[start:]:
        if provider_ready(db, cfg, provider) is None:
            conf = cfg.provider(provider)
            label = (conf.label if conf else provider) or provider
            return provider, model, label
    return None


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class AiAnswer:
    """What a routed call produced, and which step of the route produced it.

    The step matters beyond bookkeeping: the user is shown it ("Розпізнано AI:
    Google Gemini / gemini-…") and the "try a stronger AI" action resumes the
    route *after* this exact service+model pair, so escalation follows the order
    the user set in AI → Маршрути задач rather than a remembered index.
    """
    value: Any
    provider: str = ""
    model: str = ""
    #: Every "provider/model" this call went through, the successful one last.
    steps: list[str] = dataclasses.field(default_factory=list)
    #: One row per step the route actually spent, successful or not:
    #: {service, model, ok, ms, tokens_in, tokens_out, cached_tokens, error}. This is what the
    #: per-receipt recognition log and the model statistics are built from — the
    #: ai_log ring buffer holds the same facts but only for the last N calls,
    #: across every task at once.
    attempts: list[dict] = dataclasses.field(default_factory=list)


async def _run(db: Session, cfg: AiConfig, task: str, *, make_call,
               prompt_preview: str = "", accept=None,
               start_after: tuple[str, str] | None = None,
               skip: set[str] | None = None) -> AiAnswer:
    """Walk the task's priority list (Settings block 2) and use the first target
    that is enabled, configured and answers *usably*.

    `make_call(provider, model)` returns an awaitable yielding
    (result, tokens_in, tokens_out, raw_reply_text[, cached_tokens]), or None when
    that target cannot serve this kind of request (a local model asked for vision). Every
    attempt — success or failure — is metered (ai_usage) and logged (ai_log)
    together with `prompt_preview` and the raw model reply.

    `accept(result)` returns None for an answer worth keeping, or why it is
    objectively broken — empty, unparsable, missing the fields the task exists
    to produce. Only that moves the walk on to the next step; a model's own
    opinion of how well it did never does, because paying for a second model on
    a self-reported "confidence" spends real money on nothing.

    `start_after` resumes the route after a service+model pair already used (the
    user pressing "try a stronger AI"), and `skip` drops pairs already spent, so
    an escalation never re-runs work that is already on file.
    """
    attempted: list[str] = []
    errors: list[str] = []
    attempts: list[dict] = []
    resuming = start_after is not None
    for target in cfg.route(task):
        provider = target.provider
        model = target_model(cfg, target)
        if resuming:
            # Matched on the pair, not on position: the user may have reordered
            # or edited the route since the answer we are escalating from.
            if (provider, model) == start_after:
                resuming = False
            continue
        if skip and f"{provider}/{model}" in skip:
            continue
        reason = provider_ready(db, cfg, provider)
        if reason:
            errors.append(f"{provider}: {reason}")
            continue
        if not model:
            errors.append(f"{provider}: не обрано модель")
            continue
        if provider != LOCAL and _cloud_over_budget(db, cfg):
            errors.append(f"{provider}: місячний бюджет токенів вичерпано")
            log_event(db, task=task, backend=provider, model=model, ok=False,
                      prompt=prompt_preview, error="місячний бюджет токенів вичерпано")
            continue
        call = make_call(provider, model)
        if call is None:
            errors.append(f"{provider}: не підтримує цей тип запиту")
            continue
        attempted.append(f"{provider}/{model}")
        t0 = time.monotonic()
        try:
            call_result = await call()
            result, tin, tout, raw = call_result[:4]
            cached = int(call_result[4]) if len(call_result) > 4 else 0
        except Exception as exc:  # noqa: BLE001 — fall through to the next target
            dur = int((time.monotonic() - t0) * 1000)
            logger.warning("AI target %s/%s failed for %s: %s", provider, model, task, exc)
            _record_usage(db, backend=_meter_bucket(provider), ok=False)
            log_event(db, task=task, backend=provider, model=model, duration_ms=dur,
                      ok=False, error=str(exc), prompt=prompt_preview)
            errors.append(f"{provider}: {exc}")
            attempts.append({"service": provider, "model": model, "ok": False,
                             "ms": dur, "tokens_in": 0, "tokens_out": 0,
                             "error": str(exc)[:300]})
            continue
        dur = int((time.monotonic() - t0) * 1000)
        _record_usage(db, backend=_meter_bucket(provider), tokens_in=tin, tokens_out=tout,
                      cached_tokens=cached)
        objection = accept(result) if accept is not None else None
        log_event(db, task=task, backend=provider, model=model, tokens_in=tin,
                  tokens_out=tout, cached_tokens=cached, duration_ms=dur,
                  ok=objection is None,
                  error=objection or None, prompt=prompt_preview, response=str(raw))
        attempts.append({"service": provider, "model": model, "ok": objection is None,
                         "ms": dur, "tokens_in": int(tin), "tokens_out": int(tout),
                         "cached_tokens": cached,
                         "error": (objection or "")[:300]})
        if objection is not None:
            # Answered, but not with the thing that was asked for. Worth the
            # next step in the route — and worth saying which step failed and
            # why, since that is what the AI journal is read for.
            logger.info("AI target %s/%s answered unusably for %s: %s",
                        provider, model, task, objection)
            errors.append(f"{provider}/{model}: {objection}")
            continue
        return AiAnswer(result, provider=provider, model=model, steps=list(attempted),
                        attempts=attempts)
    raise AiConfigError(
        f"no usable AI backend for {task!r}"
        + (f" (tried {', '.join(attempted)})" if attempted else "")
        + (f": {'; '.join(errors)}" if errors else "")
    )


def _meter_bucket(provider: str) -> str:
    """The usage meter counts two pots only: what ran on our hardware, and what
    was paid for outside it."""
    return "local" if provider == LOCAL else "cloud"


async def run_text(db: Session, cfg: AiConfig, task: str, *, system: str, user: str,
                   max_tokens: int, prompt_preview: str = "") -> Any:
    """Text prompt through the task's routing. Returns a parsed dict (local
    backend, which answers in JSON mode) or the raw reply text (cloud)."""
    def make_call(provider: str, model: str):
        if provider == LOCAL:
            return lambda: _local_json(cfg, system, user, model=model)
        return lambda: _cloud_text(db, cfg, provider=provider, model=model, system=system,
                                   user=user, max_tokens=max_tokens)

    answer = await _run(db, cfg, task, make_call=make_call, prompt_preview=prompt_preview)
    return answer.value


async def run_vision(db: Session, cfg: AiConfig, task: str, *, system: str, prompt: str,
                     image: bytes, media_type: str, max_tokens: int,
                     prompt_preview: str = "", accept=None,
                     start_after: tuple[str, str] | None = None,
                     extra_images: list[bytes] | None = None) -> AiAnswer:
    """Image prompt through the task's routing. Local targets are skipped: the
    Ollama path here speaks text only.

    Returns the whole `AiAnswer` — for a photo the caller needs to know which
    model read it, both to show the user and to offer the next step up.
    """
    def make_call(provider: str, model: str):
        if provider == LOCAL:
            return None
        return lambda: _cloud_vision(db, cfg, provider=provider, model=model, system=system,
                                     prompt=prompt, image=image, media_type=media_type,
                                     max_tokens=max_tokens, extra_images=extra_images)

    return await _run(db, cfg, task, make_call=make_call, prompt_preview=prompt_preview,
                      accept=accept, start_after=start_after)


# --------------------------------------------------------------------------- #
# Model listing + connectivity checks (for the Settings page)
# --------------------------------------------------------------------------- #
async def list_local_models(cfg: AiConfig) -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{cfg.local.url.rstrip('/')}/api/tags")
            resp.raise_for_status()
            return sorted(m["name"] for m in resp.json().get("models", []))
    except Exception:  # noqa: BLE001 — best effort, dropdown falls back to the current value
        return []


async def _fetch_catalogue(cfg: AiConfig, provider: str, key: str | None) -> list[dict[str, str]]:
    """Live model list of one provider, or [] when it cannot be read.

    Kept separate from `list_provider_models` so the key is read from the DB by
    the caller: the catalogues of all providers are fetched concurrently, and a
    sync Session must not be touched from several tasks at once.
    """
    spec = provider_spec(cfg, provider)
    if spec is None:
        return []
    conf = cfg.provider(provider)
    if not key and not (conf is not None and conf.custom):
        return []
    try:
        if spec.kind == "anthropic":
            client = _anthropic_client(key, (conf.workspace_id if conf else "") or None)
            got = [m async for m in client.models.list()]
            return [{"id": m.id, "display_name": getattr(m, "display_name", m.id)} for m in got]
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{_openai_base_url(cfg, provider, spec).rstrip('/')}/models",
                                    headers={"Authorization": f"Bearer {key}"} if key else {})
            resp.raise_for_status()
            data = resp.json().get("data", [])
        # Google's OpenAI-compat endpoint returns ids like "models/gemini-2.5-flash";
        # strip the prefix so what's stored is what /chat/completions wants.
        ids = sorted({str(m["id"]).removeprefix("models/") for m in data if m.get("id")})
        ids = [i for i in ids if spec.owns_model(i)] or ids   # drop unrelated ids (embeddings, tts…)
        return [{"id": i, "display_name": i} for i in ids]
    except Exception as exc:  # noqa: BLE001 — dropdown falls back to the static list
        logger.info("model list unavailable for %s: %s", provider, exc)
        return []


async def list_provider_catalogue(
    db: Session, cfg: AiConfig, provider: str
) -> tuple[list[dict[str, str]], bool]:
    """One provider's catalogue plus whether it came from its API.

    This is what the per-service "refresh models" button calls: asking a single
    provider must not wait on the other seven, and a key just saved for this one
    service should be visible in its checklist immediately.
    """
    spec = provider_spec(cfg, provider)
    if spec is None:
        return [], False
    live = await _fetch_catalogue(cfg, provider, get_cloud_key(db, provider))
    if live:
        return live, True
    return [{"id": m, "display_name": m} for m in spec.fallback_models], False


async def list_provider_models(db: Session, cfg: AiConfig, provider: str) -> list[dict[str, str]]:
    """The catalogue of one provider, for the model checklist in Settings block 1.
    Falls back to the static list when the provider has no key or is unreachable."""
    models, _live = await list_provider_catalogue(db, cfg, provider)
    return models


async def list_all_models(
    db: Session, cfg: AiConfig
) -> tuple[dict[str, list[dict[str, str]]], set[str]]:
    """Catalogues of every configured provider, keyed by provider id, plus the
    set of ids whose list really came from the provider's API.

    Every provider is asked, not only the enabled ones: the page needs the list
    to let the user tick models for a service they are switching on right now.
    Providers are queried concurrently — one slow endpoint used to add its
    timeout to every other provider's wait.
    """
    ids = [conf.id for conf in cfg.providers]
    keys = {pid: get_cloud_key(db, pid) for pid in ids}          # DB reads before any await
    results = await asyncio.gather(*(_fetch_catalogue(cfg, pid, keys[pid]) for pid in ids))
    out: dict[str, list[dict[str, str]]] = {}
    live: set[str] = set()
    for pid, models in zip(ids, results):
        spec = provider_spec(cfg, pid)
        if models:
            live.add(pid)
            out[pid] = models
        elif spec is not None:
            out[pid] = [{"id": m, "display_name": m} for m in spec.fallback_models]
    return out, live


def _stt_choices(provider: str) -> list[str]:
    return list(STT_MODEL_CHOICES.get(provider, []))


# What makes a live model id plausibly a transcription model. Deliberately
# narrower than _STT_MODEL_MARKERS (which also accepts "gpt-", to keep an
# already-configured OpenAI id): a catalogue is mostly chat models, and offering
# "gpt-4o" or "gemini-2.5-pro" as a recogniser would just produce 400s.
_STT_ID_MARKERS: tuple[str, ...] = ("whisper", "transcribe", "-stt", "speech", "audio", "voxtral")


def _is_stt_model(model: str) -> bool:
    low = model.lower()
    return any(marker in low for marker in _STT_ID_MARKERS)


def list_stt_models(cfg: AiConfig,
                    catalogues: dict[str, list[dict[str, str]]] | None = None) -> dict[str, list[str]]:
    """Transcription model ids per speech provider, live where possible.

    Speech rides a different endpoint than chat but the same catalogue, so the
    provider's live model list (fetched once by `list_all_models` and passed in
    here) is filtered down to transcription-capable ids. Only live catalogues
    may be passed: a static chat fallback would offer "gpt-4o" as a recogniser.
    Google Cloud Speech has no listing API and its recognisers are a fixed set,
    so the static choices stand. The curated choices always come first — they
    are the ids known to work, the live list only adds what is new.
    """
    out: dict[str, list[str]] = {}
    for meta in stt_providers_meta(cfg):
        pid = meta["id"]
        models = _stt_choices(pid)
        for entry in (catalogues or {}).get(pid, []):
            mid = entry["id"]
            if mid not in models and _is_stt_model(mid):
                models.append(mid)
        out[pid] = models
    return out


# Hyphen/dot-delimited tokens that mark a model id as not a general chat model.
_NON_CHAT_TOKENS = {
    "embedding", "embeddings", "embed", "tts", "audio", "whisper", "transcribe",
    "speech", "dalle", "imagen", "image", "veo", "guard", "moderation",
    "rerank", "reranker", "realtime", "live", "aqa", "computer",  # computer-use
}
_CHEAP_TOKENS = {"flash", "mini", "haiku", "lite", "small", "nano", "air", "turbo", "fast"}


def _pick_default_model(live_ids: list[str]) -> str:
    """Best general-purpose model from a provider's live list — skips
    audio/image/embedding/etc variants and prefers a cheap fast tier."""
    import re

    def toks(i: str) -> list[str]:
        return re.split(r"[-.]", i.lower())

    chat = [i for i in live_ids if not (set(toks(i)) & _NON_CHAT_TOKENS)] or live_ids

    def score(i: str) -> tuple:
        t = toks(i)
        cheap = bool(set(t) & _CHEAP_TOKENS)
        reasoning = any(x in ("reasoner", "thinking", "reasoning") or re.fullmatch(r"o\d+", x)
                        for x in t)
        preview = any(x in ("preview", "exp", "experimental", "beta") for x in t)
        dated = t[-1].isdigit() and len(t[-1]) >= 4
        return (0 if cheap else 1, 1 if reasoning else 0,
                1 if preview else 0, 1 if dated else 0, len(i))

    return sorted(chat, key=score)[0]


def heal_models(cfg: AiConfig, catalogues: dict[str, list[str]]) -> bool:
    """Keep the stored config in step with what the providers still serve.

    A model that a provider has retired stays selected forever otherwise, and
    every routed call through it 404s. Only providers whose catalogue actually
    came back are touched — an unreachable endpoint must not wipe a selection.
    Returns True when something changed.
    """
    changed = False
    for conf in cfg.providers:
        live = catalogues.get(conf.id) or []
        if not live:
            continue
        kept = [m for m in conf.models if m in live]
        if kept != conf.models:
            conf.models = kept
            changed = True
        if not conf.models and conf.enabled:
            conf.models = [_pick_default_model(live)]
            changed = True
    for task, targets in cfg.routing.items():
        for target in targets:
            if target.provider == LOCAL:
                continue
            conf = cfg.provider(target.provider)
            if conf is None or not catalogues.get(conf.id):
                continue
            if target.model and target.model not in conf.models:
                target.model = conf.models[0] if conf.models else ""
                changed = True
    return changed


def usage_url(provider: str, cfg: AiConfig | None = None) -> str:
    """Provider-side usage page for `provider`, or "" when it has none."""
    if cfg is not None:
        conf = cfg.provider(provider)
        if conf is not None and conf.usage_url:
            return conf.usage_url
    return PROVIDER_USAGE_URLS.get(provider, "")


def providers_meta(cfg: AiConfig | None = None) -> list[dict[str, Any]]:
    """Catalogue for the Settings page: every provider that can be switched on,
    with the protocol facts the page needs (wire kind, static model list)."""
    out: list[dict[str, Any]] = [
        {"id": name, "label": spec.label, "kind": spec.kind, "custom": False,
         "usage_url": usage_url(name, cfg), "fallback_models": list(spec.fallback_models)}
        for name, spec in PROVIDERS.items()
    ]
    if cfg is None:
        return out
    out.extend(
        {"id": p.id, "label": p.label, "kind": "openai", "custom": True,
         "usage_url": p.usage_url, "fallback_models": list(p.models)}
        for p in cfg.providers if p.custom
    )
    return out


def stt_providers_meta(cfg: AiConfig | None = None) -> list[dict[str, str]]:
    """What can be picked for voice recognition, in dropdown order.

    Chat providers appear only when their protocol carries a transcription
    endpoint (`kind == "openai"`); Anthropic has none, so it is absent.
    """
    out = [{"id": GOOGLE_STT, "label": STT_PROVIDER_LABELS[GOOGLE_STT], "kind": GOOGLE_STT,
            "usage_url": usage_url(GOOGLE_STT)}]
    out += [{"id": name, "label": spec.label, "kind": "openai", "usage_url": usage_url(name)}
            for name, spec in PROVIDERS.items() if spec.kind == "openai"]
    if cfg is not None:
        out += [{"id": p.id, "label": p.label, "kind": "openai", "usage_url": p.usage_url}
                for p in cfg.providers if p.custom]
    return out


async def check_local(cfg: AiConfig) -> dict[str, Any]:
    if not cfg.local.enabled:
        return {"ok": False, "detail": "вимкнено"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{cfg.local.url.rstrip('/')}/api/tags")
            resp.raise_for_status()
            models = [m["name"] for m in resp.json().get("models", [])]
        if cfg.local.model not in models:
            return {"ok": False, "detail": f"модель {cfg.local.model} не встановлена", "models": models}
        return {"ok": True, "detail": f"{len(models)} моделей доступно", "models": models}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": str(exc)}


async def check_provider_key(
    provider: str, api_key: str, model: str | None = None,
    workspace_id: str | None = None, base_url: str | None = None,
) -> dict[str, Any]:
    """Minimal ping with an explicit key — used by the "Тест" button next to the
    API-key field, so a key can be validated before it's saved. Does NOT touch
    the DB and does NOT record token usage."""
    spec = PROVIDERS.get(provider)
    custom = spec is None
    if custom:
        spec = ProviderSpec(label=provider, kind="openai", base_url=base_url or "",
                            fallback_models=[model] if model else [])
    if not api_key and not custom:
        return {"ok": False, "detail": "порожній ключ"}
    model = model or (spec.fallback_models[0] if spec.fallback_models else "")
    if not model:
        return {"ok": False, "detail": "не обрано модель для перевірки"}
    try:
        if spec.kind == "anthropic":
            await _anthropic_call(api_key, model=model, system="Reply with: ok",
                                  content="ping", max_tokens=16, workspace_id=workspace_id)
        else:
            url = _custom_base_url(base_url) if (custom or base_url) else spec.base_url
            if not url:
                return {"ok": False, "detail": f"{spec.label} has no base URL"}
            await _openai_call(url, api_key, model=model, max_tokens=16,
                               messages=[{"role": "user", "content": "ping"}])
        return {"ok": True, "detail": f"{spec.label} · {model}: ключ робочий"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": str(exc)}


async def check_provider(db: Session, cfg: AiConfig, provider: str) -> dict[str, Any]:
    """Live ping of one configured provider with its stored key and first active
    model — the per-provider "Перевірити" button in Settings block 1."""
    conf = cfg.provider(provider)
    spec = provider_spec(cfg, provider)
    if conf is None or spec is None:
        return {"ok": False, "detail": f"невідомий провайдер {provider}"}
    reason = provider_ready(db, cfg, provider)
    if reason:
        return {"ok": False, "detail": reason}
    model = conf.models[0] if conf.models else ""
    if not model:
        return {"ok": False, "detail": "не обрано жодної активної моделі"}
    try:
        text, tin, tout, _ = await _cloud_text(db, cfg, provider=provider, model=model,
                                               system="Reply with: ok", user="ping", max_tokens=16)
        _record_usage(db, backend="cloud", tokens_in=tin, tokens_out=tout)
        log_event(db, task="check", backend=provider, model=model, tokens_in=tin,
                  tokens_out=tout, ok=True, prompt="ping", response=text)
        return {"ok": True, "detail": f"{conf.label} · {model}: {text.strip()[:40]}"}
    except Exception as exc:  # noqa: BLE001
        log_event(db, task="check", backend=provider, model=model, ok=False, error=str(exc))
        return {"ok": False, "detail": str(exc)}


async def check_all(db: Session, cfg: AiConfig) -> dict[str, Any]:
    """Connectivity of the local backend and of every enabled provider."""
    providers: dict[str, Any] = {}
    for conf in cfg.providers:
        if conf.enabled:
            providers[conf.id] = await check_provider(db, cfg, conf.id)
    return {"local": await check_local(cfg), "providers": providers}
