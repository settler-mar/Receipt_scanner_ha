---
name: scanner-pipeline
description: Receipt scanning work — QR decode ladder, OCR, chain templates, ДПС fiscal checks, deduplication, document inbox, AI/vision escalation. Use for anything under backend/app/scanner or the inbox/fiscal-queue/scan-jobs services.
---

Read `.agents/playbooks/scanner.md` and follow it.

Key invariant: escalate a rung (FAST -> NORMAL -> DEEP -> AI) only on an
objective failure. Dedup changes must keep `tests/test_duplicates.py` and
`tests/test_manual_dedup.py` green.
