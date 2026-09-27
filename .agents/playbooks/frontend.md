# Frontend (Vue 3 + Vite)

Composition API with `<script setup lang="ts">`, matching the file you edit.
This is a dense operational app, not a marketing site.

## Where things go

- Page -> `frontend/src/views/`
- Reusable UI -> `frontend/src/components/`
- Dashboard tile -> `frontend/src/widgets/` + its registry entry; data via
  `composables/useWidgetData.ts`; backend side in `backend/app/api/widgets.py`
- Shared HTTP -> `frontend/src/lib/api.ts` / `shoppingApi.ts`
- Types -> `frontend/src/types/`
- Formatting/date -> `frontend/src/lib/dashboardFmt.ts`, `lib/datetime.ts`
  (never hand-roll a date format or a TZ conversion)
- Routes -> `frontend/src/router/`

## Rules

- Keep existing Ukrainian/Russian UI wording unless the task is about wording.
- Reuse existing components and CSS variables from `assets/main.css`; no new
  UI library, no new dependency without a clear win.
- Widgets must handle loading / empty / error states — the collector legitimately
  fails and that must be visible, not silent.

## Check

```bash
export PATH=/home/max/.nvm/versions/node/v24.19.0/bin:$PATH
cd frontend && npx vue-tsc --noEmit -p tsconfig.app.json | grep <your-file>
cd frontend && npm run build
```

~8 type errors are PRE-EXISTING (bwip-js, PasswordInput, AccountsView,
ProductsView, StoreCategoryWidget) — leave them alone.
