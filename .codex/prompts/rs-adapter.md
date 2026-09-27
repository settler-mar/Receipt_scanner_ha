---
description: Add or fix a store/receipt-provider adapter.
---
Follow `.agents/playbooks/adapter.md` in this repo.

Read only: `backend/app/adapters/base.py`, the single closest existing adapter,
and `backend/tests/test_<provider>.py`. Register the adapter in
`registry.py` and `seed.py`, add a fixture-based test, then run
`cd backend && venv/bin/python -m pytest -q tests/test_<store>.py`.

Task: $ARGUMENTS
