# Codex / ChatGPT Setup

Codex CLI already reads `AGENTS.md` at the repo root — that is the shared rule
set. This folder adds the same task recipes Claude gets as skills.

## Slash prompts (Codex CLI)

Codex only loads prompts from `~/.codex/prompts`. Link them once:

```bash
./.codex/install-prompts.sh
```

Then `/rs-adapter`, `/rs-api`, `/rs-db`, `/rs-ui`, `/rs-scanner`, `/rs-checks`,
`/rs-debug`, `/rs-review` are available in any Codex session in this repo.

## ChatGPT (web, Projects)

Paste `CHATGPT_PROJECT_INSTRUCTIONS.md` into the Project's custom instructions.
It is deliberately short; the detail lives in the repo files it names, which you
attach or paste only when the task needs them.

## Keeping it cheap

All prompts point at `.agents/playbooks/*.md`. Edit the playbook, not eight
copies.
