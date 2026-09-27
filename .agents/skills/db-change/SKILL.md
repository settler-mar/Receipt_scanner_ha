---
name: db-change
description: Change SQLAlchemy models, DB schema, or write an Alembic migration for the live SQLite database. Use when the task adds a column/table, backfills data, or touches backend/app/db.py or backend/alembic/versions.
---

Read `.agents/playbooks/db-change.md` and follow it.

Hard rules: never delete or recreate `backend/data/*.db` or its backups; confirm
with the user before any migration that destroys existing rows; timestamps are
UTC-naive in the DB.
