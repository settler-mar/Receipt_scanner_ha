# API Endpoint

Layering: router validates + calls service + returns schema. Business logic and
provider calls live in services/adapters, never in `backend/app/api/`.

## Steps

1. Router in `backend/app/api/<area>.py` (reuse the existing file for the area).
   New file -> add `app.include_router(<area>.router)` in `backend/app/main.py`
   (~line 92, keep the existing grouping order).
2. Request/response models in `backend/app/schemas/` (Pydantic v2 style already
   used in the file you are editing).
3. Logic in `backend/app/services/<area>.py`.
4. Frontend: HTTP call in `frontend/src/lib/api.ts` (or `shoppingApi.ts`),
   types in `frontend/src/types/`.
5. Test in `backend/tests/test_<area>.py` using the existing FastAPI TestClient
   fixtures — copy the setup from a neighbouring test, don't invent one.

## Conventions

- Paths stay under `/api/...`; match the plural/kebab style of siblings.
- Return schemas, not ORM objects.
- Errors: `HTTPException` with a message the UI can show; no internals, no secrets.
- User-visible strings stay in the existing Ukrainian/Russian wording.
- User-configurable settings go through the `app_settings` KV store
  (`backend/app/api/settings.py`), not new columns.

Check: `cd backend && venv/bin/python -m pytest -q tests/test_<area>.py`
