<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/lib/api'
import {
  fmtMoney,
  fmtMoneyFull,
  fmtPeriod,
  METRIC_COLORS,
  periodRange,
  rangeParams,
} from '@/lib/dashboardFmt'
import { useWidgetData } from '@/composables/useWidgetData'
import { fmtShare, useChartTooltip } from '@/composables/useChartTooltip'
import ChartTooltip from '@/components/ChartTooltip.vue'
import DiscountToggle from '@/components/DiscountToggle.vue'
import type { DashboardRangeKey, TimeseriesPoint } from '@/types/api'

const props = withDefaults(
  defineProps<{
    range: DashboardRangeKey
    includeDiscount?: boolean
    /** Persisted widget settings; `{ series: [...] }` picks the overlay lines. */
    options?: Record<string, unknown> | null
  }>(),
  { includeDiscount: true, options: null },
)
const emit = defineEmits<{
  'update:includeDiscount': [boolean]
  'update:options': [Record<string, unknown>]
}>()
const router = useRouter()

// ---- overlay series -------------------------------------------------------
// The bars are spend; these ride on top on their own (right-hand) scale, since
// savings/bonuses are an order of magnitude smaller than the spend they come from.
type SeriesKey = 'total_discount' | 'total_bonuses' | 'total_bonuses_used'

const SERIES: { key: SeriesKey; label: string; color: string }[] = [
  { key: 'total_discount', label: 'Заощаджено (знижки)', color: METRIC_COLORS.total_discount },
  { key: 'total_bonuses', label: 'Нараховано бонусів', color: METRIC_COLORS.total_bonuses },
  { key: 'total_bonuses_used', label: 'Використано бонусів', color: METRIC_COLORS.total_bonuses_used },
]
const DEFAULT_SERIES: SeriesKey[] = ['total_discount', 'total_bonuses']

const enabled = computed<SeriesKey[]>(() => {
  const stored = (props.options as { series?: unknown } | null)?.series
  if (!Array.isArray(stored)) return DEFAULT_SERIES
  return SERIES.map((s) => s.key).filter((k) => stored.includes(k))
})

function toggleSeries(key: SeriesKey) {
  const next = enabled.value.includes(key)
    ? enabled.value.filter((k) => k !== key)
    : [...enabled.value, key]
  emit('update:options', { ...(props.options ?? {}), series: next })
}

const { data, status } = useWidgetData(
  () => api.dashboard.timeseries(rangeParams(props.range, props.includeDiscount)),
  () => [props.range, props.includeDiscount],
)

const trend = computed(() => data.value ?? [])
const maxTrend = computed(() => Math.max(1, ...trend.value.map((p) => p.total_spent)))
const totalSpent = computed(() => trend.value.reduce((sum, p) => sum + p.total_spent, 0))

/** Shared scale for every drawn overlay series, so they stay comparable. */
const maxSeries = computed(() =>
  Math.max(1, ...trend.value.flatMap((p) => enabled.value.map((k) => p[k]))),
)

const totals = computed(() =>
  Object.fromEntries(
    SERIES.map((s) => [s.key, trend.value.reduce((sum, p) => sum + p[s.key], 0)]),
  ) as Record<SeriesKey, number>,
)

// Chart geometry: 800×220 viewBox, 180px of plot height above the period labels.
const PLOT_H = 170
const BASE_Y = 180

function slotX(i: number): number {
  return (i * 800) / trend.value.length + 800 / trend.value.length / 2
}

function seriesY(value: number): number {
  return BASE_Y - (value / maxSeries.value) * PLOT_H
}

function linePoints(key: SeriesKey): string {
  return trend.value.map((p, i) => `${slotX(i)},${seriesY(p[key])}`).join(' ')
}

/** "+1 234,00 грн" / "−1 234,00 грн" — signed money for the delta rows. */
function fmtSigned(v: number): string {
  return `${v >= 0 ? '+' : '−'}${fmtMoneyFull(Math.abs(v))}`
}

function goToPeriod(period: string) {
  router.push({ path: '/receipts', query: periodRange(period) })
}

// ---- hover slice ----------------------------------------------------------
const { hovered, x: tipX, y: tipY, show, move, hide } = useChartTooltip<TimeseriesPoint>()

const tooltipRows = computed(() => {
  const p = hovered.value
  if (!p) return []
  const gross = p.total_spent + (props.includeDiscount ? p.total_discount : 0)
  const bonusDelta = p.total_bonuses - p.total_bonuses_used
  return [
    { label: 'Сума', value: fmtMoneyFull(p.total_spent), strong: true, color: METRIC_COLORS.spend },
    { label: 'Чеків', value: String(p.receipt_count) },
    {
      label: 'Середній чек',
      value: p.receipt_count ? fmtMoneyFull(p.total_spent / p.receipt_count) : '—',
    },
    { label: 'Частка періоду', value: fmtShare(p.total_spent, totalSpent.value) },
    {
      label: 'Заощаджено',
      value: `${fmtMoneyFull(p.total_discount)} (${fmtShare(p.total_discount, gross)})`,
      color: METRIC_COLORS.total_discount,
    },
    {
      label: 'Нараховано бонусів',
      value: fmtMoneyFull(p.total_bonuses),
      color: METRIC_COLORS.total_bonuses,
    },
    {
      label: 'Використано бонусів',
      value: fmtMoneyFull(p.total_bonuses_used),
      color: METRIC_COLORS.total_bonuses_used,
    },
    {
      // Net loyalty movement for the bucket: what the balance gained or lost.
      label: 'Дельта бонусів',
      value: fmtSigned(bonusDelta),
      color: METRIC_COLORS.total_bonuses,
      strong: true,
    },
  ]
})
</script>

<template>
  <div class="card">
    <div class="widget-head">
      <h3>Витрати за період</h3>
      <DiscountToggle
        :model-value="includeDiscount"
        @update:model-value="emit('update:includeDiscount', $event)"
      />
    </div>
    <p v-if="status === 'loading'" class="muted">Завантаження…</p>
    <p v-else-if="status === 'error'" class="muted">Не вдалося завантажити.</p>
    <template v-else-if="trend.length">
      <svg viewBox="0 0 800 220" style="width: 100%; height: 200px" @mouseleave="hide()">
        <g v-for="(p, i) in trend" :key="p.period">
          <rect
            :x="(i * 800) / trend.length + 2"
            :y="180 - (p.total_spent / maxTrend) * 170"
            :width="Math.max(1, 800 / trend.length - 4)"
            :height="(p.total_spent / maxTrend) * 170"
            :fill="hovered?.period === p.period ? 'var(--accent)' : 'var(--accent-dim)'"
            style="cursor: pointer"
            @click="goToPeriod(p.period)"
            @mouseenter="show(p, $event)"
            @mousemove="move($event)"
          />
          <text
            :x="(i * 800) / trend.length + 800 / trend.length / 2"
            y="198"
            font-size="11"
            fill="var(--text-dim)"
            text-anchor="middle"
          >
            {{ trend.length <= 24 ? fmtPeriod(p.period) : '' }}
          </text>
        </g>

        <!-- Overlay series on their own scale (see maxSeries), drawn under the
             hit areas so hovering a bucket still works over a line. -->
        <g v-for="s in SERIES" :key="s.key">
          <template v-if="enabled.includes(s.key)">
            <polyline
              :points="linePoints(s.key)"
              fill="none"
              :stroke="s.color"
              stroke-width="2"
              stroke-linejoin="round"
              pointer-events="none"
            />
            <circle
              v-for="(p, i) in trend"
              :key="p.period"
              :cx="slotX(i)"
              :cy="seriesY(p[s.key])"
              :r="hovered?.period === p.period ? 4 : 2.5"
              :fill="s.color"
              pointer-events="none"
            />
          </template>
        </g>

        <!-- Full-height hit areas: thin bars are hard to hover on their own. -->
        <rect
          v-for="(p, i) in trend"
          :key="`hit-${p.period}`"
          :x="(i * 800) / trend.length"
          y="0"
          :width="800 / trend.length"
          height="180"
          fill="transparent"
          style="cursor: pointer"
          @click="goToPeriod(p.period)"
          @mouseenter="show(p, $event)"
          @mousemove="move($event)"
        />
      </svg>

      <div class="legend">
        <button
          v-for="s in SERIES"
          :key="s.key"
          class="legend-item"
          :class="{ off: !enabled.includes(s.key) }"
          :title="enabled.includes(s.key) ? 'Прибрати з графіка' : 'Показати на графіку'"
          @click="toggleSeries(s.key)"
        >
          <span class="swatch" :style="{ background: s.color }"></span>
          <span :style="{ color: s.color }">{{ s.label }}</span>
          <strong :style="{ color: s.color }">{{ fmtMoney(totals[s.key]) }}</strong>
        </button>
        <span v-if="enabled.length" class="muted scale-note">
          лінії — окрема шкала, максимум {{ fmtMoney(maxSeries) }}
        </span>
      </div>
    </template>
    <p v-else class="muted">Немає даних за обраний період.</p>

    <ChartTooltip
      v-if="hovered"
      :x="tipX"
      :y="tipY"
      :title="fmtPeriod(hovered.period)"
      :subtitle="includeDiscount ? 'Сплачено (зі знижкою)' : 'Без знижки'"
      :rows="tooltipRows"
      hint="Клік — чеки за цей період"
    />
  </div>
</template>

<style scoped>
.legend {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 14px;
  margin-top: 10px;
}

.legend-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: none;
  border: none;
  padding: 2px 0;
  font-size: 13px;
  color: var(--text);
  cursor: pointer;
}

.legend-item.off {
  opacity: 0.45;
}

/* A switched-off series drops its colour entirely — swatch and text both. */
.legend-item.off .swatch {
  background: var(--border) !important;
}

.legend-item.off span,
.legend-item.off strong {
  color: var(--text-dim) !important;
}

.swatch {
  width: 10px;
  height: 10px;
  border-radius: 2px;
}

.scale-note {
  font-size: 12px;
  margin-left: auto;
}
</style>
