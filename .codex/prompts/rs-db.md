---
description: Model / schema / Alembic migration change.
---
Follow `.agents/playbooks/db-change.md`.

The SQLite DB in `backend/data` is live user data: never delete or recreate it,
and ask before any migration that destroys rows. Timestamps are UTC-naive.

Task: $ARGUMENTS
