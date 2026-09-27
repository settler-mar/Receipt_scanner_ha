<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { api, BASE } from '@/lib/api'
import ZoomableImage from '@/components/ZoomableImage.vue'
import type { Category, CategoryItemName, CategoryStats, ManualPrompt } from '@/types/api'

function imageSrc(url: string | null): string | null {
  if (!url) return null
  return url.startsWith('/') ? `${BASE}${url}` : url
}

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
const byParent = computed(() => {
  const m = new Map<string | null, Category[]>()
  for (const c of categories.value) {
    const arr = m.get(c.parent_id) ?? []
    arr.push(c)
    m.set(c.parent_id, arr)
  }
  for (const arr of m.values()) arr.sort((a, b) => a.sort_order - b.sort_order || a.name.localeCompare(b.name))
  return m
})

function hasChildren(cat: Category): boolean {
  return (byParent.value.get(cat.id)?.length ?? 0) > 0
}

// Collapsed by default: a node's children only render once its id is opened
// here. Roots themselves are always shown.
const expandedIds = ref<Set<string>>(new Set())
function toggleExpand(cat: Category) {
  const next = new Set(expandedIds.value)
  if (next.has(cat.id)) next.delete(cat.id)
  else next.add(cat.id)
  expandedIds.value = next
}
function expandAncestorsOf(id: string) {
  const next = new Set(expandedIds.value)
  let cur = categories.value.find((c) => c.id === id)
  while (cur?.parent_id) {
    next.add(cur.parent_id)
    cur = categories.value.find((c) => c.id === cur!.parent_id)
  }
  expandedIds.value = next
}

const ordered = computed<{ cat: Category; depth: number }[]>(() => {
  const out: { cat: Category; depth: number }[] = []
  const walk = (parentId: string | null, depth: number) => {
    for (const c of byParent.value.get(parentId) ?? []) {
      out.push({ cat: c, depth })
      if (expandedIds.value.has(c.id)) walk(c.id, depth + 1)
    }
  }
  walk(null, 0)
  return out
})

// --- tree editing: everything (reorder, indent/outdent, root hop) is one
// drag&drop gesture. Dropping in a row's top/bottom third inserts the dragged
// category as a sibling there (whatever parent that row has — that's how
// outdent/indent/root-hop all fall out of the same rule); the middle third
// nests it as that row's last child. A dedicated dropzone below the table
// pulls a category out to the top level. ---
function isSelfOrDescendant(candidateId: string, ofId: string): boolean {
  let cur: Category | undefined = categories.value.find((c) => c.id === candidateId)
  while (cur) {
    if (cur.id === ofId) return true
    cur = cur.parent_id ? categories.value.find((c) => c.id === cur!.parent_id) : undefined
  }
  return false
}

function nextSortOrder(parentId: string | null): number {
  const sibs = byParent.value.get(parentId) ?? []
  return sibs.length ? Math.max(...sibs.map((c) => c.sort_order)) + 1 : 0
}

function reparent(cat: Category, newParentId: string | null) {
  if (newParentId === cat.id || (newParentId && isSelfOrDescendant(newParentId, cat.id))) return
  if (newParentId === cat.parent_id) return
  run(async () => {
    await api.categories.update(cat.id, { parent_id: newParentId, sort_order: nextSortOrder(newParentId) })
    expandAncestorsOf(cat.id)
  })
}

type DropZone = 'before' | 'inside' | 'after'
const draggingId = ref<string | null>(null)
const dropTargetId = ref<string | null>(null)
const dropZone = ref<DropZone>('inside')

function onDragStart(cat: Category) {
  draggingId.value = cat.id
}
function onDragEnd() {
  draggingId.value = null
  dropTargetId.value = null
}
function onDragOverRow(cat: Category, event: DragEvent) {
  if (!draggingId.value || draggingId.value === cat.id || isSelfOrDescendant(cat.id, draggingId.value)) return
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  const frac = (event.clientY - rect.top) / rect.height
  dropZone.value = frac < 0.3 ? 'before' : frac > 0.7 ? 'after' : 'inside'
  dropTargetId.value = cat.id
}

// Final sibling order for `parentId` once `draggedId` is spliced in right
// before/after `targetId` (which is itself one of those siblings, or absent
// if it belongs to a different parent — reorder-only vs. reorder+reparent
// are the same operation here).
function siblingOrderWithInsert(parentId: string | null, draggedId: string, targetId: string, zone: 'before' | 'after'): string[] {
  const ids = (byParent.value.get(parentId) ?? []).map((c) => c.id).filter((id) => id !== draggedId)
  const at = ids.indexOf(targetId)
  ids.splice(zone === 'before' ? at : at + 1, 0, draggedId)
  return ids
}

function onDropRow(cat: Category) {
  const dragged = categories.value.find((c) => c.id === draggingId.value)
  const zone = dropZone.value
  onDragEnd()
  if (!dragged || dragged.id === cat.id) return
  if (zone === 'inside') {
    reparent(dragged, cat.id)
    return
  }
  // Insert as a sibling of `cat` — same guard reparent() uses, since `cat`'s
  // parent becomes the dragged category's new parent too.
  if (cat.parent_id && isSelfOrDescendant(cat.parent_id, dragged.id)) return
  const order = siblingOrderWithInsert(cat.parent_id, dragged.id, cat.id, zone)
  run(async () => {
    for (let i = 0; i < order.length; i++) {
      const id = order[i]
      if (!id) continue
      const current = categories.value.find((c) => c.id === id)!
      const needsReparent = id === dragged.id && dragged.parent_id !== cat.parent_id
      if (current.sort_order === i && !needsReparent) continue
      await api.categories.update(id, needsReparent ? { parent_id: cat.parent_id, sort_order: i } : { sort_order: i })
    }
    expandAncestorsOf(dragged.id)
  })
}
function onDropRoot() {
  const dragged = categories.value.find((c) => c.id === draggingId.value)
  if (dragged) reparent(dragged, null)
  onDragEnd()
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

// --- Browse & re-sort: what's actually in this category (see /store-category
// for the sibling pattern), so a catch-all bucket can be split up on the spot ---
const browseCat = ref<Category | null>(null)
const browseNames = ref<CategoryItemName[]>([])
const browseStatus = ref<'loading' | 'ready' | 'error'>('loading')
const browseSaving = ref<string | null>(null)
// Off by default (matches the API), but auto-on when the browsed node has
// children — a leaf with 0 direct items is the normal "everything's already
// sorted into subcategories" case, not an empty one.
const browseIncludeDescendants = ref(false)

// Flat, depth-indented options for the per-row category picker — same shape
// as StoreCategoryWidget's `categoryOptions`.
const categoryOptions = computed(() =>
  [...categories.value]
    .sort((a, b) => a.path.localeCompare(b.path))
    .map((c) => ({ id: c.id, label: `${'— '.repeat(c.path.split('/').length - 1)}${c.name}` })),
)
const categoryById = computed(() => new Map(categories.value.map((c) => [c.id, c])))

async function loadBrowseNames() {
  if (!browseCat.value) return
  browseStatus.value = 'loading'
  try {
    browseNames.value = (await api.categories.items(browseCat.value.id, browseIncludeDescendants.value)).names
    browseStatus.value = 'ready'
  } catch {
    browseStatus.value = 'error'
  }
}
function openBrowse(cat: Category) {
  browseCat.value = cat
  browseNames.value = []
  browseIncludeDescendants.value = hasChildren(cat)
  void loadBrowseNames()
}
async function reassignName(n: CategoryItemName, targetCategoryId: string) {
  const current = n.example_category_id ?? browseCat.value?.id
  if (!browseCat.value || !targetCategoryId || targetCategoryId === current) return
  browseSaving.value = n.normalized_name
  try {
    await api.categories.reassignItems({
      category_id: browseCat.value.id,
      normalized_names: [n.normalized_name],
      target_category_id: targetCategoryId,
      include_descendants: browseIncludeDescendants.value,
    })
    await Promise.all([loadBrowseNames(), load()])
  } finally {
    browseSaving.value = null
  }
}

// Governs both the auto backfill and the manual "bring your own AI" flow
// below: with it on, names currently parked in the "Інше" catch-all are
// treated as pending too (including overriding their sticky manual pick —
// see backend categorize.apply_guesses for why that's safe here).
const includeOther = ref(false)

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
        ? await api.categories.manualPrompt(5000, manualN.value, includeOther.value)
        : await api.categories.manualPrompt(manualN.value, manualN.value, includeOther.value)
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
    const r = await api.categories.manualApply(manualAnswer.value, manual.value?.names, includeOther.value)
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
    const r = await api.categories.backfill(force, includeOther.value)
    if (r.queued) {
      backfillMsg.value = 'Класифікацію запущено у фоні. Оновіть сторінку пізніше, щоб побачити результат.'
      return
    }
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
    <label
      class="muted" style="font-size: 13px; white-space: nowrap; display: flex; align-items: center; gap: 4px"
      title='Разом з «Без категорії» також розібрати (чи прогнати через ручний запит) назви, які зараз лежать у «Інше» — включно з тими, що туди віднесли вручну'
    >
      <input v-model="includeOther" type="checkbox" />
      разом з «Інше»
    </label>
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
      <label style="white-space: nowrap">
        <input v-model="includeOther" type="checkbox" /> разом з «Інше»
      </label>
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
      <tr
        v-for="{ cat, depth } in ordered"
        :key="cat.id"
        draggable="true"
        :title="'Перетягніть, щоб пересортувати або вкласти в іншу категорію'"
        :class="{
          'cat-dragging': draggingId === cat.id,
          'cat-drop-before': dropTargetId === cat.id && dropZone === 'before',
          'cat-drop-inside': dropTargetId === cat.id && dropZone === 'inside',
          'cat-drop-after': dropTargetId === cat.id && dropZone === 'after',
        }"
        @dragstart="onDragStart(cat)"
        @dragend="onDragEnd"
        @dragover.prevent="onDragOverRow(cat, $event)"
        @drop.prevent="onDropRow(cat)"
      >
        <td>
          <div class="cat-name-row" :style="{ paddingLeft: depth * 16 + 'px' }">
            <button
              v-if="hasChildren(cat)"
              class="cat-toggle"
              type="button"
              :title="expandedIds.has(cat.id) ? 'Згорнути підкатегорії' : 'Розгорнути підкатегорії'"
              @click="toggleExpand(cat)"
            >{{ expandedIds.has(cat.id) ? '▾' : '▸' }}</button>
            <span v-else class="cat-toggle cat-toggle--spacer"></span>
            <div>
              <div>{{ cat.name }}</div>
              <div class="cat-path">{{ cat.path }}</div>
            </div>
          </div>
        </td>
        <td
          style="text-align: right; white-space: nowrap; cursor: pointer"
          title="Переглянути товари в цій категорії"
          @click="openBrowse(cat)"
        >
          {{ statByCat.get(cat.id)?.direct ?? 0 }}<span class="muted"> / {{ statByCat.get(cat.id)?.rollup ?? 0 }}</span>
        </td>
        <td>
          <div class="row-actions">
            <button class="secondary" :disabled="busy" title="Переглянути товари в цій категорії" @click="openBrowse(cat)">👁</button>
            <button class="secondary" :disabled="busy" :title="`Додати підкатегорію в «${cat.name}»`" @click="addChild(cat)">+</button>
            <button class="secondary" :disabled="busy" title="Перейменувати" @click="rename(cat)">✎</button>
            <button class="danger" :disabled="busy" title="Видалити (підкатегорії піднімуться на рівень вище)" @click="remove(cat)">🗑</button>
          </div>
        </td>
      </tr>
    </tbody>
  </table>
  <div
    class="root-dropzone"
    :class="{ 'root-dropzone--active': draggingId }"
    @dragover.prevent
    @drop.prevent="onDropRoot"
  >
    Перетягніть сюди категорію, щоб зробити її кореневою
  </div>
  <p class="muted" style="font-size: 12px; margin-top: 4px">
    «Позицій» — напряму в цій категорії / разом із підкатегоріями. Клік по числу або 👁 — перелік товарів усередині.
    Перетягніть рядок мишею: угору/вниз країв цілі — поставити поруч (сортування, у тому числі в іншу батьківську
    категорію); у середину — вкласти як підкатегорію.
  </p>

  <!-- browse & re-sort: what's inside this category right now -->
  <div v-if="browseCat" class="modal-backdrop" @click.self="browseCat = null">
    <div class="modal" style="width: min(720px, 94vw); max-height: 85vh; overflow: auto">
      <div class="toolbar" style="flex-wrap: nowrap; margin-bottom: 4px">
        <strong style="flex: 1">{{ browseCat.name }} <span class="cat-path">{{ browseCat.path }}</span></strong>
        <button class="secondary" style="padding: 2px 8px" @click="browseCat = null">×</button>
      </div>
      <div class="toolbar" style="margin: 0 0 4px">
        <label
          v-if="hasChildren(browseCat)" class="muted" style="font-size: 13px; display: flex; align-items: center; gap: 4px"
          title="Показати й товари з усіх підкатегорій, не лише напряму в цьому вузлі"
        >
          <input v-model="browseIncludeDescendants" type="checkbox" @change="loadBrowseNames" />
          разом з підкатегоріями
        </label>
      </div>
      <p class="muted" style="margin-top: 0; font-size: 13px">
        Оберіть іншу категорію для назви — вибір одразу переносить усі такі позиції й закріплюється за назвою.
      </p>

      <p v-if="browseStatus === 'loading'" class="muted">Завантаження…</p>
      <p v-else-if="browseStatus === 'error'" class="muted">Не вдалося завантажити.</p>
      <p v-else-if="!browseNames.length && browseIncludeDescendants" class="muted">
        Тут порожньо, разом з підкатегоріями.
      </p>
      <p v-else-if="!browseNames.length" class="muted">
        Напряму в цьому вузлі порожньо{{ hasChildren(browseCat) ? ' — усе вже розкладено по підкатегоріях.' : '.' }}
      </p>
      <table v-else class="cat-browse">
        <thead>
          <tr>
            <th></th>
            <th>Назва</th>
            <th style="text-align: right">Позицій</th>
            <th>Перенести в</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="n in browseNames" :key="n.normalized_name">
            <td>
              <ZoomableImage
                v-if="imageSrc(n.example_image_url)"
                :src="imageSrc(n.example_image_url)!"
                :alt="n.display_name"
                style="width: 36px; height: 36px; object-fit: contain; border-radius: 6px"
              />
            </td>
            <td>
              <RouterLink v-if="n.example_receipt_id" :to="`/receipts/${n.example_receipt_id}`">
                {{ n.display_name }}
              </RouterLink>
              <span v-else>{{ n.display_name }}</span>
              <span v-if="n.source" class="muted" style="font-size: 11px"> ({{ n.source }})</span>
              <div
                v-if="browseIncludeDescendants && n.example_category_id && n.example_category_id !== browseCat.id"
                class="muted" style="font-size: 11px"
              >
                зараз у: {{ categoryById.get(n.example_category_id)?.name ?? '?' }}
              </div>
            </td>
            <td style="text-align: right">{{ n.count }}</td>
            <td>
              <select
                :value="n.example_category_id ?? browseCat.id"
                :disabled="browseSaving === n.normalized_name"
                @change="reassignName(n, ($event.target as HTMLSelectElement).value)"
              >
                <option v-for="o in categoryOptions" :key="o.id" :value="o.id">{{ o.label }}</option>
              </select>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
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
  gap: 4px;
  justify-content: flex-end;
  width: 108px;          /* fixed → the column shrinks to it, 4 icon buttons in one row */
  margin-left: auto;
}
.row-actions button {
  padding: 4px 8px;
  font-size: 13px;
  line-height: 1;
  flex: 0 0 auto;
}
.cat-name-row {
  display: flex;
  align-items: flex-start;
  gap: 4px;
}
.cat-toggle {
  flex: 0 0 auto;
  width: 16px;
  height: 20px;
  padding: 0;
  border: none;
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
  font-size: 11px;
  line-height: 20px;
  text-align: center;
}
.cat-toggle--spacer {
  cursor: default;
}
.cat-dragging {
  opacity: 0.4;
}
/* Drop-zone feedback: a line on the edge the row would land next to, or a
   full outline when it would be nested inside instead. */
.cat-drop-before > td {
  box-shadow: inset 0 2px 0 0 var(--accent, #4a9eff);
}
.cat-drop-after > td {
  box-shadow: inset 0 -2px 0 0 var(--accent, #4a9eff);
}
.cat-drop-inside > td {
  outline: 2px dashed var(--accent, #4a9eff);
  outline-offset: -2px;
}
.root-dropzone {
  margin-top: 8px;
  padding: 10px;
  border: 1px dashed var(--border);
  border-radius: 8px;
  text-align: center;
  font-size: 12px;
  color: var(--text-dim);
  opacity: 0.5;
}
.root-dropzone--active {
  opacity: 1;
  border-color: var(--accent, #4a9eff);
}
.cat-browse {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.cat-browse th,
.cat-browse td {
  padding: 5px 8px;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
}
</style>
