---
name: store-adapter
description: Add, fix, or debug a store/receipt-provider adapter in backend/app/adapters (ATB, Silpo, Fozzy, Fora, Metro, WOG, UPG, Ukrnafta, Rozetka, Prom, EVA, AliExpress, Brain, Foxtrot, KLO, Epicentr, Dnipro-M, BRSM, Bars, Vencon, scanner). Use when the task mentions a store code, a new receipt source, loyalty/bonus parsing, or an adapter returning wrong/empty receipts.
---

Read `.agents/playbooks/adapter.md` and follow it. Then read only:
`backend/app/adapters/base.py`, the single closest existing adapter, and
`backend/tests/test_<provider>.py`.

Do not read other adapters, `RESEARCH.md`, or `docs/tasks/in-progress/ROADMAP.md`
unless the playbook's steps leave a real question open (then grep, don't read).

Finish with `cd backend && venv/bin/python -m pytest -q tests/test_<store>.py`
and report in ≤6 lines.
