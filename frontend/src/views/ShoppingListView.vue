<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '@/lib/api'
import { shoppingApi } from '@/lib/shoppingApi'
import ShoppingBulkBar from '@/components/ShoppingBulkBar.vue'
import type { LiveSuggestStore, ShoppingArchiveReceipt, ShoppingItem, ShoppingMatchCandidate, ShoppingRoutine, ShoppingTrip, SuggestStore } from '@/types/shopping'
import { fmtDate } from '@/lib/datetime'

const items = ref<ShoppingItem[]>([])
const suggestions = ref<SuggestStore[]>([])
const liveSuggestions = ref<LiveSuggestStore[] | null>(null)
const liveLoading = ref(false)
const catName = ref<Record<string, string>>({})
const categories = ref<{ id: string; name: string; path: string }[]>([])
const loading = ref(true)
const error = ref('')
const notice = ref('')
const busy = ref(false)
const activeTab = ref<'shopping' | 'recommendations' | 'archive'>('shopping')
const trips = ref<ShoppingTrip[]>([])
const ownerName = ref(localStorage.getItem('shopping.ownerName') || '')
const countdownNow = ref(Date.now())
const routines = ref<ShoppingRoutine[]>([])
const archiveRows = ref<ShoppingArchiveReceipt[]>([])
const matchCandidates = ref<ShoppingMatchCandidate[]>([])
const stores = ref<{ code: string; name: string }[]>([])
const storeClasses = ref<{ code: string; name: string }[]>([])
const selectedStore = ref<string | null>(null)
const selectedPendingIds = ref<Set<string>>(new Set())
const selectedBoughtIds = ref<Set<string>>(new Set())
const newText = ref('')
const newQty = ref<number>(1)
const newNote = ref('')
let tripSyncTimer: ReturnType<typeof setInterval> | null = null
let countdownTimer: ReturnType<typeof setInterval> | null = null
watch(ownerName, (value) => localStorage.setItem('shopping.ownerName', value.trim()))

const pending = computed(() => items.value.filter((i) => i.status === 'pending'))

// Grouping and sorting are independent: the list is cut into groups first,
// then every group is ordered the same way. Both choices survive a reload.
type GroupMode = 'store_class' | 'category' | 'store' | 'none'
type SortMode = 'added_desc' | 'added_asc' | 'name'
const groupMode = ref<GroupMode>((localStorage.getItem('shopping.group') as GroupMode) || 'store_class')
const sortMode = ref<SortMode>((localStorage.getItem('shopping.sort') as SortMode) || 'added_desc')
watch(groupMode, (value) => localStorage.setItem('shopping.group', value))
watch(sortMode, (value) => localStorage.setItem('shopping.sort', value))

const byText = (a: string, b: string) => a.localeCompare(b, 'uk')
function sortRows(rows: ShoppingItem[]) {
  switch (sortMode.value) {
    case 'added_asc':
      return rows.sort((a, b) => a.added_at.localeCompare(b.added_at))
    case 'name':
      return rows.sort((a, b) => byText(a.raw_text, b.raw_text))
    default:
      return rows.sort((a, b) => b.added_at.localeCompare(a.added_at))
  }
}

// Group key/label per row. An empty key means "no group" and is always shown
// last, under a neutral heading.
function groupOf(i: ShoppingItem): [string, string] {
  switch (groupMode.value) {
    case 'category':
      return [i.category_id ?? '', (i.category_id && catName.value[i.category_id]) || '']
    case 'store':
      return [i.target_store_code ?? '', storeName(i.target_store_code)]
    case 'none':
      return ['', '']
    default: {
      // Класс магазина: явно выбранная сеть, иначе — по категории товара.
      const code = i.store_class && i.store_class !== 'unknown' ? i.store_class : ''
      return [code, code ? i.store_class_name ?? '' : '']
    }
  }
}

const emptyGroupLabel = computed(() => ({
  store_class: 'Тип магазина не определён',
  category: 'Без категорії',
  store: 'Магазин не выбран',
  none: '',
}[groupMode.value]))

// Display order of the store classes comes from the backend dictionary; other
// modes are alphabetical. "No group" always sinks to the bottom.
const groupRank = (key: string) =>
  groupMode.value === 'store_class'
    ? storeClasses.value.findIndex((c) => c.code === key)
    : -1

const pendingGroups = computed<[string, ShoppingItem[]][]>(() => {
  if (groupMode.value === 'none') return [['', sortRows([...pending.value])]]
  const rows = new Map<string, ShoppingItem[]>()
  const labels = new Map<string, string>()
  for (const i of pending.value) {
    const [key, label] = groupOf(i)
    labels.set(key, label)
    rows.set(key, [...(rows.get(key) ?? []), i])
  }
  return [...rows.entries()]
    .sort(([a], [b]) => {
      if (!a) return 1
      if (!b) return -1
      const [ra, rb] = [groupRank(a), groupRank(b)]
      if (ra !== rb) return ra - rb
      return byText(labels.get(a) ?? '', labels.get(b) ?? '')
    })
    .map(([key, group]) => [labels.get(key) || emptyGroupLabel.value, sortRows(group)] as [string, ShoppingItem[]])
})
const candidates = computed(() => items.value.filter((i) => i.status === 'bought' && i.auto_matched))
// Confirmed receipt matches live in Archive; the active "Куплено" block is
// reserved for manual marks that still have no receipt attached.
const bought = computed(() => items.value.filter((i) => i.status === 'bought' && !i.auto_matched && !i.bought_receipt_item_id))

function fmtMoney(v: number) {
  return new Intl.NumberFormat('uk-UA', { style: 'currency', currency: 'UAH' }).format(v)
}
function fmtQty(v: number) {
  return String(v)
}
function methodLabel(method: string) {
  return ({ web: 'веб', telegram: 'Telegram', voice: 'голос', recommendation: 'рекомендация', mcp: 'интеграция', capture: 'ввод' } as Record<string, string>)[method] ?? method
}

// `silent` refreshes keep the rendered list on screen: only the very first
// load may swap the page for a spinner, every later one just replaces the data.
async function load(silent = false) {
  if (!silent) loading.value = true
  error.value = ''
  try {
    const [list, sugg, cats, routineRows, archived, candidateRows, activeTrips] = await Promise.all([
      // The page has a separate "Куплено" block, so it needs all active rows
      // after a checkbox click; the API default intentionally returns pending
      // rows only for lightweight consumers such as Telegram.
      shoppingApi.list('all'),
      shoppingApi.suggestStore(),
      api.categories.list().catch(() => []),
      shoppingApi.routines().catch(() => []),
      shoppingApi.archive().catch(() => []),
      shoppingApi.matchCandidates().catch(() => []),
      shoppingApi.activeTrips().catch(() => []),
    ])
    items.value = list
    trips.value = activeTrips
    selectedPendingIds.value = new Set(
      [...selectedPendingIds.value].filter((id) => list.some((item) => item.id === id && item.status === 'pending')),
    )
    selectedBoughtIds.value = new Set(
      [...selectedBoughtIds.value].filter((id) => list.some((item) => item.id === id && item.status === 'bought' && !item.auto_matched && !item.bought_receipt_item_id)),
    )
    suggestions.value = sugg
    categories.value = cats
    catName.value = Object.fromEntries(cats.map((c) => [c.id, c.name]))
    routines.value = routineRows
    archiveRows.value = archived
    matchCandidates.value = candidateRows
    stores.value = await api.stores.list().catch(() => [])
    if (!storeClasses.value.length) storeClasses.value = await api.stores.classes().catch(() => [])
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

const refresh = () => load(true)

// Poll compact trip data so several participants see each other's checkbox
// changes, newly opened trips, and server-side auto-completion.
async function syncTrip() {
  if (busy.value) return
  try {
    trips.value = await shoppingApi.activeTrips()
  } catch {
    // The main page already has its own error state; background sync must not
    // turn a transient request failure into a distracting error banner.
  }
}

async function add() {
  const text = newText.value.trim()
  if (!text || busy.value) return
  busy.value = true
  try {
    const lines = text.split('\n').map((l) => l.trim()).filter(Boolean)
    for (const [idx, line] of lines.entries()) {
      // qty / note apply to a single typed item; a pasted multi-line list adds
      // plain rows the user can then adjust inline.
      await shoppingApi.create({
        raw_text: line,
        qty: lines.length === 1 ? Number(newQty.value) || 1 : 1,
        note: lines.length === 1 && idx === 0 ? newNote.value.trim() || null : null,
      })
    }
    newText.value = ''
    newQty.value = 1
    newNote.value = ''
    await refresh()
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}

async function act(fn: () => Promise<unknown>) {
  busy.value = true
  try {
    await fn()
    await refresh()
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}

// Row-level actions: apply the change to the row that is already on screen and
// re-read the list in the background, so nothing disables or re-mounts.
async function quiet(fn: () => Promise<unknown>) {
  try {
    await fn()
    await refresh()
  } catch (e) {
    error.value = (e as Error).message
  }
}

// Inline qty/note edits — patch without a full reload so the input keeps focus.
async function patchField(i: ShoppingItem, patch: BulkPatch & { qty?: number; note?: string | null }) {
  try {
    const updated = await shoppingApi.update(i.id, patch)
    Object.assign(i, updated)
  } catch (e) {
    error.value = (e as Error).message
  }
}

// Click-to-edit: a row shows plain values and swaps exactly one of them for an
// input/select while it is being edited, which keeps the list readable.
type EditField = 'raw_text' | 'qty' | 'note' | 'category_id' | 'target_store_code' | 'tag'
const editing = ref<{ id: string; field: EditField } | null>(null)
const isEditing = (i: ShoppingItem, field: EditField) =>
  editing.value?.id === i.id && editing.value.field === field
const startEdit = (i: ShoppingItem, field: EditField) => { editing.value = { id: i.id, field } }
const stopEdit = () => { editing.value = null }
const vAutofocus = {
  mounted(el: HTMLElement) {
    el.focus()
    if (el instanceof HTMLInputElement) el.select()
  },
}

function commit(i: ShoppingItem, field: EditField, raw: string) {
  stopEdit()
  const value: string | number | null =
    field === 'qty' ? Number(raw) || 1 : raw.trim() || null
  if (i[field] === value) return
  patchField(i, { [field]: value })
}
const onEditInput = (i: ShoppingItem, field: EditField, e: Event) =>
  commit(i, field, (e.target as HTMLInputElement | HTMLSelectElement).value)

const storeName = (code: string | null | undefined) =>
  stores.value.find((s) => s.code === code)?.name || code || ''

const markBought = (i: ShoppingItem) => quiet(() => shoppingApi.update(i.id, { status: 'bought' }))
function togglePending(i: ShoppingItem) {
  const next = new Set(selectedPendingIds.value)
  next.has(i.id) ? next.delete(i.id) : next.add(i.id)
  selectedPendingIds.value = next
}
function toggleBought(i: ShoppingItem) {
  const next = new Set(selectedBoughtIds.value)
  next.has(i.id) ? next.delete(i.id) : next.add(i.id)
  selectedBoughtIds.value = next
}

// Both list blocks share one set of group actions; `block` only decides which
// selection they run over and what the status button means.
type BulkPatch = { category_id?: string | null; target_store_code?: string | null; tag?: string | null }
type Block = 'pending' | 'bought'
const selectionOf = (block: Block) => (block === 'pending' ? selectedPendingIds : selectedBoughtIds)
const bulkRun = (block: Block, fn: (id: string) => Promise<unknown>) =>
  act(async () => {
    const selection = selectionOf(block)
    for (const id of [...selection.value]) await fn(id)
    selection.value = new Set()
  })
const bulkStatus = (block: Block) =>
  bulkRun(block, (id) => shoppingApi.update(id, { status: block === 'pending' ? 'bought' : 'pending' }))
const bulkAssign = (block: Block, patch: BulkPatch) => bulkRun(block, (id) => shoppingApi.update(id, patch))
const bulkRemove = (block: Block) => bulkRun(block, (id) => shoppingApi.remove(id))
const clearSelection = (block: Block) => { selectionOf(block).value = new Set() }
const markPending = (i: ShoppingItem) => quiet(() => shoppingApi.update(i.id, { status: 'pending' }))
function remove(i: ShoppingItem) {
  items.value = items.value.filter((row) => row.id !== i.id)
  return quiet(() => shoppingApi.remove(i.id))
}
const confirm = (i: ShoppingItem) => act(() => shoppingApi.confirm(i.id))
const reject = (i: ShoppingItem) => act(() => shoppingApi.reject(i.id))
const rescan = () => act(() => shoppingApi.rescan())
const categorize = () => act(async () => {
  notice.value = ''
  const result = await shoppingApi.categorize()
  if (result.ai_error) {
    error.value = `AI-класифікація недоступна: ${result.ai_error}. За словниками розкладено ${result.by_dictionary}.`
    return
  }
  notice.value = result.categorized
    ? `Перевірено ${result.checked}: за словниками ${result.by_dictionary}, через AI ${result.by_ai}.`
    : `Перевірено ${result.checked} позицій: нових категорій не знайдено.`
})
const startTrip = () => act(async () => {
  const name = ownerName.value.trim() || 'Неизвестно'
  const created = await shoppingApi.createTrip(selectedStore.value, name)
  trips.value = [...trips.value, created]
})
const storeLabel = (code: string | null | undefined) => stores.value.find((s) => s.code === code)?.name || code || 'магазин не выбран'
const finishTrip = (trip: ShoppingTrip) => act(async () => {
  await shoppingApi.completeTrip(trip.id)
  trips.value = trips.value.filter((row) => row.id !== trip.id)
})
const toggleTripItem = (trip: ShoppingTrip, item: ShoppingTrip['items'][number]) => act(async () => {
  item.status = item.status === 'bought' ? 'planned' : 'bought'
  await shoppingApi.updateTripItem(trip.id, item.id, item.status)
})
const remainingMs = (trip: ShoppingTrip) => Math.max(0, new Date(trip.expires_at).getTime() - countdownNow.value)
const remainingLabel = (trip: ShoppingTrip) => {
  const seconds = Math.ceil(remainingMs(trip) / 1000)
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  return `${hours}:${String(minutes).padStart(2, '0')}`
}
const routineAction = (routine: ShoppingRoutine, action: 'add' | 'postpone' | 'ignore') =>
  act(async () => {
    await shoppingApi.routineAction(routine.normalized_name, action)
    routines.value = routines.value.filter((row) => row.normalized_name !== routine.normalized_name)
  })
const resolveCandidate = (candidate: ShoppingMatchCandidate, action: 'confirm' | 'reject') =>
  act(async () => {
    await shoppingApi.resolveMatchCandidate(candidate.id, action)
    matchCandidates.value = matchCandidates.value.filter((row) => row.id !== candidate.id)
  })

async function checkLive() {
  liveLoading.value = true
  error.value = ''
  try {
    liveSuggestions.value = await shoppingApi.suggestStoreLive()
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    liveLoading.value = false
  }
}

onMounted(async () => {
  await load()
  tripSyncTimer = window.setInterval(syncTrip, 2000)
  countdownTimer = window.setInterval(() => { countdownNow.value = Date.now() }, 1000)
})
onBeforeUnmount(() => {
  if (tripSyncTimer !== null) window.clearInterval(tripSyncTimer)
  if (countdownTimer !== null) window.clearInterval(countdownTimer)
})
</script>

<template>
  <h2 style="margin-top: 0">Список покупок</h2>
  <p class="muted" style="margin-top: -8px">
    Ведіть список; після збору чека куплені позиції самі позначаються як «ймовірно куплено» —
    підтвердіть або відхиліть. Підбір магазину рахується з історії ваших чеків, без звернень до AI.
  </p>

  <div v-if="error" class="card" style="border-color: var(--danger); color: var(--danger)">
    {{ error }}
  </div>
  <div v-if="notice" class="card muted">{{ notice }}</div>

  <div class="toolbar" style="align-items: flex-start">
    <textarea
      v-model="newText"
      rows="1"
      placeholder="Назва позиції (можна кілька рядків)…"
      style="flex: 1; min-width: 220px; resize: vertical"
      @keydown.enter.exact.prevent="add"
    />
    <input
      v-model.number="newQty"
      type="number"
      min="0"
      step="0.001"
      title="Кількість"
      style="width: 80px"
    />
    <input
      v-model="newNote"
      type="text"
      placeholder="Примітка"
      style="width: 160px"
      @keydown.enter.prevent="add"
    />
    <button class="btn" :disabled="busy || !newText.trim()" @click="add">Додати</button>
    <button class="btn secondary" :disabled="busy || loading" @click="refresh()">Оновити</button>
    <button class="btn secondary" :disabled="busy || loading" @click="categorize">Класифікувати без категорії</button>
    <button class="btn secondary" :disabled="busy" @click="rescan">Перевірити по чеках</button>
    <select v-model="selectedStore" title="Магазин для этого похода" style="min-width: 170px">
      <option :value="null">Все магазины</option>
      <option v-for="store in stores" :key="store.code" :value="store.code">{{ store.name }}</option>
    </select>
    <input v-model="ownerName" type="text" placeholder="Кто идёт?" title="Имя участника похода" style="width: 130px" />
    <button class="btn secondary" :disabled="busy || !pending.length" @click="startTrip">Сформировать чеклист</button>
  </div>

  <div v-if="loading" class="muted">Завантаження…</div>

  <template v-else>
    <div class="toolbar" style="margin-bottom: 16px">
      <button
        class="btn"
        :class="{ secondary: activeTab !== 'shopping' }"
        @click="activeTab = 'shopping'"
      >
        Покупки <span class="muted">({{ pending.length }})</span>
      </button>
      <button
        class="btn"
        :class="{ secondary: activeTab !== 'recommendations' }"
        @click="activeTab = 'recommendations'"
      >
        Рекомендации<span v-if="routines.length"> · {{ routines.length }}</span>
      </button>
      <button
        class="btn"
        :class="{ secondary: activeTab !== 'archive' }"
        @click="activeTab = 'archive'"
      >
        Архив
      </button>
    </div>

    <template v-if="activeTab === 'shopping'">
    <div v-for="trip in trips" :key="trip.id" class="card" style="margin-bottom: 16px; border-color: var(--accent)">
      <div style="display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap">
        <h3 style="margin-top: 0; margin-bottom: 4px">Чеклист: {{ storeLabel(trip.store_code) }} ({{ trip.items.length }})</h3>
        <span class="muted" style="font-size: 13px">Открыт: <strong>{{ trip.owner_name }}</strong> · закроется через {{ remainingLabel(trip) }}</span>
        <button class="btn" :disabled="busy" @click="finishTrip(trip)">Завершить поход</button>
      </div>
      <div v-for="i in trip.items" :key="i.id" style="display: flex; gap: 8px; align-items: center; padding: 6px 0">
        <input type="checkbox" :checked="i.status === 'bought'" @change="toggleTripItem(trip, i)" />
        <span :style="{ textDecoration: i.status === 'bought' ? 'line-through' : undefined }">{{ i.raw_text }}</span>
        <span v-if="i.qty !== 1" class="muted">× {{ i.qty }} {{ i.unit || '' }}</span>
      </div>
    </div>
    <div v-if="candidates.length" class="card" style="margin-bottom: 16px; border-color: var(--warning)">
      <h3 style="margin-top: 0">Ймовірно куплено — підтвердіть</h3>
      <div
        v-for="i in candidates"
        :key="i.id"
        style="display: flex; gap: 8px; align-items: center; padding: 6px 0; border-bottom: 1px solid var(--border)"
      >
        <span style="flex: 1">
          {{ i.raw_text }}
          <span v-if="i.qty && i.qty !== 1" class="muted">× {{ fmtQty(i.qty) }}</span>
          <span v-if="i.note" class="muted"> · {{ i.note }}</span>
        </span>
        <span class="muted" style="font-size: 12px">{{ fmtDate(i.bought_at) }}</span>
        <button class="btn" :disabled="busy" @click="confirm(i)">Так</button>
        <button class="btn danger" :disabled="busy" @click="reject(i)">Ні</button>
      </div>
    </div>
    <div v-if="matchCandidates.length" class="card" style="margin-bottom: 16px; border-color: var(--warning)">
      <h3 style="margin-top: 0">Требует проверки ({{ matchCandidates.length }})</h3>
      <div
        v-for="candidate in matchCandidates"
        :key="candidate.id"
        style="display: flex; gap: 8px; align-items: center; padding: 6px 0; border-bottom: 1px solid var(--border)"
      >
        <span style="flex: 1">
          {{ candidate.list_name }} → {{ candidate.receipt_item_name }}
          <span class="muted" style="display: block; font-size: 12px">{{ candidate.explanation }}</span>
        </span>
        <button class="btn" :disabled="busy" @click="resolveCandidate(candidate, 'confirm')">Куплено</button>
        <button class="btn danger" :disabled="busy" @click="resolveCandidate(candidate, 'reject')">Нет</button>
      </div>
    </div>

    <div class="card" style="margin-bottom: 16px">
      <h3 style="margin-top: 0">До покупки ({{ pending.length }})</h3>
      <ShoppingBulkBar
        v-if="selectedPendingIds.size"
        mode="pending"
        :count="selectedPendingIds.size"
        :busy="busy"
        :stores="stores"
        :categories="categories"
        @status="bulkStatus('pending')"
        @assign="bulkAssign('pending', $event)"
        @remove="bulkRemove('pending')"
        @clear="clearSelection('pending')"
      />
      <div class="toolbar" style="margin: 0 0 6px">
        <label class="muted" style="font-size: 12px">Группировка</label>
        <select v-model="groupMode" title="Как разбить список на группы" style="width: 185px">
          <option value="store_class">По типу магазина</option>
          <option value="store">По магазину</option>
          <option value="category">По категории</option>
          <option value="none">Без группировки</option>
        </select>
        <label class="muted" style="font-size: 12px">Сортировка</label>
        <select v-model="sortMode" title="Порядок внутри группы" style="width: 160px">
          <option value="added_desc">Сначала новые</option>
          <option value="added_asc">Сначала старые</option>
          <option value="name">По названию</option>
        </select>
      </div>
      <p v-if="!pending.length" class="muted">Список порожній.</p>
      <template v-for="[title, rows] in pendingGroups" :key="title || '_'">
        <div v-if="groupMode !== 'none'" class="group-title">
          {{ title }} <span class="group-count">{{ rows.length }}</span>
        </div>
        <div v-for="i in rows" :key="i.id" class="item-row">
          <input type="checkbox" :checked="selectedPendingIds.has(i.id)" @change="togglePending(i)" />

          <span class="cell name">
            <input
              v-if="isEditing(i, 'raw_text')"
              v-autofocus
              :value="i.raw_text"
              type="text"
              @change="onEditInput(i, 'raw_text', $event)"
              @blur="stopEdit"
              @keydown.enter.prevent="onEditInput(i, 'raw_text', $event)"
              @keydown.esc="stopEdit"
            />
            <span v-else class="editable" title="Нажмите, чтобы переименовать" @click="startEdit(i, 'raw_text')">
              {{ i.raw_text }}<span v-if="i.note"> ({{ i.note }})</span>
            </span>
          </span>

          <span class="cell qty">
            <input
              v-if="isEditing(i, 'qty')"
              v-autofocus
              :value="i.qty"
              type="number"
              min="0"
              step="0.001"
              @change="onEditInput(i, 'qty', $event)"
              @blur="stopEdit"
              @keydown.enter.prevent="onEditInput(i, 'qty', $event)"
              @keydown.esc="stopEdit"
            />
            <span v-else class="editable chip" title="Количество" @click="startEdit(i, 'qty')">
              × {{ fmtQty(i.qty) }}{{ i.unit ? ' ' + i.unit : '' }}
            </span>
          </span>

          <span class="cell cat">
            <select
              v-if="isEditing(i, 'category_id')"
              v-autofocus
              :value="i.category_id ?? ''"
              @change="onEditInput(i, 'category_id', $event)"
              @blur="stopEdit"
            >
              <option value="">Без категории</option>
              <option v-for="category in categories" :key="category.id" :value="category.id">{{ category.name }}</option>
            </select>
            <span
              v-else
              class="editable chip"
              :class="{ empty: !i.category_name }"
              :title="i.category_group ? i.category_group + ' · ' + i.category_name : 'Категория'"
              @click="startEdit(i, 'category_id')"
            >
              {{ i.category_name || 'категория' }}
            </span>
          </span>

          <span class="cell store">
            <select
              v-if="isEditing(i, 'target_store_code')"
              v-autofocus
              :value="i.target_store_code ?? ''"
              @change="onEditInput(i, 'target_store_code', $event)"
              @blur="stopEdit"
            >
              <option value="">Все магазины</option>
              <option v-for="store in stores" :key="store.code" :value="store.code">{{ store.name }}</option>
            </select>
            <span
              v-else
              class="editable chip"
              :class="{ empty: !i.target_store_code }"
              title="Магазин"
              @click="startEdit(i, 'target_store_code')"
            >
              {{ storeName(i.target_store_code) || 'магазин' }}
            </span>
          </span>

          <span class="cell tag">
            <input
              v-if="isEditing(i, 'tag')"
              v-autofocus
              :value="i.tag ?? ''"
              type="text"
              placeholder="тег"
              @change="onEditInput(i, 'tag', $event)"
              @blur="stopEdit"
              @keydown.enter.prevent="onEditInput(i, 'tag', $event)"
              @keydown.esc="stopEdit"
            />
            <span v-else class="editable chip" :class="{ empty: !i.tag }" title="Тег" @click="startEdit(i, 'tag')">
              {{ i.tag ? '#' + i.tag : 'тег' }}
            </span>
          </span>

          <span class="cell note">
            <input
              v-if="isEditing(i, 'note')"
              v-autofocus
              :value="i.note ?? ''"
              type="text"
              placeholder="примітка"
              @change="onEditInput(i, 'note', $event)"
              @blur="stopEdit"
              @keydown.enter.prevent="onEditInput(i, 'note', $event)"
              @keydown.esc="stopEdit"
            />
            <span
              v-else
              class="editable note-text"
              :class="{ empty: !i.note }"
              :title="`Добавлено ${fmtDate(i.added_at)} · ${methodLabel(i.added_method)}`"
              @click="startEdit(i, 'note')"
            >
              {{ i.note || 'примітка' }}
            </span>
          </span>

          <button class="row-remove" title="Удалить позицию" @click="remove(i)">✕</button>
        </div>
      </template>
    </div>

    <div v-if="bought.length" class="card" style="margin-bottom: 16px">
      <h3 style="margin-top: 0">Куплено ({{ bought.length }})</h3>
      <ShoppingBulkBar
        v-if="selectedBoughtIds.size"
        mode="bought"
        :count="selectedBoughtIds.size"
        :busy="busy"
        :stores="stores"
        :categories="categories"
        @status="bulkStatus('bought')"
        @assign="bulkAssign('bought', $event)"
        @remove="bulkRemove('bought')"
        @clear="clearSelection('bought')"
      />
      <div
        v-for="i in bought"
        :key="i.id"
        style="display: flex; gap: 8px; align-items: center; padding: 6px 0; border-bottom: 1px solid var(--border)"
      >
        <input type="checkbox" :checked="selectedBoughtIds.has(i.id)" @change="toggleBought(i)" />
          <span style="flex: 1" class="muted">
          {{ i.raw_text }}
          <span v-if="i.qty && i.qty !== 1">× {{ fmtQty(i.qty) }}</span>
          <span v-if="i.note"> · {{ i.note }}</span>
          <span style="display: block; font-size: 12px">
            🏪 {{ i.bought_store_name || 'магазин не указан' }} ·
            🕒 {{ fmtDate(i.bought_purchased_at || i.bought_at) }} ·
            🧾 <a v-if="i.bought_receipt_url" :href="i.bought_receipt_url" target="_blank" rel="noopener">чек</a><template v-else>{{ i.bought_receipt_id ? `чек ${i.bought_receipt_id.slice(0, 8)}` : 'чек не привязан' }}</template>
          </span>
          </span>
          <span class="muted" style="font-size: 12px">
            Добавлено {{ fmtDate(i.added_at) }} · {{ methodLabel(i.added_method) }}<span v-if="i.added_by"> · {{ i.added_by }}</span>
          </span>
        <button class="row-remove" title="Удалить позицию" @click="remove(i)">✕</button>
      </div>
    </div>
    </template>

    <template v-if="activeTab === 'archive'">
      <div v-if="!archiveRows.length" class="card muted">Архив покупок пока пуст.</div>
      <div v-for="(block, index) in archiveRows" :key="block.receipt_id ?? `no-receipt-${index}`" class="card" style="margin-bottom: 16px">
        <div style="display: flex; justify-content: space-between; align-items: baseline; gap: 12px">
          <h3 style="margin-top: 0">{{ block.shop_name || 'Покупка без чека' }}</h3>
          <span class="muted">{{ fmtDate(block.purchased_at) }}</span>
        </div>
        <div class="muted" style="font-size: 12px; margin-bottom: 8px">
          <span v-if="block.receipt_id">Чек: {{ block.receipt_id }}</span>
          <span v-if="block.total_amount != null"> · {{ fmtMoney(block.total_amount) }}</span>
          <a v-if="block.receipt_url" :href="block.receipt_url" target="_blank" rel="noreferrer"> · Открыть чек</a>
        </div>
        <div v-for="item in block.items" :key="item.shopping_item_id" style="display: flex; gap: 8px; padding: 6px 0; border-bottom: 1px solid var(--border)">
          <span style="flex: 1">{{ item.list_name }} <span v-if="item.qty !== 1" class="muted">× {{ fmtQty(item.qty) }} {{ item.unit || '' }}</span></span>
          <span class="muted">{{ item.receipt_item_name || 'без строки чека' }}</span>
          <span v-if="item.receipt_item_total != null">{{ fmtMoney(item.receipt_item_total) }}</span>
        </div>
      </div>
    </template>

    <template v-if="activeTab === 'recommendations'">

    <div v-if="routines.length" class="card" style="margin-bottom: 16px; border-color: var(--warning)">
      <h3 style="margin-top: 0">Можливо, вже час купити</h3>
      <div v-for="r in routines" :key="r.normalized_name" style="display: flex; gap: 8px; align-items: center; padding: 7px 0; border-bottom: 1px solid var(--border)">
        <span style="flex: 1">
          Ви регулярно берете <strong>{{ r.display_name }}</strong> приблизно раз на {{ Math.round(r.interval_days) }} днів;
          медіанний обсяг: {{ r.median_qty }} {{ r.unit || '' }}.
          <span class="muted">Вже прострочено на {{ r.days_overdue }} дн.</span>
        </span>
        <button class="btn" :disabled="busy" @click="routineAction(r, 'add')">Додати</button>
        <button class="btn secondary" :disabled="busy" @click="routineAction(r, 'postpone')">Пізніше</button>
        <button class="btn danger" :disabled="busy" @click="routineAction(r, 'ignore')">Ігнорувати</button>
      </div>
    </div>

    <div class="card">
      <div style="display: flex; justify-content: space-between; align-items: baseline; gap: 12px">
        <h3 style="margin-top: 0">Де вигідніше купити</h3>
        <button class="btn secondary" :disabled="liveLoading || !pending.length" @click="checkLive">
          {{ liveLoading ? 'Перевіряємо…' : 'Перевірити наживо' }}
        </button>
      </div>
      <p class="muted" style="margin-top: -4px; font-size: 12px">
        За історією чеків. «Наживо» опитує каталоги магазинів зараз; вільні фрази
        розкладаються під кожну мережу через AI і кешуються.
      </p>
      <p v-if="!suggestions.length" class="muted">
        Замало історії покупок для позицій зі списку.
      </p>
      <table v-else>
        <thead>
          <tr>
            <th>Мережа</th>
            <th>Покриття</th>
            <th>Орієнтовний кошик</th>
            <th>Остання покупка</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in suggestions" :key="s.store_code">
            <td>{{ s.store_name }}</td>
            <td>{{ s.covered_items }} / {{ s.total_items }}</td>
            <td>{{ fmtMoney(s.approx_basket) }}</td>
            <td class="muted">{{ fmtDate(s.last_purchase_at) }}</td>
          </tr>
        </tbody>
      </table>

      <template v-if="liveSuggestions">
        <h4 style="margin: 16px 0 4px">Наживо</h4>
        <p v-if="!liveSuggestions.length" class="muted">
          Жоден магазин з живим каталогом не має позицій зі списку.
        </p>
        <div v-for="s in liveSuggestions" :key="s.store_code" style="margin-bottom: 12px">
          <div style="display: flex; justify-content: space-between">
            <strong>{{ s.store_name }}</strong>
            <span>{{ s.covered_items }} / {{ s.total_items }} · {{ fmtMoney(s.approx_basket) }}</span>
          </div>
          <table>
            <tbody>
              <tr v-for="it in s.items" :key="it.raw_text">
                <td>{{ it.raw_text }}</td>
                <td class="muted" style="font-size: 12px">
                  {{ it.query }}<span v-if="it.query_source === 'ai'"> · AI</span>
                </td>
                <td class="muted">{{ it.matched_name ?? '—' }}</td>
                <td>{{ it.last_price != null ? fmtMoney(it.last_price) : '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
    </div>
    </template>
  </template>
</template>

<style scoped>
/* One line per item: name grows, everything else is a compact chip that turns
   into its editor on click. Only a hairline separates the rows. */
.item-row {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 4px 0;
  border-bottom: 1px solid var(--border);
  font-size: 14px;
}
.item-row:last-child {
  border-bottom: none;
}
.group-count {
  opacity: 0.6;
}
.group-title {
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--muted, #8a93a6);
  margin: 14px 0 2px;
}
.cell {
  display: flex;
  align-items: center;
  min-width: 0;
}
.cell input,
.cell select {
  width: 100%;
  min-width: 90px;
}
.cell.name {
  flex: 1 1 auto;
}
.cell.qty { flex: 0 0 74px; }
.cell.cat { flex: 0 0 165px; }
.cell.store { flex: 0 0 135px; }
.cell.tag { flex: 0 0 100px; }
.cell.note { flex: 0 0 150px; }

.editable {
  cursor: text;
  border-radius: 6px;
  padding: 2px 6px;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.editable:hover {
  background: color-mix(in srgb, currentColor 10%, transparent);
}
.chip {
  font-size: 12px;
  color: var(--muted, #8a93a6);
}
.note-text {
  font-size: 12px;
  color: var(--muted, #8a93a6);
}
.empty {
  opacity: 0.45;
  font-style: italic;
}
.row-remove {
  flex: 0 0 auto;
  background: none;
  border: none;
  color: var(--muted, #8a93a6);
  cursor: pointer;
  padding: 2px 6px;
  border-radius: 6px;
  font-size: 13px;
}
.row-remove:hover {
  color: var(--danger);
  background: color-mix(in srgb, var(--danger) 15%, transparent);
}

/* Narrow screens: the name takes its own line, the chips wrap under it. */
@media (max-width: 720px) {
  .item-row {
    flex-wrap: wrap;
    padding: 8px 0;
  }
  .cell.name {
    flex: 1 1 calc(100% - 60px);
    font-size: 15px;
  }
  .cell.qty,
  .cell.cat,
  .cell.store,
  .cell.tag,
  .cell.note {
    flex: 0 0 auto;
  }
}
</style>
