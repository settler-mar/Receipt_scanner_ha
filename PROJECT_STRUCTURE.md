# Project Structure

Use this file as the first navigation map before reading source files. It is
intentionally short to save context.

## Root

- `README.md` - run modes, Docker/dev commands, Node requirements.
- `AGENTS.md` - coding and validation rules for AI agents.
- `CLAUDE.md` - Claude-specific session guidance.
- `.agents/playbooks/` - shared task recipes (adapter, API, DB, frontend,
  scanner, debugging, checks, token budget). Read the one matching the task.
- `.claude/skills/`, `.claude/agents/` - Claude Code skills and subagents.
- `.codex/` - Codex `/rs-*` prompts and ChatGPT project instructions.
- `DOCS.md`, `SERVICE.md`, `RESEARCH.md` - larger reference docs. Open only
  when the task asks for product/service context or adapter research.
- `docs/tasks/{done,in-progress,planned}/` - task/plan documents (ROADMAP.md,
  SHOPPING_LIST_WORKPLAN.md, etc.), sorted by status. See `docs/tasks/README.md`.
- `config.yaml` - app configuration defaults.
- `dev-docker.sh`, `local-docker.sh`, `ollama-docker.sh` - local run helpers.
- `docker-compose*.yml`, `Dockerfile`, `docker-entrypoint.sh` - container setup.
- `apks/` - APK/reverse-engineering materials for receipt provider research.

## Backend

Main code lives in `backend/app`.

- `backend/app/main.py` - FastAPI app setup, routers, MCP mount, frontend
  fallback, startup lifecycle.
- `backend/app/api/` - HTTP API routers. Keep request/response handling here;
  move business logic into services.
- `backend/app/services/` - business logic and integrations used by routers,
  schedulers, Telegram, OCR/AI, shopping list, fiscal queue, product images.
- `backend/app/adapters/` - provider/store-specific receipt import adapters
  such as ATB, Silpo, Metro, Prom, UPG, Vencon.
- `backend/app/scanner/` - receipt scanner pipeline: QR parsing, OCR, templates,
  fiscal checks, deduplication, reconciliation.
- `backend/app/scanner/templates/` - per-chain receipt layout templates.
- `backend/app/integrations/` - lower-level integration helpers, browser/history
  abstractions, order delivery utilities.
- `backend/app/models/` - SQLAlchemy models.
- `backend/app/schemas/` - Pydantic schemas shared by APIs and services.
- `backend/app/core/` - settings, credentials, security, HTTP helpers, time,
  OAuth/PKCE helpers.
- `backend/app/db.py` - engine/session setup and schema upgrade helpers.
- `backend/app/mcp_server.py` - local MCP server surface.
- `backend/app/scheduler.py` - background scheduler startup.
- `backend/app/seed.py` - seed data for stores/categories.

Backend support folders:

- `backend/tests/` - pytest coverage. Pick the test matching the touched module.
- `backend/scripts/` - one-off maintenance/import/debug scripts.
- `backend/alembic/` and `backend/alembic.ini` - migration infrastructure.
- `backend/data/` - local SQLite DB, backups, persistent runtime data. Do not
  delete or rewrite it without explicit user request.
- `backend/requirements.txt` - runtime dependencies.
- `backend/requirements-dev.txt` - dev/test dependency entry point.

## Frontend

Main code lives in `frontend/src`.

- `frontend/src/App.vue` - app shell.
- `frontend/src/main.ts` - Vue app bootstrap.
- `frontend/src/router/` - route definitions.
- `frontend/src/views/` - page-level screens.
- `frontend/src/components/` - reusable UI components.
- `frontend/src/widgets/` - dashboard/widget components and registry.
- `frontend/src/composables/` - reusable Vue composition functions.
- `frontend/src/lib/` - API clients, formatting, date/time helpers, store icons.
- `frontend/src/types/` - TypeScript API/domain types.
- `frontend/src/stores/` - Pinia stores, when present.
- `frontend/src/assets/` - local images, CSS, logos.

Frontend support files:

- `frontend/package.json` - scripts, Node engine, dependencies.
- `frontend/package-lock.json` - npm lockfile.
- `frontend/vite.config.ts` - Vite config.
- `frontend/tsconfig*.json` - TypeScript configs.
- `frontend/public/` - static public assets.
- `frontend/dist/` - generated build output; usually do not edit manually.

## Where To Make Common Changes

- New receipt provider: start in `backend/app/adapters/`, add/adjust service
  logic if needed, then add tests in `backend/tests/test_<provider>.py`.
- Receipt OCR/scanning behavior: `backend/app/scanner/` first, then services or
  APIs only if the public workflow changes.
- API endpoint: router in `backend/app/api/`, schema in `backend/app/schemas/`,
  business logic in `backend/app/services/`.
- Database field/table: `backend/app/models/`, schema/migration support in
  `backend/app/db.py` or Alembic, and focused tests.
- Settings or credentials: `backend/app/core/config.py`,
  `backend/app/core/credentials.py`, matching settings API/service files.
- Telegram behavior: `backend/app/services/telegram.py`.
- MCP behavior: `backend/app/mcp_server.py` and related services.
- Dashboard/widget UI: `frontend/src/widgets/`,
  `frontend/src/composables/useWidgetData.ts`, relevant API in
  `backend/app/api/widgets.py`.
- Frontend page behavior: matching file in `frontend/src/views/`, shared calls
  in `frontend/src/lib/`, shared types in `frontend/src/types/`.

## Fast Validation Map

- Backend targeted test: `cd backend && pytest tests/<relevant_test>.py`
- Frontend type/build check: `cd frontend && npm run build`
- Integrated dev backend: `PORT=18002 ./dev-docker.sh up`
- Integrated dev frontend:
  `cd frontend && VITE_API_BASE=http://localhost:18002 npm run dev -- --host 0.0.0.0`

## Token-Saving Rules

- Open this file first, then only the folders listed for the task.
- Avoid reading `backend/data/`, `frontend/dist/`, `frontend/node_modules/`,
  `backend/venv/`, caches, zips, APKs, and large research docs unless required.
- Use file names and short summaries in reports instead of pasting long code.
