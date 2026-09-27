# Checks (exact commands)

Run the smallest check that covers the change. Never run the full suite "just
to be safe" — it is slow and floods context.

## Backend

```bash
cd backend && venv/bin/python -m pytest -q tests/test_<module>.py
```

- Always `venv/bin/python -m pytest`. `source venv/bin/activate` from the repo
  root silently does nothing and the fallback `python` is an unrelated
  ESP-IDF env without project deps — never `pip install` into it.
- Import smoke check: `cd backend && venv/bin/python -c "import app.main"`.
- Whole suite only on request: `cd backend && venv/bin/python -m pytest -q`.

## Frontend

```bash
export PATH=/home/max/.nvm/versions/node/v24.19.0/bin:$PATH
cd frontend && npm run build
```

- Default `node` is v12; the repo needs 22.18+/24.12+.
- `npm run build` = vite build + `vue-tsc`. The type-check has ~8 PRE-EXISTING
  errors (bwip-js types, strict-undefined in PasswordInput / AccountsView /
  ProductsView / StoreCategoryWidget). Do not "fix" them as part of another task.
- Check only your own files:
  `npx vue-tsc --noEmit -p tsconfig.app.json | grep <file>`.

## Integrated run

```bash
PORT=18002 ./dev-docker.sh up
cd frontend && VITE_API_BASE=http://localhost:18002 npm run dev -- --host 0.0.0.0
```

Docker scripts are the source of truth; do not invent alternate startup commands.

## Reporting

Report: command run, pass/fail, and failing test names only. Paste a traceback
only for the failure you are actually fixing. If a check was skipped, say why.
