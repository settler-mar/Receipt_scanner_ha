# Debugging a Failed Collection / Login / Schedule Run

## Order of investigation (cheapest first)

1. The unified log: `/logs` page, backed by `backend/app/api/logs.py` and the
   `app_events` table — find the actual recorded error before reading code.
2. `backend/app/services/collector.py` — how the failure was classified and
   surfaced (health, nav badges count only OPEN problems).
3. The adapter for that store code (`backend/app/adapters/<code>.py`).
4. Scheduler only if the run never started: `backend/app/scheduler.py`.

## Known shapes

- `401` on a scheduled run while a manual run after login works = expired token
  and no refresh support for that provider (precedent: Ukrnafta). Fix belongs in
  the adapter's refresh path, and the UI must ask for re-login instead of
  retrying silently.
- Empty result with HTTP 200 = provider changed its payload shape -> update the
  parser and add a fixture-based regression test.
- Browser-backed adapters rotate cookies mid-request via
  `ROTATED_ACCESS_TOKEN_KEY`; it must be encrypted by service code, never
  persisted as plain JSON.

## Rules

- Reproduce with a recorded fixture where possible; no live provider calls in
  tests.
- Never print tokens, cookies, passwords, phone numbers or receipt contents into
  logs or chat.
- A failure must end up visible in `/logs` and in the account's status — silent
  swallowing is the bug, not just the exception.
