<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { api } from '@/lib/api'
import { storeFallbackIcon, storeLogoUrl } from '@/lib/storeIcons'
import { fmtDateTime, fmtDayTime } from '@/lib/datetime'
import { fmtMoneyFull, rangeDateFrom } from '@/lib/dashboardFmt'
import { useWidgetData } from '@/composables/useWidgetData'
import { useChartTooltip } from '@/composables/useChartTooltip'
import ChartTooltip from '@/components/ChartTooltip.vue'
import DiscountToggle from '@/components/DiscountToggle.vue'
import type {
  AccountFuelPrices,
  DashboardRangeKey,
  FuelPrice,
  FuelPriceHistory,
} from '@/types/api'

const props = withDefaults(
  defineProps<{ range: DashboardRangeKey; includeDiscount?: boolean }>(),
  { includeDiscount: true },
)
const emit = defineEmits<{ 'update:includeDiscount': [boolean] }>()

/** Chart/board price for one fuel: with the personal discount applied or without. */
function effectivePrice(price: number, discount: number | null | undefined): number {
  return props.includeDiscount ? price - (discount ?? 0) : price
}

// The comparison table isn't date-scoped; only the optional history chart looks
// back as far as the selected range.
const { data: fuelData, status } = useWidgetData<AccountFuelPrices[]>(() => api.dashboard.fuelPrices())
const fuelPrices = computed(() => fuelData.value ?? [])

function fmtDate(v: string | null) {
  return fmtDateTime(v)
}

// ---- Configurable fuel block ---------------------------------------------
// Persisted in the DB (app_settings key `dashboard.fuel_block`).
type FuelBlockConfig = {
  fuels: FuelGroupKey[] | null
  hidden_accounts: string[]
  hidden_chart_stores: string[]
}
type StoredFuelBlockConfig = Partial<FuelBlockConfig> & { hidden_groups?: FuelGroupKey[] }
const FUEL_BLOCK_KEY = 'dashboard.fuel_block'
const fuelConfig = ref<FuelBlockConfig>({ fuels: null, hidden_accounts: [], hidden_chart_stores: [] })
const fuelEditOpen = ref(false)

const selectedFuelKeys = computed<FuelGroupKey[]>(
  () => fuelConfig.value.fuels ?? FUEL_GROUPS.map((g) => g.key),
)

async function loadFuelConfig() {
  const { value } = await api.settings.get<StoredFuelBlockConfig>(FUEL_BLOCK_KEY)
  let fuels = value?.fuels ?? null
  if (fuels == null && value?.hidden_groups) {
    fuels = FUEL_GROUPS.map((g) => g.key).filter((k) => !value.hidden_groups!.includes(k))
  }
  fuelConfig.value = {
    fuels,
    hidden_accounts: value?.hidden_accounts ?? [],
    hidden_chart_stores: value?.hidden_chart_stores ?? [],
  }
}

async function saveFuelConfig() {
  await api.settings.set<FuelBlockConfig>(FUEL_BLOCK_KEY, fuelConfig.value)
}

function toggleInList(list: string[], value: string): string[] {
  return list.includes(value) ? list.filter((x) => x !== value) : [...list, value]
}

function toggleFuelGroup(key: FuelGroupKey) {
  fuelConfig.value.fuels = toggleInList(selectedFuelKeys.value, key) as FuelGroupKey[]
  saveFuelConfig()
}

function toggleFuelAccount(accountId: string) {
  fuelConfig.value.hidden_accounts = toggleInList(fuelConfig.value.hidden_accounts, accountId)
  saveFuelConfig()
}

const visibleFuelAccounts = computed(() =>
  fuelPrices.value.filter((acc) => !fuelConfig.value.hidden_accounts.includes(acc.account_id)),
)

onMounted(loadFuelConfig)

function discountedPrice(p: { price: number; discount: number | null }) {
  return p.discount ? p.price - p.discount : p.price
}

function priceTrend(p: FuelPrice): 'up' | 'down' | null {
  if (p.previous_price == null || p.previous_price === p.price) return null
  return p.price > p.previous_price ? 'up' : 'down'
}

function fmtFuelDelta(p: FuelPrice): string {
  const diff = Math.abs(p.price - (p.previous_price ?? p.price))
  return new Intl.NumberFormat('uk-UA', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(diff)
}

function fmtAllowance(acc: AccountFuelPrices): string {
  const n = new Intl.NumberFormat('uk-UA', { maximumFractionDigits: 1 }).format(acc.discount_allowance ?? 0)
  return `${n} ${acc.discount_allowance_unit ?? 'л'}`
}

function accountPerson(acc: AccountFuelPrices): string {
  const stripped = acc.account_label.replace(acc.store_name, '').replace(/^[\s·—-]+/, '').trim()
  return stripped || acc.account_label
}

const visibleAccountsPerStore = computed(() => {
  const counts: Record<string, number> = {}
  for (const acc of visibleFuelAccounts.value) counts[acc.store_code] = (counts[acc.store_code] ?? 0) + 1
  return counts
})
function showPersonFor(acc: AccountFuelPrices): boolean {
  return (visibleAccountsPerStore.value[acc.store_code] ?? 0) > 1
}

type FuelGroupKey = 'lpg' | 'a92' | 'a95' | 'a98' | 'diesel'

const FUEL_GROUPS: { key: FuelGroupKey; label: string }[] = [
  { key: 'a92', label: 'А-92' },
  { key: 'a95', label: 'А-95' },
  { key: 'a98', label: 'А-98 / А-100' },
  { key: 'diesel', label: 'Дизель' },
  { key: 'lpg', label: 'Газ (LPG)' },
]

function fuelGroup(name: string): FuelGroupKey | null {
  const normalized = name.toLocaleLowerCase('uk-UA').replace(/[\s_–—-]+/g, '')
  if (normalized.includes('lpg') || normalized.includes('газ') || normalized.includes('пропан')) return 'lpg'
  if (/дп|диз|diesel/i.test(normalized)) return 'diesel'
  if (/(^|\D)(98|100)(\D|$)/.test(name)) return 'a98'
  if (/(^|\D)95(\D|$)/.test(name)) return 'a95'
  if (/(^|\D)92(\D|$)/.test(name)) return 'a92'
  return null
}

const fuelComparisonRows = computed(() =>
  FUEL_GROUPS.filter((group) => selectedFuelKeys.value.includes(group.key))
    .map((group) => ({
      ...group,
      byAccount: Object.fromEntries(
        visibleFuelAccounts.value.map((account) => [
          account.account_id,
          account.prices
            .filter((price) => fuelGroup(price.name) === group.key)
            .sort((a, b) => discountedPrice(a) - discountedPrice(b)),
        ]),
      ),
    }))
    .filter((row) => Object.values(row.byAccount).some((prices) => (prices as FuelPrice[]).length)),
)

// ---- Fuel price history chart ------------------------------------------------
const SERIES_COLORS = ['#38bdf8', '#4ade80', '#fbbf24', '#a78bfa', '#fb923c', '#f87171', '#22d3ee', '#f472b6']
const fuelHistory = ref<FuelPriceHistory[]>([])
const fuelChartOpen = ref(false)
const fuelHistoryLoading = ref(false)

async function loadFuelHistory() {
  fuelHistoryLoading.value = true
  try {
    fuelHistory.value = await api.dashboard.fuelPriceHistory({ limit: 5000 })
  } finally {
    fuelHistoryLoading.value = false
  }
}

function toggleFuelChart() {
  fuelChartOpen.value = !fuelChartOpen.value
  if (fuelChartOpen.value && !fuelHistory.value.length) loadFuelHistory()
}

type ChartStore = { code: string; name: string; color: string }

const fuelChartStores = computed<ChartStore[]>(() => {
  const seen = new Map<string, ChartStore>()
  for (const acc of visibleFuelAccounts.value) {
    if (seen.has(acc.store_code)) continue
    seen.set(acc.store_code, {
      code: acc.store_code,
      name: acc.store_name,
      color: SERIES_COLORS[seen.size % SERIES_COLORS.length]!,
    })
  }
  return [...seen.values()]
})

const fuelChartHiddenStores = computed(() => new Set(fuelConfig.value.hidden_chart_stores))
function toggleChartStore(code: string) {
  fuelConfig.value.hidden_chart_stores = toggleInList(fuelConfig.value.hidden_chart_stores, code)
  saveFuelConfig()
}

const DAY_MS = 864e5
const dateFrom = computed(() => rangeDateFrom(props.range))
const fuelChartStartT = computed(() => (dateFrom.value ? new Date(dateFrom.value).getTime() : 0))

type ChartSeries = { code: string; name: string; color: string; pts: { t: number; y: number }[] }
type ChartGroup = {
  key: FuelGroupKey
  label: string
  stores: ChartStore[]
  series: ChartSeries[]
  yMin: number
  yMax: number
  tMin: number
  tMax: number
}

const fuelChartGroups = computed<ChartGroup[]>(() => {
  const now = Date.now()
  const startT = fuelChartStartT.value
  const groups: ChartGroup[] = []

  for (const group of FUEL_GROUPS) {
    if (!selectedFuelKeys.value.includes(group.key)) continue

    type Built = {
      store: ChartStore
      currentMin: number | null
      anchorY: number
      within: { t: number; y: number }[]
    }
    const built: Built[] = []

    for (const store of fuelChartStores.value) {
      const accIds = new Set(
        visibleFuelAccounts.value.filter((a) => a.store_code === store.code).map((a) => a.account_id),
      )
      const currentPrices = visibleFuelAccounts.value
        .filter((a) => a.store_code === store.code)
        .flatMap((a) => a.prices)
        .filter((p) => fuelGroup(p.name) === group.key)
        .map((p) => effectivePrice(p.price, p.discount))
      const currentMin = currentPrices.length ? Math.min(...currentPrices) : null

      const rows = fuelHistory.value
        .filter((h) => accIds.has(h.account_id) && fuelGroup(h.product_name) === group.key)
        .map((h) => ({
          t: new Date(h.recorded_at).getTime(),
          key: h.product_code ?? h.product_name,
          y: effectivePrice(h.price, h.discount),
        }))
        .sort((a, b) => a.t - b.t)

      if (!rows.length && currentMin == null) continue

      const cur: Record<string, number> = {}
      const changes: { t: number; y: number }[] = []
      let prevMin = Number.NaN
      for (const r of rows) {
        cur[r.key] = r.y
        const m = Math.min(...Object.values(cur))
        if (m !== prevMin) {
          changes.push({ t: r.t, y: m })
          prevMin = m
        }
      }

      const before = changes.filter((c) => c.t <= startT)
      const within = changes.filter((c) => c.t > startT)
      const anchorY = before.length
        ? before[before.length - 1]!.y
        : within.length
          ? within[0]!.y
          : (currentMin as number)
      built.push({ store, currentMin, anchorY, within })
    }

    if (!built.length) continue

    const changeTs = built.flatMap((b) => b.within.map((c) => c.t))
    let tMin = changeTs.length ? Math.min(...changeTs) : now - 7 * DAY_MS
    tMin = Math.min(tMin, now - DAY_MS)

    const stores: ChartStore[] = []
    const series: ChartSeries[] = []
    for (const b of built) {
      stores.push(b.store)
      if (fuelChartHiddenStores.value.has(b.store.code)) continue
      const pts: { t: number; y: number }[] = [{ t: tMin, y: b.anchorY }]
      for (const c of b.within) {
        if (c.t <= tMin) pts[0]!.y = c.y
        else pts.push({ t: c.t, y: c.y })
      }
      pts.push({ t: now, y: b.currentMin ?? pts[pts.length - 1]!.y })
      series.push({ code: b.store.code, name: b.store.name, color: b.store.color, pts })
    }

    const all = series.flatMap((s) => s.pts)
    const ys = all.length ? all.map((p) => p.y) : [0, 1]
    const yMin = Math.min(...ys)
    const yMax = Math.max(...ys)
    const pad = (yMax - yMin) * 0.12 || 0.5
    groups.push({
      key: group.key,
      label: group.label,
      stores,
      series,
      yMin: yMin - pad,
      yMax: yMax + pad,
      tMin,
      tMax: now,
    })
  }
  return groups
})

const CHART_W = 800
const CHART_H = 200
const CHART_PAD = { l: 46, r: 8, t: 10, b: 20 }

function chartX(g: ChartGroup, t: number): number {
  const span = g.tMax - g.tMin || 1
  return CHART_PAD.l + ((t - g.tMin) / span) * (CHART_W - CHART_PAD.l - CHART_PAD.r)
}
function chartY(g: ChartGroup, y: number): number {
  const span = g.yMax - g.yMin || 1
  return CHART_PAD.t + (1 - (y - g.yMin) / span) * (CHART_H - CHART_PAD.t - CHART_PAD.b)
}
function stepPath(g: ChartGroup, pts: { t: number; y: number }[]): string {
  if (!pts.length) return ''
  let d = `M ${chartX(g, pts[0]!.t).toFixed(1)} ${chartY(g, pts[0]!.y).toFixed(1)}`
  for (let i = 1; i < pts.length; i++) {
    d += ` H ${chartX(g, pts[i]!.t).toFixed(1)} V ${chartY(g, pts[i]!.y).toFixed(1)}`
  }
  return d
}
function chartYTicks(g: ChartGroup): { y: number; label: string }[] {
  return [g.yMax, (g.yMax + g.yMin) / 2, g.yMin].map((v) => ({
    y: chartY(g, v),
    label: new Intl.NumberFormat('uk-UA', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(v),
  }))
}
function fmtChartDate(g: ChartGroup, t: number): string {
  const span = g.tMax - g.tMin
  const opts: Intl.DateTimeFormatOptions = { day: '2-digit', month: '2-digit', timeZone: 'Europe/Kyiv' }
  if (span <= 2 * DAY_MS) {
    opts.hour = '2-digit'
    opts.minute = '2-digit'
  } else if (span >= 300 * DAY_MS) {
    opts.year = '2-digit'
  }
  return new Date(t).toLocaleString('uk-UA', opts).replace(',', '')
}

// ---- hover slice on the price chart ---------------------------------------
// A vertical crosshair: what every visible station charged for this fuel at the
// hovered moment (step series — the last change at or before that time).
type ChartHover = {
  key: FuelGroupKey
  label: string
  t: number
  x: number
  rows: { code: string; name: string; color: string; y: number }[]
}
const { hovered, x: tipX, y: tipY, show, hide } = useChartTooltip<ChartHover>()

const priceFmt = new Intl.NumberFormat('uk-UA', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

function priceAt(series: ChartSeries, t: number): number | null {
  let value: number | null = null
  for (const p of series.pts) {
    if (p.t > t) break
    value = p.y
  }
  return value ?? series.pts[0]?.y ?? null
}

function hoverChart(g: ChartGroup, event: MouseEvent) {
  const rect = (event.currentTarget as SVGSVGElement).getBoundingClientRect()
  if (!rect.width) return
  const vx = ((event.clientX - rect.left) / rect.width) * CHART_W
  const inner = CHART_W - CHART_PAD.l - CHART_PAD.r
  const ratio = Math.min(1, Math.max(0, (vx - CHART_PAD.l) / inner))
  const t = g.tMin + ratio * (g.tMax - g.tMin)

  const rows = g.series
    .map((s) => ({ code: s.code, name: s.name, color: s.color, y: priceAt(s, t) }))
    .filter((r): r is ChartHover['rows'][number] => r.y != null)
    .sort((a, b) => a.y - b.y)

  show({ key: g.key, label: g.label, t, x: CHART_PAD.l + ratio * inner, rows }, event)
}

const hoverSubtitle = computed(() => {
  const h = hovered.value
  const group = h ? fuelChartGroups.value.find((g) => g.key === h.key) : undefined
  const when = h && group ? fmtChartDate(group, h.t) : ''
  return `${when} · ${props.includeDiscount ? 'з особистою знижкою' : 'без знижки'}`
})

const hoverRows = computed(() =>
  (hovered.value?.rows ?? []).map((r, i) => ({
    label: `${i === 0 ? '● ' : ''}${r.name}`,
    value: `${priceFmt.format(r.y)} ₴`,
    color: r.color,
    strong: i === 0,
  })),
)
</script>

<template>
  <div v-if="status === 'loading'" class="card"><p class="muted">Завантаження цін на паливо…</p></div>
  <div v-else-if="status === 'error'" class="card"><p class="muted">Не вдалося завантажити ціни на паливо.</p></div>
  <div v-else-if="fuelPrices.length" class="card fuel-block">
    <div class="fuel-block-head">
      <h3 style="margin: 0">Ціни на паливо</h3>
      <DiscountToggle
        :model-value="includeDiscount"
        @update:model-value="emit('update:includeDiscount', $event)"
      />
      <button
        class="fuel-edit-btn"
        :class="{ active: fuelChartOpen }"
        title="Графік зміни цін"
        aria-label="Графік зміни цін"
        @click="toggleFuelChart"
      >
        <svg viewBox="0 0 16 16" class="ico">
          <path d="M2 2v12h12" />
          <path d="M4 10.5 7 7l2.5 2L13 4" />
        </svg>
      </button>
      <button
        class="fuel-edit-btn"
        :class="{ active: fuelEditOpen }"
        title="Налаштувати блок"
        aria-label="Налаштувати блок"
        @click="fuelEditOpen = !fuelEditOpen"
      >
        <svg viewBox="0 0 16 16" class="ico">
          <path d="M11.6 2.3a1.1 1.1 0 0 1 1.6 0l.5.5a1.1 1.1 0 0 1 0 1.6L6.4 12 3 13l1-3.4Z" />
        </svg>
      </button>
    </div>

    <div v-if="fuelEditOpen" class="fuel-edit-panel">
      <div class="fuel-edit-group">
        <div class="fuel-edit-title">Марки палива</div>
        <label v-for="g in FUEL_GROUPS" :key="g.key" class="fuel-edit-check">
          <input type="checkbox" :checked="selectedFuelKeys.includes(g.key)" @change="toggleFuelGroup(g.key)" />
          {{ g.label }}
        </label>
      </div>
      <div class="fuel-edit-group">
        <div class="fuel-edit-title">АЗС</div>
        <label v-for="acc in fuelPrices" :key="acc.account_id" class="fuel-edit-check">
          <input
            type="checkbox"
            :checked="!fuelConfig.hidden_accounts.includes(acc.account_id)"
            @change="toggleFuelAccount(acc.account_id)"
          />
          {{ acc.store_name }} · {{ acc.account_label }}
        </label>
      </div>
    </div>

    <p class="muted fuel-comparison-hint">
      Звичайне та фірмове паливо однієї марки зібрані в одному рядку. Жирним показана ціна з особистою знижкою.
    </p>

    <div v-if="fuelChartOpen" class="fuel-chart-wrap">
      <p v-if="fuelHistoryLoading" class="muted">Завантаження історії…</p>
      <p v-else-if="!fuelChartGroups.length" class="muted">
        Ще недостатньо історії цін для графіка — потрібна щонайменше одна зафіксована зміна ціни.
      </p>
      <div v-for="g in fuelChartGroups" :key="g.key" class="fuel-chart">
        <div class="fuel-chart-title">{{ g.label }}, ₴/л</div>
        <svg
          :viewBox="`0 0 ${CHART_W} ${CHART_H}`"
          class="fuel-chart-svg"
          @mousemove="hoverChart(g, $event)"
          @mouseleave="hide()"
        >
          <g v-for="(tick, i) in chartYTicks(g)" :key="i">
            <line
              :x1="CHART_PAD.l"
              :x2="CHART_W - CHART_PAD.r"
              :y1="tick.y"
              :y2="tick.y"
              stroke="var(--border)"
              stroke-width="1"
            />
            <text :x="CHART_PAD.l - 6" :y="tick.y + 3" text-anchor="end" font-size="10" fill="var(--text-dim)">
              {{ tick.label }}
            </text>
          </g>
          <text :x="CHART_PAD.l" :y="CHART_H - 6" font-size="10" fill="var(--text-dim)">
            {{ fmtChartDate(g, g.tMin) }}
          </text>
          <text :x="CHART_W - CHART_PAD.r" :y="CHART_H - 6" text-anchor="end" font-size="10" fill="var(--text-dim)">
            {{ fmtChartDate(g, g.tMax) }}
          </text>
          <path
            v-for="s in g.series"
            :key="s.code"
            :d="stepPath(g, s.pts)"
            fill="none"
            :stroke="s.color"
            stroke-width="2"
            stroke-linejoin="round"
          />
          <g v-if="hovered && hovered.key === g.key">
            <line
              :x1="hovered.x"
              :x2="hovered.x"
              :y1="CHART_PAD.t"
              :y2="CHART_H - CHART_PAD.b"
              stroke="var(--text-dim)"
              stroke-width="1"
              stroke-dasharray="3 3"
            />
            <circle
              v-for="r in hovered.rows"
              :key="r.code"
              :cx="hovered.x"
              :cy="chartY(g, r.y)"
              r="3.5"
              :fill="r.color"
            />
          </g>
        </svg>
        <div class="fuel-chart-legend">
          <label
            v-for="st in g.stores"
            :key="st.code"
            class="fuel-chart-legend-item"
            :class="{ off: fuelChartHiddenStores.has(st.code) }"
          >
            <input
              type="checkbox"
              :checked="!fuelChartHiddenStores.has(st.code)"
              @change="toggleChartStore(st.code)"
            />
            <span class="swatch" :style="{ background: st.color }"></span>
            <img
              v-if="storeLogoUrl(st.code)"
              :src="storeLogoUrl(st.code)!"
              alt=""
              width="14"
              height="14"
              style="border-radius: 3px"
            />
            <span v-else>{{ storeFallbackIcon(st.code) }}</span>
            {{ st.name }}
          </label>
        </div>
      </div>

      <ChartTooltip
        v-if="hovered"
        :x="tipX"
        :y="tipY"
        :title="hovered.label"
        :subtitle="hoverSubtitle"
        :rows="hoverRows"
      />
    </div>

    <p v-if="!visibleFuelAccounts.length || !fuelComparisonRows.length" class="muted">
      Усі марки або АЗС приховані — увімкніть щось у налаштуваннях блоку (✏️).
    </p>
    <div v-else class="fuel-comparison-scroll">
      <table class="fuel-comparison-table">
        <thead>
          <tr>
            <th>Паливо</th>
            <th v-for="acc in visibleFuelAccounts" :key="acc.account_id">
              <RouterLink :to="`/accounts/${acc.account_id}`" style="display: inline-flex; align-items: center; gap: 6px">
                <img
                  v-if="storeLogoUrl(acc.store_code)"
                  :src="storeLogoUrl(acc.store_code)!"
                  alt=""
                  width="18"
                  height="18"
                  style="border-radius: 4px"
                />
                <span v-else>{{ storeFallbackIcon(acc.store_code) }}</span>
                {{ acc.store_name }}
              </RouterLink>
              <small v-if="showPersonFor(acc)">{{ accountPerson(acc) }}</small>
              <span v-if="acc.bonus_balance != null && acc.bonus_balance !== 0" class="fuel-acc-meta">
                <svg viewBox="0 0 16 16" class="ico">
                  <rect x="1.5" y="4" width="13" height="8" rx="1.5" />
                  <circle cx="8" cy="8" r="1.75" />
                </svg>
                Баланс: {{ fmtMoneyFull(acc.bonus_balance) }}
              </span>
              <span
                v-if="acc.discount_allowance != null"
                class="fuel-acc-meta"
                :title="acc.discount_allowance_resets_at ? `Оновиться ${fmtDate(acc.discount_allowance_resets_at)}` : 'Місячний ліміт знижки на паливо'"
              >
                <svg viewBox="0 0 16 16" class="ico">
                  <rect x="2.5" y="2.5" width="7" height="11" rx="1" />
                  <path d="M2.5 6.5h7M10 5l1.5 1.5V11a1.25 1.25 0 0 1-2.5 0V4" />
                </svg>
                Ліміт знижки: {{ fmtAllowance(acc) }}
                <template v-if="acc.discount_allowance_resets_at">
                  · до {{ fmtDayTime(acc.discount_allowance_resets_at).split(' ')[0] }}
                </template>
              </span>
              <span v-if="acc.checked_at" class="fuel-acc-meta" :title="`Оновлено ${fmtDate(acc.checked_at)}`">
                <svg viewBox="0 0 16 16" class="ico">
                  <circle cx="8" cy="8" r="6" />
                  <path d="M8 4.5V8l2.4 1.5" />
                </svg>
                {{ fmtDayTime(acc.checked_at) }}
              </span>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in fuelComparisonRows" :key="row.key">
            <th scope="row">{{ row.label }}</th>
            <td v-for="acc in visibleFuelAccounts" :key="acc.account_id">
              <div
                v-for="p in row.byAccount[acc.account_id]"
                :key="p.code ?? p.name"
                class="fuel-comparison-price"
              >
                <span class="fuel-name">{{ p.name }}</span>
                <span class="fuel-price-value">
                  <strong>{{ fmtMoneyFull(discountedPrice(p)) }}</strong>
                  <span class="muted">/л</span>
                  <span
                    v-if="priceTrend(p)"
                    class="fuel-trend"
                    :class="`fuel-trend-${priceTrend(p)}`"
                    :title="`Було ${fmtMoneyFull(p.previous_price!)}`"
                  >
                    <svg viewBox="0 0 16 16" class="ico">
                      <path v-if="priceTrend(p) === 'up'" d="M8 3.5 13 11H3z" />
                      <path v-else d="M8 12.5 3 5h10z" />
                    </svg>
                    {{ fmtFuelDelta(p) }}
                  </span>
                </span>
                <small v-if="p.discount" class="fuel-sub">
                  {{ fmtMoneyFull(p.price) }} − {{ fmtMoneyFull(p.discount) }}
                </small>
              </div>
              <span v-if="!row.byAccount[acc.account_id]?.length" class="muted">—</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.ico {
  width: 1em;
  height: 1em;
  flex: none;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.4;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.fuel-block-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}

.fuel-block-head h3 {
  flex: 1;
}

.fuel-edit-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 6px;
  font-size: 16px;
  color: var(--text-dim);
  background: transparent;
  border: 1px solid transparent;
  border-radius: 8px;
  cursor: pointer;
  transition: color 0.15s, background 0.15s, border-color 0.15s;
}

.fuel-edit-btn:hover {
  color: var(--text);
  background: var(--bg-hover);
}

.fuel-edit-btn.active {
  color: var(--accent);
}

.fuel-edit-panel {
  display: flex;
  flex-wrap: wrap;
  gap: 24px;
  padding: 12px 14px;
  margin-bottom: 14px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg);
}

.fuel-edit-check input {
  accent-color: var(--accent);
  width: 15px;
  height: 15px;
}

.fuel-edit-title {
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-dim);
  margin-bottom: 8px;
}

.fuel-edit-check {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 3px 0;
  cursor: pointer;
}

.fuel-comparison-hint {
  margin: -4px 0 14px;
}

.fuel-chart-wrap {
  margin-bottom: 16px;
}

.fuel-chart {
  margin-bottom: 18px;
}

.fuel-chart-title {
  font-size: 12px;
  color: var(--text-dim);
  margin-bottom: 4px;
}

.fuel-chart-svg {
  width: 100%;
  height: 200px;
  display: block;
}

.fuel-chart-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
  margin-top: 4px;
}

.fuel-chart-legend-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  color: var(--text-dim);
  cursor: pointer;
}

.fuel-chart-legend-item input {
  accent-color: var(--accent);
  width: 13px;
  height: 13px;
  margin: 0;
}

.fuel-chart-legend-item.off {
  opacity: 0.45;
}

.fuel-chart-legend-item .swatch {
  width: 10px;
  height: 3px;
  border-radius: 2px;
}

.fuel-comparison-scroll {
  overflow-x: auto;
}

.fuel-comparison-table {
  min-width: 620px;
  table-layout: fixed;
}

.fuel-comparison-table th:first-child {
  width: 130px;
  position: sticky;
  left: 0;
  z-index: 1;
  background: var(--bg-elevated);
}

.fuel-comparison-table thead th:not(:first-child) {
  min-width: 190px;
}

.fuel-comparison-table th small,
.fuel-name {
  display: block;
}

.fuel-comparison-table th small {
  margin-top: 3px;
  color: var(--text-dim);
  font-size: 11px;
  text-transform: none;
  letter-spacing: normal;
}

.fuel-comparison-price + .fuel-comparison-price {
  border-top: 1px dashed var(--border);
  margin-top: 7px;
  padding-top: 7px;
}

.fuel-name {
  color: var(--text-dim);
  font-size: 12px;
  margin-bottom: 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.fuel-price-value {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px;
  white-space: nowrap;
}

.fuel-price-value strong {
  font-size: 14px;
}

.fuel-price-value .muted {
  font-size: 12px;
}

.fuel-trend {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  font-size: 11px;
  font-weight: 600;
}

.fuel-trend .ico {
  fill: currentColor;
  stroke: none;
}

.fuel-trend-up {
  color: var(--danger);
}

.fuel-trend-down {
  color: var(--success);
}

.fuel-sub {
  display: block;
  margin-top: 3px;
  font-size: 11px;
  color: var(--text-dim);
}

.fuel-acc-meta {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-top: 3px;
  font-size: 11px;
  color: var(--text-dim);
  text-transform: none;
  letter-spacing: normal;
}

.fuel-acc-meta .ico {
  color: var(--text-dim);
}

.fuel-comparison-table tbody tr:nth-child(even) td,
.fuel-comparison-table tbody tr:nth-child(even) th {
  background: rgba(15, 23, 42, 0.22);
}

.fuel-comparison-table td,
.fuel-comparison-table th {
  vertical-align: top;
}

@media (max-width: 700px) {
  .fuel-comparison-table th:first-child {
    width: 90px;
  }
}
</style>
