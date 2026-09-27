"""What the AI journal caught real providers doing, and how a call survives it.

Every case here comes from `ai_log` in the running instance: a model that
rejects `max_tokens` outright (400 on every single call), a model under load
(503), and a quota that is either a per-minute limit worth waiting out or a plan
limit that no waiting fixes.
"""
import asyncio

import httpx
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


@pytest.fixture(autouse=True)
def forget_learned_quirks():
    """The adaptations are remembered per process; tests must not inherit them."""
    ai._RENAMED_TOKEN_FIELD.clear()
    ai._NO_TEMPERATURE.clear()
    yield
    ai._RENAMED_TOKEN_FIELD.clear()
    ai._NO_TEMPERATURE.clear()


def _serve(*responses):
    """A mock endpoint answering with each response in turn; records requests."""
    import json as jsonlib

    sent: list[dict] = []
    replies = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(jsonlib.loads(request.content))
        status, body = replies[min(len(sent), len(replies)) - 1]
        return httpx.Response(status, json=body)

    return handler, sent


_OK = (200, {"choices": [{"message": {"content": '{"ok": true}'}}],
             "usage": {"prompt_tokens": 7, "completion_tokens": 3}})


def _call(monkeypatch, handler):
    monkeypatch.setattr(ai, "_http",
                        lambda timeout=120: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return asyncio.run(ai._openai_call("https://api.example.com/v1", "sk-x",
                                       model="gpt-5.6-luna",
                                       messages=[{"role": "user", "content": "hi"}],
                                       max_tokens=4096))


def _no_sleeping(monkeypatch):
    waits: list[float] = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(ai.asyncio, "sleep", fake_sleep)
    return waits


def test_a_model_that_renamed_max_tokens_is_adapted_to(monkeypatch):
    """Seen on gpt-5.6-luna: 'Unsupported parameter: max_tokens … Use
    max_completion_tokens instead' — every call to it failed, for both OCR and
    classification, until the payload matched what it asks for."""
    handler, sent = _serve(
        (400, {"error": {"message": "Unsupported parameter: 'max_tokens' is not "
                                    "supported with this model. Use "
                                    "'max_completion_tokens' instead."}}),
        _OK,
    )
    text, tin, tout, _cached = _call(monkeypatch, handler)

    assert (text, tin, tout) == ('{"ok": true}', 7, 3)
    assert "max_tokens" in sent[0] and "max_completion_tokens" not in sent[0]
    assert sent[1]["max_completion_tokens"] == 4096 and "max_tokens" not in sent[1]


def test_openai_uses_stable_prompt_cache_key_and_reports_cache_hits(monkeypatch):
    handler, sent = _serve(
        (200, {"choices": [{"message": {"content": '{"ok": true}'}}],
               "usage": {"prompt_tokens": 1400, "completion_tokens": 3,
                         "prompt_tokens_details": {"cached_tokens": 1280}}}),
    )
    monkeypatch.setattr(ai, "_http",
                        lambda timeout=120: httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    result = asyncio.run(ai._openai_call(
        "https://api.openai.com/v1", "sk-x", model="gpt-4o-mini",
        messages=[{"role": "system", "content": "stable rules"},
                  {"role": "user", "content": "dynamic receipt"}], max_tokens=100,
    ))

    assert result == ('{"ok": true}', 1400, 3, 1280)
    assert sent[0]["prompt_cache_key"].startswith("receipt-scanner-")


def test_openai_compatible_provider_does_not_receive_openai_cache_key(monkeypatch):
    handler, sent = _serve(_OK)
    _call(monkeypatch, handler)
    assert "prompt_cache_key" not in sent[0]


def test_the_rename_is_remembered_so_it_costs_one_wasted_call_at_most(monkeypatch):
    handler, sent = _serve(
        (400, {"error": {"message": "Unsupported parameter: 'max_tokens' is not supported "
                                    "with this model. Use 'max_completion_tokens' instead."}}),
        _OK,
    )
    _call(monkeypatch, handler)
    _call(monkeypatch, handler)

    assert len(sent) == 3, "the second call must not repeat the rejected payload"
    assert "max_completion_tokens" in sent[2]


def test_a_model_that_refuses_an_explicit_temperature_gets_none(monkeypatch):
    handler, sent = _serve(
        (400, {"error": {"message": "Unsupported value: 'temperature' does not support "
                                    "0 with this model."}}),
        _OK,
    )
    _call(monkeypatch, handler)

    assert sent[0]["temperature"] == 0
    assert "temperature" not in sent[1]


def test_a_model_under_load_is_retried_once(monkeypatch):
    """503 'experiencing high demand' clears in seconds, and the alternative is
    escalating to a costlier model for no reason."""
    waits = _no_sleeping(monkeypatch)
    handler, sent = _serve(
        (503, [{"error": {"code": 503, "message": "This model is currently experiencing "
                                                  "high demand.", "status": "UNAVAILABLE"}}]),
        _OK,
    )
    text, _tin, _tout, _cached = _call(monkeypatch, handler)

    assert text == '{"ok": true}'
    assert len(sent) == 2 and waits == [2.0]


def test_a_rate_limit_that_names_a_delay_is_waited_out(monkeypatch):
    waits = _no_sleeping(monkeypatch)
    handler, sent = _serve(
        (429, {"error": {"code": 429, "message": "Quota exceeded",
                         "details": [{"retryDelay": "7s"}]}}),
        _OK,
    )
    _call(monkeypatch, handler)

    assert len(sent) == 2 and waits == [7.0]


def test_an_exhausted_plan_quota_fails_straight_through_to_the_next_model(monkeypatch):
    """The 429 that filled the journal: a plan quota, with no retryDelay. Waiting
    cannot fix it — the route's next step can, so fail fast and let it."""
    waits = _no_sleeping(monkeypatch)
    handler, sent = _serve(
        (429, [{"error": {"code": 429, "message": "You exceeded your current quota, please "
                                                  "check your plan and billing details."}}]),
    )
    with pytest.raises(RuntimeError) as exc:
        _call(monkeypatch, handler)

    assert len(sent) == 1 and waits == []
    # And the message the route reports is the provider's own, not "429".
    assert "exceeded your current quota" in str(exc.value)


def test_a_long_retry_delay_is_not_waited_out(monkeypatch):
    waits = _no_sleeping(monkeypatch)
    handler, sent = _serve(
        (429, {"error": {"message": "Quota exceeded", "details": [{"retryDelay": "58s"}]}}),
    )
    with pytest.raises(RuntimeError):
        _call(monkeypatch, handler)
    assert len(sent) == 1 and waits == []


def test_a_plain_error_still_surfaces_the_providers_own_message(monkeypatch):
    handler, _sent = _serve(
        (404, {"error": {"message": "The model 'gpt-5.6-luna' does not exist"}}),
    )
    with pytest.raises(RuntimeError) as exc:
        _call(monkeypatch, handler)
    assert "does not exist" in str(exc.value) and "404" in str(exc.value)
