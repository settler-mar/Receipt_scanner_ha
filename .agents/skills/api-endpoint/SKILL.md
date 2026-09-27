---
name: api-endpoint
description: Add or change a FastAPI endpoint — router in backend/app/api, Pydantic schema, service logic, frontend client call and types. Use when the task asks for a new /api route, changes a response shape, or wires a UI screen to backend data.
---

Read `.agents/playbooks/api-endpoint.md` and follow it.

Touch at most: one router in `backend/app/api/`, one schema in
`backend/app/schemas/`, one service in `backend/app/services/`, plus
`frontend/src/lib/api.ts` and `frontend/src/types/` when the UI consumes it.

Check with `cd backend && venv/bin/python -m pytest -q tests/test_<area>.py`.
