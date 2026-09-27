---
name: collection-debug
description: Diagnose a failing receipt collection, provider login, token refresh, scheduled run, or an account stuck in error. Use when the symptom is "no new receipts", 401/403 from a store, or an account health/error badge.
---

Read `.agents/playbooks/debug-collection.md` and follow the investigation order:
`/logs` + `app_events` first, then collector, then the adapter, then scheduler.

Never print tokens, cookies, or receipt personal data. Reproduce with fixtures,
not live provider calls.
