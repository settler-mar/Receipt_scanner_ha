<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '@/lib/api'
import type { LiveProduct, ProductPrice } from '@/types/api'
import { fmtDate as fmtDateOnly } from '@/lib/datetime'
import ZoomableImage from '@/components/ZoomableImage.vue'

const query = ref('')
const tab = ref<'live' | 'history'>('live')

// Stores whose adapter can do a live catalog search *and* that we have an
// account for — the set the /live-search endpoint would actually query.
const liveStores = ref<{ code: string; name: string }[]>([])
const STORAGE_KEY = 'products.liveSearchStores'
// null = "all" (send no filter); otherwise an explicit allow-list of codes.
const selectedStores = ref<Set<string> | null>(loadSelection())

function loadSelection(): Set<string> | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return null
    return new Set(JSON.parse(raw) as string[])
  } catch {
    return null
  }
}

onMounted(async () => {
  const [stores, accounts] = await Promise.all([api.stores.list(), api.accounts.list()])
  const withAccount = new Set(accounts.map((a) => a.store.code))
  liveStores.value = stores
    .filter((s) => s.capabilities.catalog_search && withAccount.has(s.code))
    .map((s) => ({ code: s.code, name: s.name }))
})

function isSelected(code: string) {
  return selectedStores.value === null || selectedStores.value.has(code)
}

function toggleStore(code: string) {
  // Materialise "all" into a concrete set the first time the user narrows it.
  const set = new Set(selectedStores.value ?? liveStores.value.map((s) => s.code))
  if (set.has(code)) set.delete(code)
  else set.add(code)
  const allSelected = liveStores.value.every((s) => set.has(s.code))
  selectedStores.value = allSelected ? null : set
  persistSelection()
  if (query.value.trim().length >= 2) runLiveSearch(query.value.trim())
}

function persistSelection() {
  if (selectedStores.value === null) localStorage.removeItem(STORAGE_KEY)
  else localStorage.setItem(STORAGE_KEY, JSON.stringify([...selectedStores.value]))
}

const activeStoreCodes = computed(() =>
  selectedStores.value === null ? undefined : [...selectedStores.value],
)

const results = ref<ProductPrice[]>([])
const loading = ref(false)
const searched = ref(false)

const liveResults = ref<LiveProduct[]>([])
const liveLoading = ref(false)
const liveSearched = ref(false)

let debounceHandle: ReturnType<typeof setTimeout> | undefined
watch(query, (q) => {
  clearTimeout(debounceHandle)
  const trimmed = q.trim()
  if (trimmed.length < 2) {
    results.value = []
    liveResults.value = []
    searched.value = false
    liveSearched.value = false
    return
  }
  debounceHandle = setTimeout(() => {
    runHistorySearch(trimmed)
    runLiveSearch(trimmed)
  }, 300)
})

async function runHistorySearch(q: string) {
  loading.value = true
  try {
    results.value = await api.products.search(q)
  } finally {
    loading.value = false
    searched.value = true
  }
}

async function runLiveSearch(q: string) {
  if (activeStoreCodes.value && activeStoreCodes.value.length === 0) {
    liveResults.value = []
    liveSearched.value = true
    return
  }
  liveLoading.value = true
  try {
    liveResults.value = await api.products.liveSearch(q, activeStoreCodes.value)
  } finally {
    liveLoading.value = false
    liveSearched.value = true
  }
}

// Backend already sorts by (name, last_price) — group in display order so
// each product name becomes its own card with stores sorted cheapest-first.
const groups = computed(() => {
  const map = new Map<string, ProductPrice[]>()
  for (const row of results.value) {
    const list = map.get(row.name) ?? []
    list.push(row)
    map.set(row.name, list)
  }
  return [...map.entries()]
})

// Live results already come back sorted by price across all stores together —
// group by store just for a legible layout, keeping that overall price order
// within each store's list.
const liveGroups = computed(() => {
  const map = new Map<string, LiveProduct[]>()
  for (const row of liveResults.value) {
    const list = map.get(row.store_code) ?? []
    list.push(row)
    map.set(row.store_code, list)
  }
  return [...map.entries()]
})

function fmtMoney(v: number) {
  return new Intl.NumberFormat('uk-UA', { style: 'currency', currency: 'UAH' }).format(v)
}
function fmtDate(v: string) {
  return fmtDateOnly(v)
}
</script>

<template>
  <h2 style="margin-top: 0">Товари</h2>
  <p class="muted" style="margin-top: -8px">
    Пошук товарів — або "живцем" прямо в застосунках магазинів (актуальна ціна й наявність зараз),
    або по історії власних чеків (де і коли купували раніше). Спільного каталогу товарів між
    мережами немає, тож обидва пошуки — текстові, за назвою.
  </p>

  <div class="toolbar">
    <input
      v-model="query"
      type="text"
      placeholder="Почніть вводити назву товару, напр. «молоко»…"
      style="flex: 1; min-width: 260px"
      autofocus
    />
  </div>

  <div class="toolbar" style="gap: 8px">
    <button :class="{ secondary: tab !== 'live' }" @click="tab = 'live'">
      Живий пошук{{ liveResults.length ? ` (${liveResults.length})` : '' }}
    </button>
    <button :class="{ secondary: tab !== 'history' }" @click="tab = 'history'">
      Історія покупок{{ results.length ? ` (${results.length})` : '' }}
    </button>
  </div>

  <template v-if="tab === 'live'">
    <div v-if="liveStores.length" class="toolbar" style="gap: 8px; flex-wrap: wrap; align-items: center">
      <span class="muted" style="font-size: 13px">Шукати в магазинах:</span>
      <label
        v-for="s in liveStores"
        :key="s.code"
        style="display: inline-flex; align-items: center; gap: 4px; font-size: 13px; cursor: pointer"
      >
        <input type="checkbox" :checked="isSelected(s.code)" @change="toggleStore(s.code)" />
        {{ s.name }}
      </label>
    </div>

    <div v-if="liveLoading" class="muted">Опитуємо застосунки магазинів…</div>
    <div v-else-if="liveSearched && activeStoreCodes && !activeStoreCodes.length" class="muted">
      Оберіть хоча б один магазин для пошуку.
    </div>
    <div v-else-if="liveSearched && !liveGroups.length" class="muted">
      Нічого не знайдено за «{{ query }}» — або жоден магазин не підтримує живий пошук ще,
      або запит не збігся з жодним товаром.
    </div>
    <div v-else-if="!liveSearched" class="muted">Введіть щонайменше 2 символи</div>

    <div v-for="[storeCode, rows] in liveGroups" :key="storeCode" class="card" style="margin-bottom: 16px">
      <h3 style="margin-top: 0">{{ rows[0].store_name }}</h3>
      <div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 12px">
        <a
          v-for="p in rows"
          :key="p.product_url ?? p.name"
          :href="p.product_url ?? undefined"
          target="_blank"
          rel="noopener noreferrer"
          style="color: inherit; display: block; border: 1px solid var(--border); border-radius: var(--radius); padding: 8px; text-decoration: none"
        >
          <ZoomableImage
            v-if="p.image_url"
            :src="p.image_url"
            :alt="p.name"
            style="width: 100%; aspect-ratio: 1; object-fit: contain; background: var(--bg); border-radius: 6px; margin-bottom: 6px"
            @click.stop.prevent
          />
          <div style="font-size: 13px; line-height: 1.3; min-height: 2.6em">{{ p.name }}</div>
          <div style="display: flex; justify-content: space-between; align-items: baseline; margin-top: 4px">
            <strong>{{ fmtMoney(p.price) }}</strong>
            <span v-if="p.in_stock === false" class="muted" style="color: var(--danger)">немає</span>
          </div>
        </a>
      </div>
    </div>
  </template>

  <template v-else>
    <div v-if="loading" class="muted">Пошук…</div>
    <div v-else-if="searched && !groups.length" class="muted">Нічого не знайдено за «{{ query }}»</div>
    <div v-else-if="!searched" class="muted">Введіть щонайменше 2 символи</div>

    <div v-for="[name, rows] in groups" :key="name" class="card" style="margin-bottom: 16px">
      <h3 style="margin-top: 0">{{ name }}</h3>
      <table>
        <thead>
          <tr>
            <th>Магазин</th>
            <th>Остання ціна</th>
            <th>Востаннє купували</th>
            <th>Мін</th>
            <th>Макс</th>
            <th>Середня</th>
            <th>Разів куплено</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in rows"
            :key="`${row.store_code}:${row.unit}`"
            :style="row.last_price === Math.min(...rows.map((r) => r.last_price)) ? { color: 'var(--success)' } : {}"
          >
            <td>{{ row.store_name }}</td>
            <td>
              {{ fmtMoney(row.last_price) }}<span v-if="row.unit" class="muted"> / {{ row.unit }}</span>
            </td>
            <td class="muted">{{ fmtDate(row.last_purchased_at) }}</td>
            <td class="muted">{{ fmtMoney(row.min_price) }}</td>
            <td class="muted">{{ fmtMoney(row.max_price) }}</td>
            <td class="muted">{{ fmtMoney(row.avg_price) }}</td>
            <td class="muted">{{ row.times_bought }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </template>
</template>
