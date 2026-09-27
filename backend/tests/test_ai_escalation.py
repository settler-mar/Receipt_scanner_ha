"""When a second AI model is worth paying for, and which one is next.

Two rules, both from ROADMAP §2.2/§2.4: the route moves on only when a step came
back objectively broken — never on a model's own opinion of itself — and the
step it moves on to is found by the service+model pair already spent, so the
order the user set in AI → Маршрути задач is the order that is followed.
"""
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


def _route(db, *targets):
    """Two enabled cloud providers with three models between them, in order."""
    cfg = ai.load_config(db)
    for provider, models_ in (("google", ["gemini-a"]), ("openai", ["gpt-b", "gpt-c"])):
        conf = cfg.provider(provider)
        conf.enabled = True
        conf.models = models_
        ai.set_cloud_key(db, provider, f"key-{provider}")
    cfg.routing["ocr"] = [ai.RouteTarget(provider=p, model=m) for p, m in targets]
    ai.save_config(db, cfg)
    return ai.load_config(db)


_FULL = {"total_amount": 120.0, "items": [{"name": "молоко", "total_price": 120.0}]}


def _replies(*answers):
    """A make_call that hands each target its scripted answer in turn."""
    seen = []

    def make_call(provider, model):
        async def call():
            seen.append(f"{provider}/{model}")
            reply = answers[len(seen) - 1]
            if isinstance(reply, Exception):
                raise reply
            return reply, 10, 5, str(reply)
        return call

    return make_call, seen


# --------------------------------------------------------------------------- #
# What counts as an objective failure
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("reply, broken", [
    (_FULL, False),
    # A model that read a collapsed screenshot honestly says so. That is the
    # pipeline's business (it files a draft); buying a second model changes
    # nothing about what the photo shows.
    ({**_FULL, "complete": False}, False),
    ({**_FULL, "items": [{"name": "молоко", "total_price": 118.0}]}, False),  # rounding
    ("not json at all", True),
    ({"items": [], "total_amount": 0}, True),
    ({"total_amount": 120.0, "items": []}, True),
    ({"total_amount": 0, "items": [{"name": "молоко", "total_price": 120.0}]}, True),
    ({"total_amount": 120.0,
      "items": [{"name": "молоко", "total_price": 12.0}]}, True),  # lines vs total
])
def test_only_a_broken_answer_is_worth_the_next_model(reply, broken):
    assert (ai._ocr_objection(reply) is not None) is broken


def test_a_usable_first_answer_stops_the_route(db):
    cfg = _route(db, ("google", "gemini-a"), ("openai", "gpt-b"))
    make_call, seen = _replies(_FULL, _FULL)

    answer = asyncio.run(ai._run(db, cfg, "ocr", make_call=make_call,
                                 accept=ai._ocr_objection))

    assert answer.value == _FULL
    assert (answer.provider, answer.model) == ("google", "gemini-a")
    assert seen == ["google/gemini-a"], "the second model must not be paid for"


def test_a_broken_first_answer_escalates_to_the_next_step(db):
    cfg = _route(db, ("google", "gemini-a"), ("openai", "gpt-b"))
    make_call, seen = _replies({"items": [], "total_amount": 0}, _FULL)

    answer = asyncio.run(ai._run(db, cfg, "ocr", make_call=make_call,
                                 accept=ai._ocr_objection))

    assert answer.value == _FULL
    assert (answer.provider, answer.model) == ("openai", "gpt-b")
    assert seen == ["google/gemini-a", "openai/gpt-b"]
    # The journal records the step that failed, with the reason, not just the win.
    failures = [row for row in ai.recent_log(db) if not row["ok"]]
    assert failures and "товар" in (failures[0]["error"] or "")


def test_every_step_broken_reports_what_each_one_did(db):
    cfg = _route(db, ("google", "gemini-a"), ("openai", "gpt-b"))
    make_call, _seen = _replies("nonsense", {"items": []})

    with pytest.raises(ai.AiConfigError) as exc:
        asyncio.run(ai._run(db, cfg, "ocr", make_call=make_call, accept=ai._ocr_objection))
    assert "google/gemini-a" in str(exc.value) and "openai/gpt-b" in str(exc.value)


# --------------------------------------------------------------------------- #
# Manual escalation
# --------------------------------------------------------------------------- #
def test_escalation_resumes_after_the_pair_already_used(db):
    cfg = _route(db, ("google", "gemini-a"), ("openai", "gpt-b"), ("openai", "gpt-c"))
    make_call, seen = _replies(_FULL)

    answer = asyncio.run(ai._run(db, cfg, "ocr", make_call=make_call,
                                 accept=ai._ocr_objection,
                                 start_after=("google", "gemini-a")))

    assert (answer.provider, answer.model) == ("openai", "gpt-b")
    assert seen == ["openai/gpt-b"], "the step already spent must not run again"


def test_the_next_step_follows_the_route_as_it_stands_now(db):
    """"Stronger" is whatever the user put next, so a reordered route reorders
    the escalation — which is why the next step is found by pair, not by index."""
    _route(db, ("google", "gemini-a"), ("openai", "gpt-b"), ("openai", "gpt-c"))

    assert ai.next_ocr_step(db, None)[:2] == ("google", "gemini-a")
    assert ai.next_ocr_step(db, ("google", "gemini-a"))[:2] == ("openai", "gpt-b")
    assert ai.next_ocr_step(db, ("openai", "gpt-b"))[:2] == ("openai", "gpt-c")
    # Nothing after the last step: the action is hidden rather than offered.
    assert ai.next_ocr_step(db, ("openai", "gpt-c")) is None

    _route(db, ("openai", "gpt-c"), ("google", "gemini-a"))
    assert ai.next_ocr_step(db, ("openai", "gpt-c"))[:2] == ("google", "gemini-a")
    assert ai.next_ocr_step(db, ("google", "gemini-a")) is None


def test_a_step_that_is_no_longer_in_the_route_starts_from_the_top(db):
    """The user deleted the model that read this photo. There is still a route,
    and the user still asked for a stronger model — so offer its first step."""
    _route(db, ("openai", "gpt-b"), ("openai", "gpt-c"))

    assert ai.next_ocr_step(db, ("google", "gemini-a"))[:2] == ("openai", "gpt-b")


def test_a_step_whose_provider_is_switched_off_is_skipped(db):
    cfg = _route(db, ("google", "gemini-a"), ("openai", "gpt-b"))
    cfg.provider("openai").enabled = False
    ai.save_config(db, cfg)

    assert ai.next_ocr_step(db, ("google", "gemini-a")) is None
