---
description: Vue 3 view / component / dashboard widget change.
---
Follow `.agents/playbooks/frontend.md`.

Reuse `src/lib` helpers and existing components, keep current UI wording, handle
loading/empty/error states. Verify with
`npx vue-tsc --noEmit -p tsconfig.app.json | grep <file>` (Node 24 in PATH);
~8 type errors are pre-existing.

Task: $ARGUMENTS
