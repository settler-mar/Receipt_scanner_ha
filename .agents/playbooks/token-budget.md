# Token Budget

## Read order

1. `PROJECT_STRUCTURE.md` (map) — never `ls -R` the tree.
2. `grep -rn "<symbol>" backend/app frontend/src` to locate, then read only the
   relevant range (`sed -n '120,200p' file`), not the whole file.
3. The one test file matching the touched module.

Open `RESEARCH.md`, `SERVICE.md`, `DOCS.md`, or task docs under `docs/tasks/`
(e.g. `docs/tasks/in-progress/ROADMAP.md`, `docs/tasks/done/CAPTURE_PLAN.md`)
only when the task explicitly needs product history — grep them, don't read them.

## Never read

`backend/data/`, `backend/venv/`, `frontend/node_modules/`, `frontend/dist/`,
`apks/`, `*.zip`, `__pycache__/`, `.pytest_cache/`, log dumps.

## While working

- One feature/bug per patch. No drive-by formatting or renames.
- Don't re-read a file you just edited to verify the edit.
- Don't paste code back into chat that is already in a file you edited.
- Delegate broad "where is X" sweeps to a search agent that returns the answer,
  not the file dumps (Claude: `repo-navigator`).

## Reporting

3–8 lines: what changed (file:line), why, which checks ran, what is left.
