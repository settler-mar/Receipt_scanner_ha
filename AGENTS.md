# AI Agent Instructions

This repository is a receipt scanner app: FastAPI backend in `backend/app`,
Vue 3/Vite frontend in `frontend/src`, SQLite data under `backend/data`, and
Docker scripts for local/dev/production runs.

## Playbooks

Task recipes live in `.agents/playbooks/` (shared by every assistant). Open the
one matching the task instead of rediscovering the conventions:

`adapter.md`, `api-endpoint.md`, `db-change.md`, `frontend.md`, `scanner.md`,
`debug-collection.md`, `checks.md`, `token-budget.md`. Index: `.agents/README.md`.

Claude Code loads them as skills (`.claude/skills/`) and has subagents in
`.claude/agents/`. Codex loads them as `/rs-*` prompts (`.codex/README.md`).

## Work Efficiently

- For navigation, read `PROJECT_STRUCTURE.md` before scanning the tree. It maps
  the main folders and where common changes belong.
- Read only the files needed for the task first. Prefer targeted search over
  loading large docs such as `RESEARCH.md`, `SERVICE.md`, or task docs under
  `docs/tasks/`.
- Keep changes narrow. Do not rewrite unrelated modules, rename broad APIs, or
  reformat files that are not part of the request.
- Preserve existing behavior and Ukrainian/Russian UI text unless the task asks
  for wording changes.
- Reuse local patterns: adapters live in `backend/app/adapters`, API routers in
  `backend/app/api`, services in `backend/app/services`, Vue views in
  `frontend/src/views`, shared frontend API helpers in `frontend/src/lib`.
- Avoid new dependencies unless they clearly remove more code or risk than they
  add. If a dependency is needed, update the matching lock/requirements file.

## Backend

- Use Python 3.10+ style and FastAPI/Pydantic v2 conventions already present in
  the project.
- Keep database changes compatible with existing SQLite data in `backend/data`.
  Add migrations or guarded schema upgrades when needed; do not delete local
  databases or backups.
- Receipt source logic should stay in adapters/services rather than API routers.
  Routers should mostly validate requests, call services, and return schemas.
- Receipt totals are composed at receipt level: product lines are only products.
  Store adapters must expose delivery, tax, fees, coupons, coins, seller
  adjustments, and other order-level amounts as signed receipt components
  (`NormalizedReceipt.components`). Positive values add to the payable total;
  negative values reduce it. Never hide an order-level amount in a product line.
  If the provider leaves a residual that cannot be classified, keep it in
  `total_adjustment` with a reason so it remains auditable and analyzable.
- Network integrations must handle timeouts, auth failures, and parse failures
  explicitly. Do not log tokens, cookies, passwords, or receipt personal data.

## Frontend

- Keep the current Vue 3 composition style and existing component structure.
- Put API types in `frontend/src/types` and HTTP calls in `frontend/src/lib`
  when shared across views.
- UI changes should be dense and practical; this is an operational app, not a
  marketing site.
- Respect the Node engine in `frontend/package.json`: Node `22.18+` or
  `24.12+`.

## Validation

Run the smallest useful check for the change — exact forms matter:

```bash
cd backend && venv/bin/python -m pytest -q tests/<relevant_test>.py
```

```bash
export PATH=/home/max/.nvm/versions/node/v24.19.0/bin:$PATH
cd frontend && npm run build
```

`source venv/bin/activate` from the repo root silently does nothing and the
fallback `python` is an unrelated ESP-IDF env. The frontend type-check has ~8
pre-existing errors (bwip-js, PasswordInput, AccountsView, ProductsView,
StoreCategoryWidget) — do not fix them as part of another task. Details:
`.agents/playbooks/checks.md`.

For full local development, use:

```bash
PORT=18002 ./dev-docker.sh up
cd frontend && VITE_API_BASE=http://localhost:18002 npm run dev -- --host 0.0.0.0
```

## Token Discipline

- Summarize findings instead of pasting long file contents.
- When reporting, mention changed files and validation only.
- If blocked by missing credentials, external services, or unavailable local
  binaries, state the blocker and continue with offline/unit-testable work.
