---
description: Diagnose a failed collection, login, token refresh, or scheduled run.
---
Follow `.agents/playbooks/debug-collection.md`.

Order: `/logs` + `app_events` -> `services/collector.py` -> the adapter ->
`scheduler.py`. Reproduce with fixtures, never live provider calls. Redact all
credentials and personal data.

Symptom: $ARGUMENTS
