<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { api } from '@/lib/api'
import { fmtMoney, fmtMoneyFull, rangeParams } from '@/lib/dashboardFmt'
import { useWidgetData } from '@/composables/useWidgetData'
import { fmtShare, useChartTooltip } from '@/composables/useChartTooltip'
import ChartTooltip from '@/components/ChartTooltip.vue'
import DiscountToggle from '@/components/DiscountToggle.vue'
import type { CategorySpend, DashboardRangeKey } from '@/types/api'

const props = withDefaults(
  defineProps<{ range: DashboardRangeKey; includeDiscount?: boolean }>(),
  { includeDiscount: true },
)
const emit = defineEmits<{ 'update:includeDiscount': [boolean] }>()

const { data, status } = useWidgetData(
  () => api.dashboard.byCategory(rangeParams(props.range, props.includeDiscount)),
  () => [props.range, props.includeDiscount],
)

const byCategory = computed(() => data.value ?? [])
const expandedCats = ref<Set<string>>(new Set())

function catDepth(c: CategorySpend) {
  return c.path === '' ? 0 : c.path.split('/').length - 1
}
function catHasChildren(c: CategorySpend) {
  return (
    c.path !== '' &&
    byCategory.value.some(
      (o) => o.rollup_total > 0 && o.path.startsWith(c.path + '/') && catDepth(o) === catDepth(c) + 1,
    )
  )
}
// Hierarchy-ordered rows: each expanded parent is immediately followed by its
// own children (indented), so expanding a block visibly pushes the rows after it
// down instead of scattering children across the list by amount.
const visibleCategories = computed(() => {
  const all = byCategory.value.filter((c) => c.rollup_total > 0)
  const parentPath = (c: CategorySpend) => c.path.split('/').slice(0, -1).join('/')
  const childrenOf = (path: string, depth: number) =>
    all
      .filter((c) => c.path !== '' && catDepth(c) === depth && parentPath(c) === path)
      .sort((a, b) => b.rollup_total - a.rollup_total)

  const out: CategorySpend[] = []
  const walk = (path: string, depth: number) => {
    for (const c of childrenOf(path, depth)) {
      out.push(c)
      if (expandedCats.value.has(c.path)) walk(c.path, depth + 1)
    }
  }
  for (const root of all.filter((c) => catDepth(c) === 0).sort((a, b) => b.rollup_total - a.rollup_total)) {
    out.push(root)
    if (root.path !== '' && expandedCats.value.has(root.path)) walk(root.path, 1)
  }
  return out
})
const maxCategory = computed(() =>
  Math.max(1, ...byCategory.value.filter((c) => catDepth(c) === 0).map((c) => c.rollup_total)),
)
const rootsTotal = computed(() =>
  byCategory.value.filter((c) => catDepth(c) === 0).reduce((sum, c) => sum + c.rollup_total, 0),
)

// ---- hover slice ----------------------------------------------------------
const { hovered, x: tipX, y: tipY, show, move, hide } = useChartTooltip<CategorySpend>()

const tooltipRows = computed(() => {
  const c = hovered.value
  if (!c) return []
  const parent = c.path.includes('/')
    ? byCategory.value.find((o) => o.path === c.path.split('/').slice(0, -1).join('/'))
    : undefined
  return [
    { label: 'Разом з підкатегоріями', value: fmtMoneyFull(c.rollup_total), strong: true },
    ...(c.rollup_total !== c.direct_total
      ? [{ label: 'Тільки ця категорія', value: fmtMoneyFull(c.direct_total) }]
      : []),
    { label: 'Позицій', value: String(c.item_count) },
    {
      label: 'Середня позиція',
      value: c.item_count ? fmtMoneyFull(c.rollup_total / c.item_count) : '—',
    },
    { label: 'Частка витрат', value: fmtShare(c.rollup_total, rootsTotal.value) },
    ...(parent
      ? [{ label: `Частка «${parent.name}»`, value: fmtShare(c.rollup_total, parent.rollup_total) }]
      : []),
  ]
})

function toggleCat(c: CategorySpend) {
  if (!catHasChildren(c)) return
  const next = new Set(expandedCats.value)
  if (next.has(c.path)) next.delete(c.path)
  else next.add(c.path)
  expandedCats.value = next
}
</script>

<template>
  <div class="card">
    <div class="widget-head">
      <h3>Витрати за категоріями</h3>
      <DiscountToggle
        :model-value="includeDiscount"
        @update:model-value="emit('update:includeDiscount', $event)"
      />
    </div>
    <p v-if="status === 'loading'" class="muted">Завантаження…</p>
    <p v-else-if="status === 'error'" class="muted">Не вдалося завантажити.</p>
    <template v-else>
      <div
        v-for="c in visibleCategories"
        :key="c.path || 'uncat'"
        :style="{ marginBottom: '16px', marginLeft: catDepth(c) * 16 + 'px' }"
        @mouseenter="show(c, $event)"
        @mousemove="move($event)"
        @mouseleave="hide()"
      >
        <div class="toolbar" style="margin-bottom: 6px; flex-wrap: nowrap">
          <span
            :style="{
              flex: 1,
              minWidth: 0,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              cursor: catHasChildren(c) ? 'pointer' : 'default',
            }"
            @click="toggleCat(c)"
          >
            <span v-if="catHasChildren(c)" class="muted">{{ expandedCats.has(c.path) ? '▾' : '▸' }}</span>
            {{ c.name }}
          </span>
          <span class="muted">{{ c.item_count }} поз.</span>
          <strong>{{ fmtMoney(c.rollup_total) }}</strong>
        </div>
        <div style="background: var(--bg); border-radius: 4px; overflow: hidden; height: 8px">
          <div
            :style="`width: ${Math.min(100, (c.rollup_total / maxCategory) * 100)}%; height: 100%; background: var(--accent)`"
          ></div>
        </div>
      </div>
      <p v-if="!byCategory.length" class="muted">
        Немає даних. Категорії проставляються автоматично у фоні — див.
        <RouterLink to="/categories">Категорії</RouterLink>.
      </p>
    </template>

    <ChartTooltip
      v-if="hovered"
      :x="tipX"
      :y="tipY"
      :title="hovered.name"
      :subtitle="includeDiscount ? 'Сплачено (зі знижкою)' : 'Без знижки'"
      :rows="tooltipRows"
      :hint="catHasChildren(hovered) ? 'Клік — розгорнути підкатегорії' : undefined"
    />
  </div>
</template>
