---
name: project-checks
description: Run this project's tests, type-check, build, or local Docker dev stack. Use whenever a change needs validating, pytest or npm is about to be run, or the user asks whether something still works.
---

Read `.agents/playbooks/checks.md` and use the exact commands there.

Non-negotiable: backend via `backend/venv/bin/python -m pytest`, frontend with
the Node 24 PATH exported. Run the narrowest test file that covers the change,
and report only the command plus failing test names.
