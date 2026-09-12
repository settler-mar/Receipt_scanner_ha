"""Config/persistence/routing for app.services.ai (no live model calls)."""
import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import models  # noqa: F401 — registers tables
from app.services import ai


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


def _target(provider, model=""):
    return ai.RouteTarget(provider=provider, model=model)


def _enable(cfg, provider, *models):
    conf = cfg.provider(provider)
    conf.enabled = True
    conf.models = list(models)
    return conf


async def _ok(result=None, tin=5, tout=1):
    return (result if result is not None else {"query": "молоко"}), tin, tout, '{"query": "молоко"}'


def test_defaults_when_unset(db):
    cfg = ai.load_config(db)
    # Nothing is switched on out of the box, and nothing is routed anywhere.
    assert [p.id for p in cfg.providers] == list(ai.PROVIDERS)
    assert all(not p.enabled and not p.models for p in cfg.providers)
    assert cfg.route("ocr") == []
    assert ai.cloud_key_hints(db)["anthropic"] is None


def test_config_roundtrip_and_malformed_blob(db):
    cfg = ai.load_config(db)
    cfg.local.enabled = True
    cfg.monthly_token_budget = 123
    _enable(cfg, "openai", "gpt-4o-mini")
    cfg.routing["classify"] = [_target(ai.LOCAL), _target("openai", "gpt-4o-mini")]
    ai.save_config(db, cfg)

    saved = ai.load_config(db)
    assert saved.local.enabled is True and saved.monthly_token_budget == 123
    assert saved.provider("openai").models == ["gpt-4o-mini"]
    assert [t.provider for t in saved.route("classify")] == ["local", "openai"]

    db.get(models.AppSetting, ai.CONFIG_KEY).value = {"providers": "garbage"}
    db.commit()
    assert ai.load_config(db).local.enabled is False  # falls back to defaults


def test_legacy_config_is_migrated_on_read(db):
    """Configs written before the providers/routing split named one active cloud
    provider and routed backend *kinds*; they must keep working unchanged."""
    db.add(models.AppSetting(key=ai.CONFIG_KEY, value={
        "local": {"enabled": True, "model": "qwen2.5:3b-instruct"},
        "cloud": {
            "enabled": True, "provider": "openai",
            "model_text": "gpt-4o-mini", "model_vision": "gpt-4o",
            "stt_provider": "groq", "model_stt": "whisper-large-v3-turbo",
            "stt_language": "uk-UA",
        },
        "routing": {"classify": ["local", "cloud"], "ocr": ["cloud"]},
        "monthly_token_budget": 7,
    }))
    db.commit()

    cfg = ai.load_config(db)
    openai = cfg.provider("openai")
    assert openai.enabled is True and openai.models == ["gpt-4o-mini", "gpt-4o"]
    assert cfg.provider("anthropic").enabled is False
    assert [(t.provider, t.model) for t in cfg.route("classify")] == [
        ("local", "qwen2.5:3b-instruct"), ("openai", "gpt-4o-mini")]
    # The vision task keeps the vision model.
    assert [(t.provider, t.model) for t in cfg.route("ocr")] == [("openai", "gpt-4o")]
    assert cfg.speech.enabled is True and cfg.speech.provider == "groq"
    assert cfg.speech.model == "whisper-large-v3-turbo"
    assert cfg.monthly_token_budget == 7


def test_cloud_key_is_encrypted_at_rest_and_masked(db):
    ai.set_cloud_key(db, "openai", "sk-openai-secret-abcd")
    stored = db.get(models.AppSetting, ai.CLOUD_KEY_KEY).value["openai"]["enc"]
    assert "secret" not in stored and stored != "sk-openai-secret-abcd"
    assert ai.get_cloud_key(db, "openai") == "sk-openai-secret-abcd"
    assert ai.get_cloud_key(db, "anthropic") is None  # per-provider isolation
    assert ai.cloud_key_hints(db)["openai"] == "••••abcd"
    ai.set_cloud_key(db, "openai", None)
    assert ai.get_cloud_key(db, "openai") is None


def test_usage_meter_resets_per_month(db):
    ai._record_usage(db, backend="cloud", tokens_in=10, tokens_out=5)
    ai._record_usage(db, backend="local")
    usage = ai.load_usage(db)
    assert usage["cloud_tokens_in"] == 10 and usage["cloud_calls"] == 1 and usage["local_calls"] == 1

    db.get(models.AppSetting, ai.USAGE_KEY).value = {
        "month": "1999-01", "cloud_tokens_in": 999, "cloud_tokens_out": 0,
        "cloud_calls": 9, "local_calls": 9,
    }
    db.commit()
    assert ai.load_usage(db)["cloud_tokens_in"] == 0  # stale month ignored


def test_run_raises_when_nothing_is_routed(db):
    cfg = ai.load_config(db)  # empty routing

    with pytest.raises(ai.AiConfigError):
        asyncio.run(ai._run(db, cfg, "classify", make_call=lambda p, m: _ok))


def test_run_walks_the_priority_list(db):
    cfg = ai.load_config(db)
    cfg.local.enabled = True
    _enable(cfg, "anthropic", "claude-haiku-4-5")
    cfg.routing["normalize"] = [_target(ai.LOCAL), _target("anthropic")]
    ai.save_config(db, cfg)
    ai.set_cloud_key(db, "anthropic", "sk-ant-x")

    used = []

    def make_call(provider, model):
        used.append((provider, model))

        async def call():
            if provider == ai.LOCAL:
                raise RuntimeError("ollama down")
            return await _ok()

        return call

    assert asyncio.run(ai._run(db, cfg, "normalize", make_call=make_call)) == {"query": "молоко"}
    # The model comes from the provider's active list when the route leaves it empty.
    assert used == [("local", "qwen2.5:3b-instruct"), ("anthropic", "claude-haiku-4-5")]


def test_a_target_without_key_or_switched_off_is_skipped(db):
    cfg = ai.load_config(db)
    _enable(cfg, "mistral", "mistral-small-latest")   # enabled, but no key yet
    cfg.provider("openai").models = ["gpt-4o-mini"]   # has a key below, but disabled
    cfg.routing["normalize"] = [_target("openai"), _target("mistral")]
    ai.save_config(db, cfg)
    ai.set_cloud_key(db, "openai", "sk-openai-x")

    with pytest.raises(ai.AiConfigError):
        asyncio.run(ai._run(db, cfg, "normalize", make_call=lambda p, m: _ok))

    ai.set_cloud_key(db, "mistral", "sk-mistral-x")
    called = []

    def make_call(provider, model):
        called.append(provider)
        return _ok

    assert asyncio.run(ai._run(db, cfg, "normalize", make_call=make_call)) == {"query": "молоко"}
    assert called == ["mistral"]   # the disabled provider never got a turn


def test_cloud_dropped_when_over_budget_but_local_still_runs(db):
    cfg = ai.load_config(db)
    cfg.local.enabled = True
    _enable(cfg, "anthropic", "claude-haiku-4-5")
    cfg.monthly_token_budget = 5
    cfg.routing["normalize"] = [_target("anthropic"), _target(ai.LOCAL)]
    ai.save_config(db, cfg)
    ai.set_cloud_key(db, "anthropic", "sk-ant-x")
    ai._record_usage(db, backend="cloud", tokens_in=10, tokens_out=0)

    seen = []

    def make_call(provider, model):
        seen.append(provider)
        return _ok

    assert asyncio.run(ai._run(db, cfg, "normalize", make_call=make_call)) == {"query": "молоко"}
    assert seen == ["local"]   # the paid target was skipped, the free one ran


def test_vision_skips_local_targets(db):
    cfg = ai.load_config(db)
    cfg.local.enabled = True
    _enable(cfg, "openai", "gpt-4o")
    cfg.routing["ocr"] = [_target(ai.LOCAL), _target("openai")]
    ai.save_config(db, cfg)
    ai.set_cloud_key(db, "openai", "sk-openai-x")

    seen = []

    async def fake_vision(db_, cfg_, *, provider, model, **kwargs):
        seen.append((provider, model))
        return '{"items": []}', 4, 2, '{"items": []}'

    original = ai._cloud_vision
    ai._cloud_vision = fake_vision
    try:
        out = asyncio.run(ai.run_vision(db, cfg, "ocr", system="s", prompt="p", image=b"jpg",
                                        media_type="image/jpeg", max_tokens=64))
    finally:
        ai._cloud_vision = original
    assert out == '{"items": []}'
    assert seen == [("openai", "gpt-4o")]


def test_custom_provider_needs_a_base_url_but_no_key(db):
    cfg = ai.load_config(db)
    cfg.providers.append(ai.ProviderConfig(
        id="custom_openai_vllm", label="Local vLLM", enabled=True,
        models=["local-muse"], custom=True))
    cfg.routing["normalize"] = [_target("custom_openai_vllm")]
    ai.save_config(db, cfg)

    # No base URL yet → the target is not callable.
    assert ai.provider_ready(db, ai.load_config(db), "custom_openai_vllm") == "не задано base URL"
    with pytest.raises(ai.AiConfigError):
        asyncio.run(ai._run(db, cfg, "normalize", make_call=lambda p, m: _ok))

    cfg = ai.load_config(db)
    cfg.provider("custom_openai_vllm").base_url = "http://localhost:8000/v1"
    ai.save_config(db, cfg)
    assert ai.provider_ready(db, ai.load_config(db), "custom_openai_vllm") is None
    assert asyncio.run(ai._run(db, cfg, "normalize", make_call=lambda p, m: _ok)) == {"query": "молоко"}


def test_multiple_custom_providers_have_distinct_keys(db):
    cfg = ai.load_config(db)
    cfg.providers += [
        ai.ProviderConfig(id="custom_openai_hf", label="HF Muse",
                          base_url="https://hf.example/v1", custom=True),
        ai.ProviderConfig(id="custom_openai_local", label="Local vLLM",
                          base_url="http://localhost:8000/v1", custom=True),
    ]
    ai.save_config(db, cfg)

    saved = ai.load_config(db)
    assert [p["id"] for p in ai.providers_meta(saved)][-2:] == [
        "custom_openai_hf", "custom_openai_local"]
    ai.set_cloud_key(db, "custom_openai_hf", "hf-secret")
    ai.set_cloud_key(db, "custom_openai_local", "local-secret")
    assert ai.get_cloud_key(db, "custom_openai_hf") == "hf-secret"
    assert ai.get_cloud_key(db, "custom_openai_local") == "local-secret"
    hints = ai.cloud_key_hints(db, saved)
    assert hints["custom_openai_hf"] == "••••cret" and hints["custom_openai_local"] == "••••cret"


def test_custom_openai_base_url_validation():
    assert ai._custom_base_url(" https://example.test/v1/ ") == "https://example.test/v1"
    with pytest.raises(ai.AiConfigError):
        ai._custom_base_url("")
    with pytest.raises(ai.AiConfigError):
        ai._custom_base_url("ftp://example.test/v1")


def test_list_provider_models_falls_back_without_a_key(db):
    cfg = ai.load_config(db)
    models_ = asyncio.run(ai.list_provider_models(db, cfg, "openai"))
    assert {m["id"] for m in models_} == set(ai.PROVIDERS["openai"].fallback_models)
    assert asyncio.run(ai.list_local_models(cfg)) == []  # nothing on localhost:11434


def test_check_provider_key_guards(db):
    # unknown provider / empty key never touch the network
    assert asyncio.run(ai.check_provider_key("openai", ""))["ok"] is False
    assert asyncio.run(ai.check_provider_key("custom_openai", "sk-test"))["ok"] is False


def test_pick_default_model_prefers_cheap_general_chat():
    assert ai._pick_default_model(
        ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-image",
         "gemini-2.5-flash-preview-tts", "text-embedding-004"]
    ) == "gemini-2.5-flash"
    assert ai._pick_default_model(
        ["gpt-4o", "gpt-4o-mini", "o3-mini", "dall-e-3", "whisper-1"]
    ) == "gpt-4o-mini"
    # "mini" inside "gemini" must NOT count as the cheap tier (token, not substring)
    assert ai._pick_default_model(["gemini-2.5-pro"]) == "gemini-2.5-pro"


def test_heal_models_drops_retired_models_and_fixes_routes(db):
    cfg = ai.load_config(db)
    _enable(cfg, "google", "gemini-2.0-flash", "gemini-2.5-flash")   # first one retired
    _enable(cfg, "openai", "gpt-4o-mini")
    cfg.routing["classify"] = [ai.RouteTarget(provider="google", model="gemini-2.0-flash")]

    live = {"google": ["gemini-2.5-flash", "gemini-2.5-pro"]}   # openai unreachable
    assert ai.heal_models(cfg, live) is True
    assert cfg.provider("google").models == ["gemini-2.5-flash"]
    assert cfg.route("classify")[0].model == "gemini-2.5-flash"
    # A provider whose catalogue did not come back keeps its selection untouched.
    assert cfg.provider("openai").models == ["gpt-4o-mini"]
    assert ai.heal_models(cfg, live) is False   # idempotent


def test_an_enabled_provider_with_no_models_gets_a_sensible_default(db):
    cfg = ai.load_config(db)
    cfg.provider("google").enabled = True
    assert ai.heal_models(cfg, {"google": ["gemini-2.5-pro", "gemini-2.5-flash"]}) is True
    assert cfg.provider("google").models == ["gemini-2.5-flash"]


def test_run_meters_and_logs_local_and_errors(db):
    cfg = ai.load_config(db)
    cfg.local.enabled = True
    cfg.routing["normalize"] = [_target(ai.LOCAL)]
    ai.save_config(db, cfg)

    async def local_ok():
        return {"query": "молоко"}, 120, 8, '{"query": "молоко"}'

    asyncio.run(ai._run(db, cfg, "normalize", make_call=lambda p, m: local_ok))
    u = ai.load_usage(db)
    assert u["local_calls"] == 1 and u["local_tokens_in"] == 120 and u["local_tokens_out"] == 8

    async def local_boom():
        raise RuntimeError("ollama refused")

    with pytest.raises(ai.AiConfigError):
        asyncio.run(ai._run(db, cfg, "normalize", make_call=lambda p, m: local_boom))
    assert ai.load_usage(db)["local_errors"] == 1

    log = ai.recent_log(db)
    assert log[0]["ok"] is False and "ollama refused" in log[0]["error"]
    assert log[1]["backend"] == "local" and log[1]["tokens_in"] == 120
    assert len(log) == 2


def test_build_classify_prompt_contains_names_and_taxonomy():
    system, user = ai.build_classify_prompt(["молоко", "хліб"], ["продукти", "продукти/овочі"])
    assert "молоко" in user and "продукти/овочі" in user and "category_path" in user
    assert "JSON" in system.upper() or "json" in system


def test_anthropic_workspace_id_becomes_a_header():
    with_ws = ai._anthropic_client("k", "wrkspc_1")
    assert with_ws.default_headers.get("anthropic-workspace-id") == "wrkspc_1"
    without = ai._anthropic_client("k", None)
    assert "anthropic-workspace-id" not in without.default_headers


def test_routing_gains_tasks_added_after_the_config_was_saved():
    """A stored config predates `intent`; the Settings page must still get an
    entry for it instead of an undefined one."""
    cfg = ai.AiConfig.model_validate({
        "providers": [],
        "routing": {"classify": [{"provider": "local", "model": "qwen"}]},
    })
    assert cfg.routing["intent"] == []
    assert cfg.route("classify")[0].model == "qwen"   # existing choices untouched


def test_speech_key_test_does_not_ping_a_chat_model(tmp_path, monkeypatch):
    """A speech key must be judged by the transcription provider, not by a chat
    model: Groq's chat catalogue moves on, and a retired id there would report a
    perfectly good speech key as broken."""
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import app
    from app.services import stt

    def explode(*args, **kwargs):  # pragma: no cover — must not run
        raise AssertionError("the chat ping must not be used for a speech key")

    seen = {}

    async def fake_check(db_, provider, key=None):
        seen["provider"] = provider
        return {"ok": True, "detail": "ключ робочий"}

    # File-backed: TestClient answers on another thread, which an in-memory
    # SQLite would give its own empty database.
    engine = create_engine(f"sqlite:///{tmp_path / 'ai.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    monkeypatch.setattr(ai, "check_provider_key", explode)
    monkeypatch.setattr(stt, "check_key", fake_check)
    app.dependency_overrides[get_db] = lambda: factory()
    try:
        out = TestClient(app).post("/api/settings/ai/test-key", json={
            "provider": "groq", "purpose": "stt", "api_key": "gsk-test",
        }).json()
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert out["ok"] is True
    assert seen["provider"] == "groq"


def test_every_provider_offers_a_usage_page(db):
    """Our meter only counts this app's calls — each model has to link out to
    where the provider reports the real spend and the free-tier quota."""
    cfg = ai.AiConfig()
    cfg.providers.append(ai.ProviderConfig(
        id="custom_openai", label="Mine", custom=True,
        usage_url="https://my-endpoint.example/usage"))

    chat = {p["id"]: p["usage_url"] for p in ai.providers_meta(cfg)}
    assert chat["groq"] == "https://console.groq.com/"
    assert chat["anthropic"].startswith("https://console.anthropic.com/")
    assert chat["custom_openai"] == "https://my-endpoint.example/usage"
    assert all(chat[name] for name in ai.PROVIDERS)

    speech = {p["id"]: p["usage_url"] for p in ai.stt_providers_meta(cfg)}
    # Google Cloud STT bills separately from the Gemini chat API, so it points
    # at its own metrics page.
    assert "speech.googleapis.com" in speech[ai.GOOGLE_STT]
    assert speech["groq"] == chat["groq"]


def test_custom_provider_usage_link_survives_a_save(db):
    cfg = ai.load_config(db)
    cfg.providers.append(ai.ProviderConfig(
        id="custom_openai", label="Mine", custom=True,
        usage_url=" https://my-endpoint.example/usage "))
    ai.save_config(db, cfg)
    assert ai.load_config(db).provider("custom_openai").usage_url == "https://my-endpoint.example/usage"


def test_list_all_models_covers_disabled_providers_and_marks_live(db, monkeypatch):
    """Every configured service is asked, not only the enabled ones — models are
    ticked while a service is being switched on. A provider that did not answer
    is served the static set and stays out of `live`, so healing skips it."""
    cfg = ai.load_config(db)

    async def fake_fetch(_cfg, provider, _key):
        return [{"id": "live-model", "display_name": "Live"}] if provider == "openai" else []

    monkeypatch.setattr(ai, "_fetch_catalogue", fake_fetch)
    catalogues, live = asyncio.run(ai.list_all_models(db, cfg))
    assert {p.id for p in cfg.providers} <= set(catalogues)   # disabled ones included
    assert live == {"openai"}
    assert [m["id"] for m in catalogues["openai"]] == ["live-model"]
    assert {m["id"] for m in catalogues["anthropic"]} == set(
        ai.PROVIDERS["anthropic"].fallback_models)


def test_list_stt_models_keeps_curated_first_and_adds_only_audio_ids():
    cfg = ai.AiConfig()
    live = {"groq": [{"id": "llama-3.3-70b-versatile", "display_name": ""},
                     {"id": "whisper-large-v3-fake", "display_name": ""}],
            "openai": [{"id": "gpt-4o", "display_name": ""},
                       {"id": "gpt-5-transcribe-fake", "display_name": ""}]}
    out = ai.list_stt_models(cfg, live)
    assert out["groq"][: len(ai.STT_MODEL_CHOICES["groq"])] == ai.STT_MODEL_CHOICES["groq"]
    assert "whisper-large-v3-fake" in out["groq"]       # a transcription id is offered
    assert "llama-3.3-70b-versatile" not in out["groq"]  # a chat id is not
    assert "gpt-5-transcribe-fake" in out["openai"]
    assert "gpt-4o" not in out["openai"]                # chat ids never reach the STT menu
    assert out[ai.GOOGLE_STT] == ai.STT_MODEL_CHOICES[ai.GOOGLE_STT]  # no listing API


def test_per_provider_model_endpoint(tmp_path, monkeypatch):
    """Each service has its own refresh button: the endpoint must answer for one
    provider without touching the others, and must not 404 on the speech-only
    provider (which simply has no catalogue to fetch)."""
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import app

    asked: list[str] = []

    async def fake_fetch(_cfg, provider, _key):
        asked.append(provider)
        return [{"id": "whisper-live", "display_name": "Whisper live"}]

    engine = create_engine(f"sqlite:///{tmp_path / 'models.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(ai, "_fetch_catalogue", fake_fetch)
    app.dependency_overrides[get_db] = lambda: factory()
    try:
        client = TestClient(app)
        out = client.get("/api/settings/ai/models/groq").json()
        speech = client.get(f"/api/settings/ai/models/{ai.GOOGLE_STT}").json()
        missing = client.get("/api/settings/ai/models/nope")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert asked == ["groq"]                      # only the asked-for provider
    assert out["live"] is True
    assert [m["id"] for m in out["models"]] == ["whisper-live"]
    assert "whisper-live" in out["stt"]
    assert speech["live"] is False and speech["models"] == []
    assert speech["stt"] == ai.STT_MODEL_CHOICES[ai.GOOGLE_STT]
    assert missing.status_code == 404
