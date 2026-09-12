<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '@/lib/api'
import type { Category, CategoryStats, ManualPrompt } from '@/types/api'

const categories = ref<Category[]>([])
const stats = ref<CategoryStats | null>(null)
const loading = ref(true)
const error = ref('')
const busy = ref(false)
const newRoot = ref('')
const backfillMsg = ref('')

async function load() {
  loading.value = true
  try {
    const [cats, st] = await Promise.all([
      api.categories.list(),
      api.categories.stats().catch(() => null),
    ])
    categories.value = cats
    stats.value = st
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

onMounted(load)

// category_id -> {direct, rollup} item counts
const statByCat = computed(() => {
  const m = new Map<string, { direct: number; rollup: number }>()
  for (const s of stats.value?.per_category ?? []) {
    m.set(s.category_id, { direct: s.direct_items, rollup: s.rollup_items })
  }
  return m
})

// Tree order = sort by path, but path is slug-based; within a parent we want the
// server's sort_order. Build an explicit ordered walk instead.
const ordered = computed<{ cat: Category; depth: number }[]>(() => {
  const byParent = new Map<string | null, Category[]>()
  for (const c of categories.value) {
    const arr = byParent.get(c.parent_id) ?? []
    arr.push(c)
    byParent.set(c.parent_id, arr)
  }
  for (const arr of byParent.values()) arr.sort((a, b) => a.sort_order - b.sort_order || a.name.localeCompare(b.name))
  const out: { cat: Category; depth: number }[] = []
  const walk = (parentId: string | null, depth: number) => {
    for (const c of byParent.get(parentId) ?? []) {
      out.push({ cat: c, depth })
      walk(c.id, depth + 1)
    }
  }
  walk(null, 0)
  return out
})

function siblings(cat: Category): Category[] {
  return categories.value
    .filter((c) => c.parent_id === cat.parent_id)
    .sort((a, b) => a.sort_order - b.sort_order)
}

async function run(fn: () => Promise<unknown>) {
  busy.value = true
  error.value = ''
  try {
    await fn()
    await load()
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}

function addRoot() {
  const name = newRoot.value.trim()
  if (!name) return
  run(async () => {
    await api.categories.create({ name })
    newRoot.value = ''
  })
}

function addChild(parent: Category) {
  const name = prompt(`Нова підкатегорія в «${parent.name}»:`)?.trim()
  if (name) run(() => api.categories.create({ name, parent_id: parent.id }))
}

function rename(cat: Category) {
  const name = prompt('Нова назва:', cat.name)?.trim()
  if (name && name !== cat.name) run(() => api.categories.update(cat.id, { name }))
}

function remove(cat: Category) {
  if (confirm(`Видалити «${cat.name}»? Підкатегорії піднімуться на рівень вище, товари втратять цю категорію.`))
    run(() => api.categories.remove(cat.id))
}

function move(cat: Category, dir: -1 | 1) {
  const sibs = siblings(cat)
  const i = sibs.findIndex((c) => c.id === cat.id)
  const other = sibs[i + dir]
  if (i === -1 || !other) return
  run(async () => {
    await api.categories.update(cat.id, { sort_order: other.sort_order })
    await api.categories.update(other.id, { sort_order: cat.sort_order })
  })
}

// --- Manual classification (bring your own AI) ---------------------------
const manual = ref<ManualPrompt | null>(null)
const manualN = ref(40)
const manualMode = ref<'single' | 'files'>('single')
const manualAnswer = ref('')
const manualMsg = ref('')
const copied = ref('')

function genPrompt() {
  run(async () => {
    manualMsg.value = ''
    manual.value =
      manualMode.value === 'files'
        ? await api.categories.manualPrompt(5000, manualN.value)
        : await api.categories.manualPrompt(manualN.value, manualN.value)
    if (!manual.value.count) manualMsg.value = 'Немає невідомих назв — усе вже класифіковано.'
  })
}
async function copyText(text: string, tag: string) {
  await navigator.clipboard.writeText(text)
  copied.value = tag
  setTimeout(() => (copied.value = ''), 1500)
}
function downloadText(text: string, filename: string) {
  const a = document.createElement('a')
  a.href = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }))
  a.download = filename
  a.click()
  URL.revokeObjectURL(a.href)
}
function downloadAllBatches() {
  for (const b of manual.value?.batches ?? []) downloadText(b.prompt, b.filename)
}

function resetAll() {
  if (!confirm(
    'Очистити ВСЮ категоризацію?\n' +
      'Зникнуть і AI-, і ручні прив’язки товарів до категорій, усі позиції стануть «Без категорії». Дерево категорій лишиться.',
  )) return
  run(async () => {
    backfillMsg.value = ''
    const r = await api.categories.reset()
    backfillMsg.value =
      `Очищено: аліасів ${r.aliases_cleared}, позицій ${r.items_cleared}.`
    manual.value = null
  })
}
function applyAnswer() {
  run(async () => {
    const r = await api.categories.manualApply(manualAnswer.value, manual.value?.names)
    manualMsg.value =
      `${r.names} назв: ${r.mapped} розкладено` +
      (r.unknown_path ? `, ${r.unknown_path} невідомий шлях` : '') +
      (r.low_confidence ? `, ${r.low_confidence} низька впевненість` : '') +
      (r.skipped_manual ? `, ${r.skipped_manual} ручних збережено` : '') +
      `; позицій оновлено ${r.items_tagged}.`
    manualAnswer.value = ''
    if (manualMode.value !== 'files') manual.value = null
  })
}

function backfill(force: boolean) {
  if (force && !confirm(
    'Скинути всі AI-категорії (ручні лишаться) і переразподілити всю історію заново?\n' +
      'Може зайняти час і витратити токени.',
  )) return
  run(async () => {
    backfillMsg.value = ''
    const r = await api.categories.backfill(force)
    if (!r.names_classified && !r.reset) {
      backfillMsg.value = 'Немає що класифікувати або не налаштовано AI-бекенд (див. Налаштування).'
      return
    }
    let msg = `Класифіковано назв: ${r.names_classified} (партій: ${r.batches}).`
    if (r.reset) msg += ` Скинуто AI-аліасів: ${r.reset.aliases_cleared}, повернуто ручних: ${r.reset.items_reapplied}.`
    backfillMsg.value = msg
  })
}
</script>

<template>
  <div class="toolbar">
    <h2 style="margin: 0; flex: 1; white-space: nowrap">Категорії товарів</h2>
    <button class="secondary" style="white-space: nowrap" :disabled="busy" @click="backfill(false)">
      Розібрати «Без категорії»
    </button>
    <button class="secondary" style="white-space: nowrap" :disabled="busy" @click="backfill(true)">
      Переразподілити все
    </button>
    <button class="secondary" style="white-space: nowrap; color: var(--danger)" :disabled="busy" @click="resetAll">
      Очистити всю категоризацію
    </button>
  </div>
  <p v-if="backfillMsg" class="muted">{{ backfillMsg }}</p>
  <p v-if="error" style="color: var(--danger)">{{ error }}</p>

  <div v-if="stats" class="card" style="margin-bottom: 16px; display: flex; gap: 24px; flex-wrap: wrap">
    <div>
      <div class="muted" style="font-size: 12px">Позицій з категорією</div>
      <strong style="font-size: 20px">{{ stats.total_items }}</strong>
    </div>
    <div>
      <div class="muted" style="font-size: 12px">Без категорії</div>
      <strong style="font-size: 20px" :style="{ color: stats.uncategorized_items ? 'var(--warning)' : undefined }">
        {{ stats.uncategorized_items }}
      </strong>
    </div>
    <div>
      <div class="muted" style="font-size: 12px">Класифіковано назв</div>
      <strong style="font-size: 20px">{{ stats.classified_names }}</strong>
    </div>
  </div>

  <details class="card" style="margin-bottom: 16px">
    <summary style="cursor: pointer; font-weight: 600">Ручна обробка (без API-ключа)</summary>
    <p class="muted" style="margin: 8px 0">
      Згенеруйте запит, вставте його в будь-який чат-бот (ChatGPT, Claude, Gemini…),
      скопіюйте JSON-відповідь сюди й натисніть «Застосувати».
    </p>
    <div class="toolbar">
      <label class="muted" style="font-size: 13px">
        {{ manualMode === 'files' ? 'Розмір батча:' : 'Скільки назв:' }}
        <input v-model.number="manualN" type="number" min="1" max="300" style="width: 80px; margin-left: 6px" />
      </label>
      <label class="muted" style="font-size: 13px">
        <input v-model="manualMode" type="radio" value="single" /> одним повідомленням
      </label>
      <label class="muted" style="font-size: 13px">
        <input v-model="manualMode" type="radio" value="files" /> файлами по батчах
      </label>
      <button class="secondary" :disabled="busy" @click="genPrompt">Згенерувати запит</button>
    </div>

    <template v-if="manual && manual.count && manualMode === 'single'">
      <div class="toolbar" style="margin-bottom: 6px">
        <span class="muted" style="font-size: 13px; flex: 1">Запит на {{ manual.count }} назв:</span>
        <button class="secondary" @click="copyText(manual.prompt, 'single')">
          {{ copied === 'single' ? 'Скопійовано ✓' : 'Копіювати' }}
        </button>
        <button class="secondary" @click="downloadText(manual.prompt, manual.filename)">Завантажити .txt</button>
      </div>
      <textarea :value="manual.prompt" readonly rows="6"
        style="width: 100%; font-family: monospace; font-size: 12px"></textarea>
    </template>

    <template v-if="manual && manual.count && manualMode === 'files'">
      <div class="toolbar" style="margin-bottom: 6px">
        <span class="muted" style="font-size: 13px; flex: 1">
          1) Вставте цю інструкцію в чат-бот один раз:
        </span>
        <button class="secondary" @click="copyText(manual.preamble, 'pre')">
          {{ copied === 'pre' ? 'Скопійовано ✓' : 'Копіювати' }}
        </button>
      </div>
      <textarea :value="manual.preamble" readonly rows="5"
        style="width: 100%; font-family: monospace; font-size: 12px"></textarea>

      <div class="toolbar" style="margin: 10px 0 6px">
        <span class="muted" style="font-size: 13px; flex: 1">
          2) Потім по черзі надсилайте {{ manual.batches.length }} файл(ів) ({{ manual.count }} назв):
        </span>
        <button class="secondary" @click="downloadAllBatches">Завантажити всі</button>
      </div>
      <ul style="list-style: none; padding: 0; margin: 0">
        <li v-for="b in manual.batches" :key="b.index"
          style="display: flex; align-items: center; gap: 8px; padding: 4px 0">
          <span class="muted" style="font-size: 13px; flex: 1">{{ b.filename }} — {{ b.count }} назв</span>
          <button class="secondary" @click="copyText(b.prompt, 'b' + b.index)">
            {{ copied === 'b' + b.index ? '✓' : 'Копіювати' }}
          </button>
          <button class="secondary" @click="downloadText(b.prompt, b.filename)">.txt</button>
        </li>
      </ul>
    </template>

    <template v-if="manual && manual.count">
      <label class="muted" style="font-size: 13px; display: block; margin-top: 10px">Відповідь AI (JSON):</label>
      <textarea v-model="manualAnswer" rows="5" placeholder='{"1": {"category_path": "...", "confidence": 0.9}, ...}'
        style="width: 100%; font-family: monospace; font-size: 12px"></textarea>
      <button style="margin-top: 6px" :disabled="busy || !manualAnswer.trim()" @click="applyAnswer">Застосувати</button>
    </template>

    <p v-if="manualMsg" class="muted" style="margin-top: 8px">{{ manualMsg }}</p>
  </details>

  <div class="card" style="margin-bottom: 16px">
    <div class="toolbar" style="margin-bottom: 0">
      <input v-model="newRoot" placeholder="Нова коренева категорія" style="flex: 1" @keyup.enter="addRoot" />
      <button style="white-space: nowrap" :disabled="busy || !newRoot.trim()" @click="addRoot">Додати</button>
    </div>
  </div>

  <div v-if="loading" class="muted">Завантаження…</div>
  <table v-else class="cat-tree">
    <thead>
      <tr>
        <th>Категорія</th>
        <th style="text-align: right; white-space: nowrap">Позицій</th>
        <th aria-label="Дії"></th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="{ cat, depth } in ordered" :key="cat.id">
        <td>
          <div :style="{ paddingLeft: depth * 16 + 'px' }">
            <div>{{ cat.name }}</div>
            <div class="cat-path">{{ cat.path }}</div>
          </div>
        </td>
        <td
          style="text-align: right; white-space: nowrap"
          :title="`Напряму в цій категорії: ${statByCat.get(cat.id)?.direct ?? 0}\nРазом з підкатегоріями: ${statByCat.get(cat.id)?.rollup ?? 0}`"
        >
          {{ statByCat.get(cat.id)?.direct ?? 0 }}<span class="muted"> / {{ statByCat.get(cat.id)?.rollup ?? 0 }}</span>
        </td>
        <td>
          <div class="row-actions">
            <button class="secondary" :disabled="busy" title="Вгору" @click="move(cat, -1)">↑</button>
            <button class="secondary" :disabled="busy" title="Вниз" @click="move(cat, 1)">↓</button>
            <button class="secondary" :disabled="busy" @click="addChild(cat)">+&nbsp;підкат.</button>
            <button class="secondary" :disabled="busy" @click="rename(cat)">Переймен.</button>
            <button class="danger" :disabled="busy" @click="remove(cat)">Видалити</button>
          </div>
        </td>
      </tr>
    </tbody>
  </table>
  <p class="muted" style="font-size: 12px; margin-top: 4px">
    «Позицій» — напряму в цій категорії / разом із підкатегоріями.
  </p>
</template>

<style scoped>
.cat-tree {
  width: 100%;
}
.cat-tree td {
  vertical-align: top;
}
.cat-tree td:nth-child(2) {
  white-space: nowrap;
}
.cat-path {
  color: var(--text-dim);
  font-size: 11px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  word-break: break-all;
}
.row-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  justify-content: flex-end;
  width: 168px;          /* fixed → the column shrinks to it, buttons wrap 3 + 2 */
  margin-left: auto;
}
.row-actions button {
  padding: 5px 9px;
  font-size: 12px;
  flex: 0 0 auto;
}
</style>
