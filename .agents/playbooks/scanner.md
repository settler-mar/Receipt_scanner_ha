# Scanner Pipeline (QR / OCR / fiscal / dedup)

Code: `backend/app/scanner/` — `pipeline.py` orchestrates, `qr/` decodes,
`networks.py` maps a fiscal doc to a retail network, `templates/` holds per-chain
layouts. Entry points: document inbox (`app/api/inbox.py`,
`services/capture.py`), fiscal queue (`services/fiscal_queue.py`),
scan jobs (`services/scan_jobs.py`).

## Rules

- QR ladder is FAST -> NORMAL -> DEEP. Escalate only on an **objective** failure
  (no decode, checksum mismatch), never on a hunch — escalation costs money and
  latency. Same for AI/vision escalation.
- Keep decoding deterministic where possible; AI is the last rung, not the first.
- New chain layout -> a template in `scanner/templates/`, not an `if` in the
  pipeline.
- Dedup/reconciliation changes: verify against `tests/test_duplicates.py` and
  `tests/test_manual_dedup.py` — a false merge destroys user data.
- Fiscal (ДПС) calls need explicit timeout/parse error handling; the queue must
  record the failure, not swallow it.

## Tests

`tests/test_scanner.py`, `test_qr_ladder.py`, `test_qr_stamped.py`,
`test_fiscal_check.py`, `test_inbox.py`, `test_duplicates.py`,
`test_ai_escalation.py`.

Real-photo accuracy cannot be judged from unit tests — if the task is tuning,
ask the user for a sample image rather than guessing.
