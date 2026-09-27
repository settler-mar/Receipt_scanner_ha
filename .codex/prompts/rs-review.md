---
description: Review the working-tree diff against this project's rules.
---
Review `git diff` (and `git diff --staged`) against `AGENTS.md`:

1. Layering — no logic in routers, no DB access in adapters.
2. External calls — explicit timeout/auth/parse error handling, failures visible.
3. Secrets — no tokens, cookies, passwords, phone numbers or receipt data in
   logs, exceptions, or fixtures.
4. Data safety — nothing destructive to `backend/data` or existing rows.
5. Scope — no drive-by reformatting, renames, or new dependencies.
6. Tests — coverage for the changed behavior in the matching test file.

Output: `path:line — problem — fix`, most severe first, then a one-line verdict.

Focus: $ARGUMENTS
