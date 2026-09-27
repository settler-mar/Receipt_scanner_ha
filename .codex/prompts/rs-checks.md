---
description: Run the right tests/build for the current change and report briefly.
---
Follow `.agents/playbooks/checks.md`.

Backend: `cd backend && venv/bin/python -m pytest -q tests/<file>.py`.
Frontend: export the Node 24 PATH, then `npm run build`.
Report only commands, pass/fail, and failing test names.

Scope: $ARGUMENTS
