"""In-memory store for in-flight OAuth-browser login attempts (silpo.py and any
future oauth_browser store). Keyed by the `state` param round-tripped through the
provider's redirect, holding the account id + PKCE verifier needed to finish the
exchange in the callback endpoint. Deliberately not persisted to the DB — a login
attempt is only ever a few minutes long and this is a single-process service."""
from __future__ import annotations

import threading
import time

_TTL_SECONDS = 600
_lock = threading.Lock()
_states: dict[str, dict] = {}


def put(state: str, data: dict) -> None:
    with _lock:
        _states[state] = {**data, "_ts": time.monotonic()}


def pop(state: str) -> dict | None:
    with _lock:
        _cleanup()
        entry = _states.pop(state, None)
    if entry is None:
        return None
    entry.pop("_ts", None)
    return entry


def _cleanup() -> None:
    now = time.monotonic()
    expired = [k for k, v in _states.items() if now - v["_ts"] > _TTL_SECONDS]
    for k in expired:
        _states.pop(k, None)
