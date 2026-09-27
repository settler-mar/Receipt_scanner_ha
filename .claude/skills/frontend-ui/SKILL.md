---
name: frontend-ui
description: Vue 3 frontend work — views, components, dashboard widgets, composables, API types, styling. Use for any change under frontend/src, including new screens, widget tiles, and TypeScript build/type errors.
---

Read `.agents/playbooks/frontend.md` and follow it.

Before `npm run build`, export the Node 24 PATH from the playbook. Ignore the ~8
pre-existing vue-tsc errors; verify only your own files with
`npx vue-tsc --noEmit -p tsconfig.app.json | grep <file>`.
