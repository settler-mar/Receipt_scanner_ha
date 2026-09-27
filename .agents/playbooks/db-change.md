# Model / Schema / Migration Change

The production DB is a live SQLite file in `backend/data/`. Never delete,
rewrite, or "recreate" it, and never drop a backup.

## Steps

1. Model in `backend/app/models/` (`models.py`, `scanner.py`, `shopping.py`).
2. Pydantic schema in `backend/app/schemas/` if the field crosses the API.
3. Migration:
   - additive column with a safe default -> guarded upgrade in
     `backend/app/db.py` is acceptable when that is the local pattern;
   - anything structural or data-rewriting -> Alembic revision in
     `backend/alembic/versions/` (copy the style of the newest file there).
4. Backfill existing rows explicitly in the migration when the new field must be
   correct for old data (precedent: `a4b7c2e91d35` recalculating bonuses).
5. Test the behavior, not the DDL: `backend/tests/test_<area>.py`.

## Rules

- Timestamps stored UTC-naive; convert to Europe/Kyiv only for display.
- User-tunable config belongs in the `app_settings` KV table, not a new column.
- A migration that wipes user data (like the taxonomy rebuild) needs an explicit
  go-ahead from the user first — say what will be lost before writing it.

Check: `cd backend && venv/bin/python -m pytest -q tests/test_<area>.py`
