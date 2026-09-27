<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { api } from '@/lib/api'
import { fmtMoney, fmtMoneyFull, rangeParams } from '@/lib/dashboardFmt'
import { storeFallbackIcon, storeLogoUrl } from '@/lib/storeIcons'
import { useWidgetData } from '@/composables/useWidgetData'
import DiscountToggle from '@/components/DiscountToggle.vue'
import type { Category, DashboardRangeKey, StoreCategoryItem, StoreCategoryRow } from '@/types/api'

const props = withDefaults(
  defineProps<{ range: DashboardRangeKey; includeDiscount?: boolean }>(),
  { includeDiscount: true },
)
const emit = defineEmits<{ 'update:includeDiscount': [boolean] }>()

const { data, status, reload } = useWidgetData(
  () => api.dashboard.storeCategory(rangeParams(props.range, props.includeDiscount)),
  () => [props.range, props.includeDiscount],
)

const logoSrc = (code: string): string | undefined => storeLogoUrl(code) ?? undefined

// full taxonomy for the per-item category picker in the drill-down
const categories = ref<Category[]>([])
const categoryOptions = computed(() =>
  [...categories.value]
    .sort((a, b) => a.path.localeCompare(b.path))
    .map((c) => ({ id: c.id, label: `${'— '.repeat(c.path.split('/').length - 1)}${c.name}` })),
)

// --- persisted filter: which stores / root categories to drop from the table ---
const FILTER_KEY = 'store-category.filter'
interface StoreCatFilter {
  hidden_stores?: string[]
  hidden_roots?: string[]
}
const hiddenStores = ref<Set<string>>(new Set())
const hiddenRoots = ref<Set<string>>(new Set())
const filterOpen = ref(false)

// focus selection (also cleared when its target gets filtered out)
const selCatPath = ref<string | null>(null)
const selStore = ref<string | null>(null)

onMounted(async () => {
  try {
    const { value } = await api.settings.get<StoreCatFilter>(FILTER_KEY)
    hiddenStores.value = new Set(value?.hidden_stores ?? [])
    hiddenRoots.value = new Set(value?.hidden_roots ?? [])
  } catch {
    /* no saved filter yet */
  }
  categories.value = await api.categories.list().catch(() => [])
})
function saveFilter() {
  void api.settings.set<StoreCatFilter>(FILTER_KEY, {
    hidden_stores: [...hiddenStores.value],
    hidden_roots: [...hiddenRoots.value],
  })
}
function toggleStore(code: string) {
  hiddenStores.value.has(code) ? hiddenStores.value.delete(code) : hiddenStores.value.add(code)
  hiddenStores.value = new Set(hiddenStores.value)
  if (selStore.value && hiddenStores.value.has(selStore.value)) selStore.value = null
  saveFilter()
}
function toggleRoot(key: string) {
  hiddenRoots.value.has(key) ? hiddenRoots.value.delete(key) : hiddenRoots.value.add(key)
  hiddenRoots.value = new Set(hiddenRoots.value)
  saveFilter()
}

const depth = (c: StoreCategoryRow) => (c.path === '' ? 0 : c.path.split('/').length - 1)
const parentPath = (c: StoreCategoryRow) => c.path.split('/').slice(0, -1).join('/')
const rootKey = (c: StoreCategoryRow) => (c.path === '' ? 'uncat' : c.path.split('/')[0] ?? c.path)

const allStores = computed(() => data.value?.stores ?? [])
const stores = computed(() => allStores.value.filter((s) => !hiddenStores.value.has(s.store_code)))
const everyRow = computed(() => (data.value?.categories ?? []).filter((c) => c.total > 0))
// roots offered in the filter checklist
const rootOptions = computed(() =>
  everyRow.value
    .filter((c) => depth(c) === 0)
    .map((c) => ({ key: rootKey(c), name: c.name })),
)
const allRows = computed(() => everyRow.value.filter((c) => !hiddenRoots.value.has(rootKey(c))))

// cell lookup: path -> (store_code -> total)
const cellMap = computed(() => {
  const m = new Map<string, Record<string, number>>()
  for (const r of everyRow.value) {
    m.set(r.path || 'uncat', Object.fromEntries(r.per_store.map((c) => [c.store_code, c.total])))
  }
  return m
})
function cell(path: string, code: string): number {
  return cellMap.value.get(path || 'uncat')?.[code] ?? 0
}
// row total across the *visible* stores only
function rowTotal(c: StoreCategoryRow): number {
  return stores.value.reduce((sum, s) => sum + cell(c.path, s.store_code), 0)
}
function storeColTotal(code: string): number {
  return allRows.value
    .filter((c) => depth(c) === 0)
    .reduce((sum, c) => sum + cell(c.path, code), 0)
}

function hasChildren(c: StoreCategoryRow) {
  return c.path !== '' && allRows.value.some((o) => o.path.startsWith(c.path + '/') && depth(o) === depth(c) + 1)
}

const expanded = ref<Set<string>>(new Set())
function toggle(c: StoreCategoryRow) {
  if (!hasChildren(c)) return
  const next = new Set(expanded.value)
  next.has(c.path) ? next.delete(c.path) : next.add(c.path)
  expanded.value = next
}

// Hierarchy-ordered visible rows: expanded parent immediately followed by its children.
const visibleRows = computed(() => {
  const rows = allRows.value
  const childrenOf = (path: string, d: number) =>
    rows.filter((c) => c.path !== '' && depth(c) === d && parentPath(c) === path).sort((a, b) => b.total - a.total)
  const out: StoreCategoryRow[] = []
  const walk = (path: string, d: number) => {
    for (const c of childrenOf(path, d)) {
      out.push(c)
      if (expanded.value.has(c.path)) walk(c.path, d + 1)
    }
  }
  for (const root of rows.filter((c) => depth(c) === 0).sort((a, b) => b.total - a.total)) {
    out.push(root)
    if (root.path !== '' && expanded.value.has(root.path)) walk(root.path, 1)
  }
  return out
})

// --- Focus: pick a category or a store to see its one-dimensional breakdown ---
const selCat = computed(() => visibleRows.value.find((r) => r.path === selCatPath.value) ?? null)

function pickCat(c: StoreCategoryRow) {
  selCatPath.value = selCatPath.value === c.path ? null : c.path
}
function pickStore(code: string) {
  selStore.value = selStore.value === code ? null : code
}

// category selected -> distribution across the *visible* stores
const catAcrossStores = computed(() => {
  const c = selCat.value
  if (!c) return []
  return c.per_store
    .filter((pc) => !hiddenStores.value.has(pc.store_code))
    .sort((a, b) => b.total - a.total)
})
const selCatTotal = computed(() => (selCat.value ? rowTotal(selCat.value) : 0))
// store selected -> its category breakdown (top-level rows, that store's column)
const storeCategories = computed(() => {
  if (!selStore.value) return []
  return allRows.value
    .filter((r) => depth(r) === 0)
    .map((r) => ({ row: r, total: cell(r.path, selStore.value as string) }))
    .filter((x) => x.total > 0)
    .sort((a, b) => b.total - a.total)
})

const storeName = (code: string) => stores.value.find((s) => s.store_code === code)?.store_name ?? code
function pct(part: number, whole: number) {
  return whole > 0 ? Math.round((part / whole) * 100) : 0
}

// --- Drill-down: the receipt lines behind one store×category cell ------------
const drill = ref<{ store: string; row: StoreCategoryRow } | null>(null)
const drillItems = ref<StoreCategoryItem[]>([])
const drillStatus = ref<'loading' | 'ready' | 'error'>('loading')
const drillSaving = ref<string | null>(null)

async function loadDrillItems() {
  const d = drill.value
  if (!d) return
  drillStatus.value = 'loading'
  try {
    drillItems.value = await api.dashboard.storeCategoryItems({
      store_code: d.store,
      category_id: d.row.category_id ?? 'none',
      ...rangeParams(props.range, props.includeDiscount),
    })
    drillStatus.value = 'ready'
  } catch {
    drillStatus.value = 'error'
  }
}

function openCell(row: StoreCategoryRow, store: string) {
  if (!cell(row.path, store)) return
  drill.value = { store, row }
  drillItems.value = []
  void loadDrillItems()
}

async function changeItemCategory(item: StoreCategoryItem, newId: string) {
  drillSaving.value = item.item_id
  try {
    await api.receipts.updateItem(item.receipt_id, item.item_id, { category_id: newId || null })
    await Promise.all([loadDrillItems(), reload()])
  } finally {
    drillSaving.value = null
  }
}

const fmtDay = (iso: string) => new Date(iso).toLocaleDateString('uk-UA', { day: '2-digit', month: '2-digit', year: '2-digit' })
const fmtQty = (q: number) => (Number.isInteger(q) ? String(q) : q.toFixed(3).replace(/\.?0+$/, ''))
</script>

<template>
  <div class="card">
    <div class="toolbar" style="margin-bottom: 8px; flex-wrap: nowrap">
      <h3 style="margin: 0; flex: 1">Магазини × категорії</h3>
      <DiscountToggle
        :model-value="includeDiscount"
        @update:model-value="emit('update:includeDiscount', $event)"
      />
      <button
        v-if="status === 'ready'"
        class="secondary"
        :class="{ active: filterOpen }"
        style="padding: 2px 10px"
        @click="filterOpen = !filterOpen"
      >
        ⚙ Фільтр<span v-if="hiddenStores.size + hiddenRoots.size">
          ({{ hiddenStores.size + hiddenRoots.size }})</span>
      </button>
    </div>

    <div v-if="filterOpen && status === 'ready'" class="card" style="background: var(--bg); margin-bottom: 12px">
      <div class="sc-filter-cols">
        <div>
          <strong style="font-size: 13px">Магазини</strong>
          <label v-for="s in allStores" :key="s.store_code" class="sc-filter-item">
            <input
              type="checkbox"
              :checked="!hiddenStores.has(s.store_code)"
              @change="toggleStore(s.store_code)"
            />
            <span class="sc-logo">
              <img v-if="logoSrc(s.store_code)" :src="logoSrc(s.store_code)" alt="" />
              <span v-else>{{ storeFallbackIcon(s.store_code) }}</span>
            </span>
            <span>{{ s.store_name }}</span>
          </label>
        </div>
        <div>
          <strong style="font-size: 13px">Категорії</strong>
          <label v-for="r in rootOptions" :key="r.key" class="sc-filter-item">
            <input
              type="checkbox"
              :checked="!hiddenRoots.has(r.key)"
              @change="toggleRoot(r.key)"
            />
            <span>{{ r.name }}</span>
          </label>
        </div>
      </div>
    </div>

    <p v-if="status === 'loading'" class="muted">Завантаження…</p>
    <p v-else-if="status === 'error'" class="muted">Не вдалося завантажити.</p>
    <template v-else-if="!visibleRows.length">
      <p class="muted">
        Немає даних. Категорії проставляються у фоні — див.
        <RouterLink to="/categories">Категорії</RouterLink>.
      </p>
    </template>
    <template v-else>
      <p class="muted" style="margin-top: 0; font-size: 13px">
        Клікніть категорію або магазин, щоб побачити розподіл. Клітинка = сума за період.
      </p>

      <!-- focus panel -->
      <div v-if="selCat" class="card" style="background: var(--bg); margin-bottom: 12px">
        <div class="toolbar" style="flex-wrap: nowrap">
          <strong style="flex: 1">{{ selCat.name }} — по магазинах</strong>
          <span class="muted">{{ fmtMoneyFull(selCatTotal) }}</span>
          <button class="secondary" style="padding: 2px 8px" @click="selCatPath = null">×</button>
        </div>
        <div v-for="c in catAcrossStores" :key="c.store_code" style="margin-top: 8px">
          <div class="toolbar" style="margin-bottom: 4px; flex-wrap: nowrap; font-size: 13px">
            <span style="flex: 1">{{ storeName(c.store_code) }}</span>
            <span class="muted">{{ pct(c.total, selCatTotal) }}%</span>
            <strong>{{ fmtMoney(c.total) }}</strong>
          </div>
          <div style="background: var(--bg); border-radius: 4px; overflow: hidden; height: 6px">
            <div :style="`width:${pct(c.total, selCatTotal)}%;height:100%;background:var(--accent)`"></div>
          </div>
        </div>
      </div>

      <div v-if="selStore" class="card" style="background: var(--bg); margin-bottom: 12px">
        <div class="toolbar" style="flex-wrap: nowrap">
          <strong style="flex: 1">{{ storeName(selStore) }} — по категоріях</strong>
          <button class="secondary" style="padding: 2px 8px" @click="selStore = null">×</button>
        </div>
        <div v-for="x in storeCategories" :key="x.row.path" style="margin-top: 8px">
          <div class="toolbar" style="margin-bottom: 4px; flex-wrap: nowrap; font-size: 13px">
            <span style="flex: 1">{{ x.row.name }}</span>
            <strong>{{ fmtMoney(x.total) }}</strong>
          </div>
          <div style="background: var(--bg); border-radius: 4px; overflow: hidden; height: 6px">
            <div
              :style="`width:${pct(x.total, storeCategories[0]?.total ?? 1)}%;height:100%;background:var(--accent)`"
            ></div>
          </div>
        </div>
      </div>

      <!-- cross-tab -->
      <div style="overflow-x: auto">
        <table class="sc-table">
          <thead>
            <tr>
              <th class="sc-cat">Категорія</th>
              <th
                v-for="s in stores"
                :key="s.store_code"
                class="sc-num sc-store-h"
                :class="{ 'sc-sel': selStore === s.store_code }"
                :title="`Розподіл для ${s.store_name}`"
                @click="pickStore(s.store_code)"
              >
                <span class="sc-logo">
                  <img v-if="logoSrc(s.store_code)" :src="logoSrc(s.store_code)" alt="" />
                  <span v-else>{{ storeFallbackIcon(s.store_code) }}</span>
                </span>
                {{ s.store_name }}
              </th>
              <th class="sc-num">Усього</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="c in visibleRows"
              :key="c.path || 'uncat'"
              :class="{ 'sc-sel-row': selCatPath === c.path }"
            >
              <td class="sc-cat">
                <span
                  :style="{ paddingLeft: depth(c) * 14 + 'px', cursor: 'pointer' }"
                  @click="pickCat(c)"
                >
                  <span
                    v-if="hasChildren(c)"
                    class="muted"
                    style="cursor: pointer"
                    @click.stop="toggle(c)"
                  >{{ expanded.has(c.path) ? '▾' : '▸' }}</span>
                  {{ c.name }}
                </span>
              </td>
              <td
                v-for="s in stores"
                :key="s.store_code"
                class="sc-num"
                :class="{ 'sc-sel': selStore === s.store_code, 'sc-cell-link': cell(c.path, s.store_code) }"
                :title="cell(c.path, s.store_code) ? `${c.name} @ ${s.store_name} — показати товари` : ''"
                @click="openCell(c, s.store_code)"
              >
                <span v-if="cell(c.path, s.store_code)">{{ fmtMoney(cell(c.path, s.store_code)) }}</span>
                <span v-else class="muted">·</span>
              </td>
              <td class="sc-num"><strong>{{ fmtMoney(rowTotal(c)) }}</strong></td>
            </tr>
          </tbody>
          <tfoot>
            <tr>
              <td class="sc-cat muted">Усього по магазину</td>
              <td v-for="s in stores" :key="s.store_code" class="sc-num">
                <strong>{{ fmtMoney(storeColTotal(s.store_code)) }}</strong>
              </td>
              <td class="sc-num"></td>
            </tr>
          </tfoot>
        </table>
      </div>
    </template>

    <!-- drill-down: items behind a cell -->
    <div v-if="drill" class="modal-backdrop" @click.self="drill = null">
      <div class="modal" style="width: min(720px, 94vw); max-height: 85vh; overflow: auto">
        <div class="toolbar" style="flex-wrap: nowrap; margin-bottom: 4px">
          <span class="sc-logo">
            <img v-if="logoSrc(drill.store)" :src="logoSrc(drill.store)" alt="" />
            <span v-else>{{ storeFallbackIcon(drill.store) }}</span>
          </span>
          <strong style="flex: 1">{{ storeName(drill.store) }} · {{ drill.row.name }}</strong>
          <button class="secondary" style="padding: 2px 8px" @click="drill = null">×</button>
        </div>
        <p class="muted" style="margin-top: 0; font-size: 13px">
          Змініть категорію товару у випадному списку — вибір закріпиться за цією назвою.
        </p>

        <p v-if="drillStatus === 'loading'" class="muted">Завантаження…</p>
        <p v-else-if="drillStatus === 'error'" class="muted">Не вдалося завантажити.</p>
        <p v-else-if="!drillItems.length" class="muted">Немає позицій.</p>
        <table v-else class="sc-items">
          <thead>
            <tr>
              <th>Дата</th>
              <th>Товар</th>
              <th class="sc-num">К-ть</th>
              <th class="sc-num">Ціна</th>
              <th class="sc-num">Сума</th>
              <th>Категорія</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="it in drillItems" :key="it.item_id">
              <td class="muted" style="white-space: nowrap">{{ fmtDay(it.purchased_at) }}</td>
              <td>
                <RouterLink :to="`/receipts/${it.receipt_id}`" style="color: inherit">{{ it.name }}</RouterLink>
              </td>
              <td class="sc-num" style="white-space: nowrap">
                {{ fmtQty(it.quantity) }}<span v-if="it.unit" class="muted"> {{ it.unit }}</span>
              </td>
              <td class="sc-num muted" style="white-space: nowrap">
                {{ it.unit_price != null ? fmtMoney(it.unit_price) : '—' }}
              </td>
              <td class="sc-num" style="white-space: nowrap">
                {{ fmtMoney(it.net) }}<span v-if="it.discount" class="muted" :title="`знижка ${fmtMoney(it.discount)}`"> ▾</span>
              </td>
              <td>
                <select
                  :value="it.category_id ?? ''"
                  :disabled="drillSaving === it.item_id"
                  @change="changeItemCategory(it, ($event.target as HTMLSelectElement).value)"
                >
                  <option value="">— без категорії —</option>
                  <option v-for="o in categoryOptions" :key="o.id" :value="o.id">{{ o.label }}</option>
                </select>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<style scoped>
.sc-table {
  border-collapse: collapse;
  font-size: 13px;
  min-width: 100%;
}
.sc-table th,
.sc-table td {
  padding: 5px 10px;
  white-space: nowrap;
  border-bottom: 1px solid var(--border);
}
.sc-cat {
  text-align: left;
  position: sticky;
  left: 0;
  background: var(--bg-elevated);
}
.sc-num {
  text-align: right;
}
.sc-store-h {
  cursor: pointer;
  user-select: none;
}
.sc-store-h:hover {
  color: var(--accent);
}
.sc-sel,
.sc-sel-row td {
  background: var(--bg-hover);
}
.sc-table tfoot td {
  border-bottom: none;
  border-top: 2px solid var(--border);
}
.sc-filter-cols {
  display: flex;
  gap: 32px;
  flex-wrap: wrap;
}
.sc-filter-item {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  padding: 3px 0;
  cursor: pointer;
}
.sc-filter-item span:last-child {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sc-logo {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  flex: none;
  font-size: 13px;
  line-height: 1;
  vertical-align: middle;
}
.sc-logo img {
  max-width: 16px;
  max-height: 16px;
  object-fit: contain;
}
.sc-cell-link {
  cursor: pointer;
}
.sc-cell-link:hover {
  background: var(--bg-hover);
}
.sc-items {
  border-collapse: collapse;
  width: 100%;
  font-size: 13px;
}
.sc-items th,
.sc-items td {
  padding: 5px 8px;
  border-bottom: 1px solid var(--border);
  text-align: left;
}
.sc-items .sc-num {
  text-align: right;
}
.sc-items select {
  max-width: 200px;
}
</style>
