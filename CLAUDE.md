# Claude Instructions

Follow `AGENTS.md` first. This file adds Claude-specific guidance for keeping
sessions short and code changes easy to review.

## Skills, Agents, Playbooks

- Task recipes: `.agents/playbooks/` — the shared source of truth.
- Skills (`.claude/skills/`) are thin wrappers over them; invoke the matching one
  instead of improvising: `store-adapter`, `api-endpoint`, `db-change`,
  `frontend-ui`, `scanner-pipeline`, `project-checks`, `collection-debug`.
- Subagents (`.claude/agents/`) keep bulk output out of the main context:
  - `repo-navigator` — "where is X" sweeps; returns file:line, not file dumps.
  - `checks-runner` — runs pytest / vue-tsc / build, returns pass-fail + failures.
  - `adapter-scout` — maps a provider payload onto `NormalizedReceipt`.
  - `change-reviewer` — reviews the diff against `AGENTS.md` before handoff.
- Edit the playbook when a convention changes; never fork the text into skills.

## Context Budget

- Start by reading `README.md`, the specific files involved in the request, and
  relevant tests. Do not preload large planning documents unless the task points
  to them.
- Prefer concise investigation notes. Avoid copying long code blocks into the
  chat when file paths and line references are enough.
- Before editing, identify the smallest backend/frontend boundary that solves
  the request.

## Coding Rules

- Match existing project style. Do not introduce a new architecture for a local
  fix.
- Keep commits/patches focused by feature or bug. Do not mix formatting cleanup
  with behavior changes.
- Use explicit error handling for external receipt providers, fiscal services,
  browser automation, Telegram, OAuth, and AI/OCR flows.
- Never expose secrets from `.env`, settings, cookies, browser profiles, logs,
  or receipt payloads.

## Checks

- Backend: run targeted `pytest` from `backend` for touched behavior.
- Frontend: run `npm run build` from `frontend` for Vue/TypeScript changes.
- Docker scripts are the source of truth for integrated local runs; avoid
  inventing alternate startup commands unless debugging requires it.

## Output Style

- Final replies should be short: what changed, where, and which checks ran.
- If a check was skipped, say why.
