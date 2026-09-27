<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import { api } from '@/lib/api'
import {
  fmtMoney,
  fmtMoneyFull,
  METRIC_COLORS,
  rangeDateFromShort,
  rangeParams,
} from '@/lib/dashboardFmt'
import { useWidgetData } from '@/composables/useWidgetData'
import { fmtShare, useChartTooltip } from '@/composables/useChartTooltip'
import ChartTooltip from '@/components/ChartTooltip.vue'
import DiscountToggle from '@/components/DiscountToggle.vue'
import type { DashboardRangeKey, StoreBreakdown } from '@/types/api'

const props = withDefaults(
  defineProps<{ range: DashboardRangeKey; includeDiscount?: boolean }>(),
  { includeDiscount: true },
)
const emit = defineEmits<{ 'update:includeDiscount': [boolean] }>()

const { data, status } = useWidgetData(
  () => api.dashboard.byStore(rangeParams(props.range, props.includeDiscount)),
  () => [props.range, props.includeDiscount],
)

const byStore = computed(() => data.value ?? [])
const maxStore = computed(() => Math.max(1, ...byStore.value.map((s) => s.total_spent)))
const totalSpent = computed(() => byStore.value.reduce((sum, s) => sum + s.total_spent, 0))
const dateFromShort = computed(() => rangeDateFromShort(props.range))

const storeColors: Record<string, string> = {
  atb: '#f87171',
  fozzy: '#fbbf24',
  fora: '#a78bfa',
  silpo: '#4ade80',
  epicentrk: '#38bdf8',
  metro: '#fb923c',
}
function storeColor(code: string) {
  return storeColors[code] ?? 'var(--accent-dim)'
}

// ---- hover slice ----------------------------------------------------------
const { hovered, x: tipX, y: tipY, show, move, hide } = useChartTooltip<StoreBreakdown>()

const tooltipRows = computed(() => {
  const s = hovered.value
  if (!s) return []
  return [
    { label: 'Сума', value: fmtMoneyFull(s.total_spent), strong: true },
    { label: 'Чеків', value: String(s.receipt_count) },
    {
      label: 'Середній чек',
      value: s.receipt_count ? fmtMoneyFull(s.total_spent / s.receipt_count) : '—',
    },
    { label: 'Частка витрат', value: fmtShare(s.total_spent, totalSpent.value) },
    {
      label: 'Знижки',
      value: fmtMoneyFull(s.total_discount),
      color: METRIC_COLORS.total_discount,
    },
    { label: 'Бонуси', value: fmtMoneyFull(s.total_bonuses), color: METRIC_COLORS.total_bonuses },
  ]
})
</script>

<template>
  <div class="card">
    <div class="widget-head">
      <h3>Витрати за магазином</h3>
      <DiscountToggle
        :model-value="includeDiscount"
        @update:model-value="emit('update:includeDiscount', $event)"
      />
    </div>
    <p v-if="status === 'loading'" class="muted">Завантаження…</p>
    <p v-else-if="status === 'error'" class="muted">Не вдалося завантажити.</p>
    <template v-else>
      <div
        v-for="s in byStore"
        :key="s.store_code"
        style="margin-bottom: 16px"
        @mouseenter="show(s, $event)"
        @mousemove="move($event)"
        @mouseleave="hide()"
      >
        <RouterLink
          :to="{ path: '/receipts', query: { store_code: s.store_code, date_from: dateFromShort } }"
          style="color: inherit; text-decoration: none"
        >
          <div class="toolbar" style="margin-bottom: 6px; flex-wrap: nowrap">
            <span style="flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap">{{ s.store_name }}</span>
            <span class="muted">{{ s.receipt_count }} чеків</span>
            <strong>{{ fmtMoney(s.total_spent) }}</strong>
          </div>
          <div style="background: var(--bg); border-radius: 4px; overflow: hidden; height: 8px">
            <div
              :style="`width: ${(s.total_spent / maxStore) * 100}%; height: 100%; background: ${storeColor(s.store_code)}`"
            ></div>
          </div>
        </RouterLink>
      </div>
      <p v-if="!byStore.length" class="muted">Немає даних.</p>
    </template>

    <ChartTooltip
      v-if="hovered"
      :x="tipX"
      :y="tipY"
      :title="hovered.store_name"
      :subtitle="includeDiscount ? 'Сплачено (зі знижкою)' : 'Без знижки'"
      :rows="tooltipRows"
      hint="Клік — чеки цього магазину"
    />
  </div>
</template>
