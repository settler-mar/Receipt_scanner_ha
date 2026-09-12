<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/lib/api'
import { fmtMoneyFull, fmtPeriod, periodRange, rangeParams } from '@/lib/dashboardFmt'
import { useWidgetData } from '@/composables/useWidgetData'
import { fmtShare, useChartTooltip } from '@/composables/useChartTooltip'
import ChartTooltip from '@/components/ChartTooltip.vue'
import DiscountToggle from '@/components/DiscountToggle.vue'
import type { DashboardRangeKey, TimeseriesPoint } from '@/types/api'

const props = withDefaults(
  defineProps<{ range: DashboardRangeKey; includeDiscount?: boolean }>(),
  { includeDiscount: true },
)
const emit = defineEmits<{ 'update:includeDiscount': [boolean] }>()
const router = useRouter()

const { data, status } = useWidgetData(
  () => api.dashboard.timeseries(rangeParams(props.range, props.includeDiscount)),
  () => [props.range, props.includeDiscount],
)

const trend = computed(() => data.value ?? [])
const maxTrend = computed(() => Math.max(1, ...trend.value.map((p) => p.total_spent)))
const totalSpent = computed(() => trend.value.reduce((sum, p) => sum + p.total_spent, 0))

function goToPeriod(period: string) {
  router.push({ path: '/receipts', query: periodRange(period) })
}

// ---- hover slice ----------------------------------------------------------
const { hovered, x: tipX, y: tipY, show, move, hide } = useChartTooltip<TimeseriesPoint>()

const tooltipRows = computed(() => {
  const p = hovered.value
  if (!p) return []
  const i = trend.value.findIndex((x) => x.period === p.period)
  const prev = i > 0 ? trend.value[i - 1] : undefined
  const delta = prev ? p.total_spent - prev.total_spent : null
  return [
    { label: 'Сума', value: fmtMoneyFull(p.total_spent), strong: true },
    { label: 'Чеків', value: String(p.receipt_count) },
    {
      label: 'Середній чек',
      value: p.receipt_count ? fmtMoneyFull(p.total_spent / p.receipt_count) : '—',
    },
    { label: 'Частка періоду', value: fmtShare(p.total_spent, totalSpent.value) },
    ...(delta === null
      ? []
      : [
          {
            label: 'До попереднього',
            value: `${delta >= 0 ? '+' : '−'}${fmtMoneyFull(Math.abs(delta))}`,
            color: delta >= 0 ? 'var(--danger)' : 'var(--success)',
          },
        ]),
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
    <svg
      v-else-if="trend.length"
      viewBox="0 0 800 220"
      style="width: 100%; height: 200px"
      @mouseleave="hide()"
    >
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
        <!-- Full-height hit area: thin bars are hard to hover on their own. -->
        <rect
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
    </svg>
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
