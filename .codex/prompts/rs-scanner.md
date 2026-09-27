---
description: QR / OCR / fiscal / dedup pipeline work.
---
Follow `.agents/playbooks/scanner.md`.

Escalate the QR/AI ladder only on objective failure. New chain layouts go into
`backend/app/scanner/templates/`, not into pipeline branches. Keep
`test_duplicates.py` and `test_manual_dedup.py` green.

Task: $ARGUMENTS
