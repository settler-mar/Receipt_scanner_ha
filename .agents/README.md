# Shared AI Playbooks

Single source of truth for task recipes, used by every assistant:

- Claude Code -> `.claude/skills/*/SKILL.md` (thin wrappers that read these files)
- Codex / ChatGPT -> `.codex/prompts/*.md` and `AGENTS.md`
- Cursor -> `.cursor/rules/*.mdc` (concise rules that point back to `AGENTS.md`
  and these playbooks)

Rules shared by all: `AGENTS.md`. Navigation map: `PROJECT_STRUCTURE.md`.

| Playbook | Use when |
| --- | --- |
| `playbooks/checks.md` | running tests/build/dev stack (exact commands + env quirks) |
| `playbooks/token-budget.md` | deciding what to read and how to report |
| `playbooks/adapter.md` | new or broken store/receipt provider adapter |
| `playbooks/api-endpoint.md` | adding/changing a FastAPI endpoint |
| `playbooks/db-change.md` | model/schema/migration change |
| `playbooks/frontend.md` | Vue view, component, widget, dashboard change |
| `playbooks/scanner.md` | QR/OCR/fiscal/dedup pipeline work |
| `playbooks/debug-collection.md` | a collection/login/schedule run fails |

Keep playbooks under ~60 lines. If a rule is universal, it belongs in
`AGENTS.md` instead of being repeated here.
