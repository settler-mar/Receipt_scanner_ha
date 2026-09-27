# Задачи — статус

Ревизия корневых задач/планов 2026-09-27. Живые архитектурные справочники
(`RESEARCH.md`, `SERVICE.md`, `DOCS.md`) остались в корне — это не задачи, а
справка, на которую ссылаются инструменты (`PROJECT_STRUCTURE.md`,
`.agents/playbooks/token-budget.md` и т.д.).

## done/

- [CAPTURE_PLAN.md](done/CAPTURE_PLAN.md) — кроки 1–4 реалізовані (2026-09-09);
  відкладено лише локальний faster-whisper.
- [DUPLICATE_RECEIPTS_PROMPT.md](done/DUPLICATE_RECEIPTS_PROMPT.md) — постановка
  задачи по дедупликации чеков; реализовано (`backend/app/services/duplicates.py`,
  `api/duplicates.py`, `DuplicatesView.vue`).

## in-progress/

- [ROADMAP.md](in-progress/ROADMAP.md) — кроки 0, 1, 2, 4 готові; крок 3
  (роутинг інбоксу) — бекенд-каркас готовий, лишилась звірка шаблонів/OCR на
  живих фото та live-перевірка кабінету ДПС.
- [SHOPPING_LIST_WORKPLAN.md](in-progress/SHOPPING_LIST_WORKPLAN.md) — активна
  точка синхронізації списку покупок; розділ «Что ещё нужно сделать» містить
  відкриті пріоритети 1–6.

## planned/

Пусто на 2026-09-27 — нет отдельных документов для незапущенных задач. Кладите
сюда новые постановки задач до начала работы над ними.
