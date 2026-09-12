<script setup lang="ts">
import { computed } from 'vue'
import { api } from '@/lib/api'
import { fmtMoney, fmtMoneyFull, rangeParams } from '@/lib/dashboardFmt'
import { useWidgetData } from '@/composables/useWidgetData'
import { fmtShare, useChartTooltip } from '@/composables/useChartTooltip'
import ChartTooltip from '@/components/ChartTooltip.vue'
import DiscountToggle from '@/components/DiscountToggle.vue'
import type { ChannelBreakdown, DashboardRangeKey } from '@/types/api'

const props = withDefaults(
  defineProps<{ range: DashboardRangeKey; includeDiscount?: boolean }>(),
  { includeDiscount: true },
)
const emit = defineEmits<{ 'update:includeDiscount': [boolean] }>()

const { data, status } = useWidgetData(
  () => api.dashboard.byChannel(rangeParams(props.range, props.includeDiscount)),
  () => [props.range, props.includeDiscount],
)

const byChannel = computed(() => data.value ?? [])
const channelTotal = computed(() => byChannel.value.reduce((sum, c) => sum + c.total_spent, 0) || 1)
function channelPct(c: ChannelBreakdown) {
  return Math.round((c.total_spent / channelTotal.value) * 100)
}
function channelLabel(c: ChannelBreakdown) {
  return c.channel === 'online' ? 'Онлайн' : 'Офлайн'
}

// ---- hover slice ----------------------------------------------------------
const { hovered, x: tipX, y: tipY, show, move, hide } = useChartTooltip<ChannelBreakdown>()

const receiptTotal = computed(() => byChannel.value.reduce((sum, c) => sum + c.receipt_count, 0))

const tooltipRows = computed(() => {
  const c = hovered.value
  if (!c) return []
  return [
    { label: 'Сума', value: fmtMoneyFull(c.total_spent), strong: true },
    { label: 'Частка витрат', value: fmtShare(c.total_spent, channelTotal.value) },
    { label: 'Чеків', value: `${c.receipt_count} (${fmtShare(c.receipt_count, receiptTotal.value)})` },
    {
      label: 'Середній чек',
      value: c.receipt_count ? fmtMoneyFull(c.total_spent / c.receipt_count) : '—',
    },
  ]
})
</script>

<template>
  <div class="card">
    <div class="widget-head">
      <h3>Онлайн / офлайн</h3>
      <DiscountToggle
        :model-value="includeDiscount"
        @update:model-value="emit('update:includeDiscount', $event)"
      />
    </div>
    <p v-if="status === 'loading'" class="muted">Завантаження…</p>
    <p v-else-if="status === 'error'" class="muted">Не вдалося завантажити.</p>
    <template v-else>
      <div
        style="display: flex; border-radius: 4px; overflow: hidden; height: 24px; margin-bottom: 12px"
        @mouseleave="hide()"
      >
        <div
          v-for="c in byChannel"
          :key="c.channel"
          :style="`width: ${channelPct(c)}%; background: ${c.channel === 'online' ? 'var(--accent)' : 'var(--text-dim)'}`"
          @mouseenter="show(c, $event)"
          @mousemove="move($event)"
        ></div>
      </div>
      <div
        v-for="c in byChannel"
        :key="c.channel"
        class="toolbar"
        style="margin-bottom: 4px"
        @mouseenter="show(c, $event)"
        @mousemove="move($event)"
        @mouseleave="hide()"
      >
        <span class="badge" :class="`badge-channel-${c.channel}`" style="flex: 1">
          {{ channelLabel(c) }}
        </span>
        <span class="muted">{{ c.receipt_count }} чеків</span>
        <strong>{{ fmtMoney(c.total_spent) }} ({{ channelPct(c) }}%)</strong>
      </div>
      <p v-if="!byChannel.length" class="muted">Немає даних.</p>
    </template>

    <ChartTooltip
      v-if="hovered"
      :x="tipX"
      :y="tipY"
      :title="channelLabel(hovered)"
      :subtitle="includeDiscount ? 'Сплачено (зі знижкою)' : 'Без знижки'"
      :rows="tooltipRows"
    />
  </div>
</template>
