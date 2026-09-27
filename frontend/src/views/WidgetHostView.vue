<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import { WIDGET_MAP } from '@/widgets/registry'
import { DEFAULT_RANGE, isRangeKey } from '@/lib/dashboardFmt'

// Chrome-less single-widget page, for embedding one dashboard block elsewhere
// (a Home Assistant `iframe` / webpage Lovelace card → /#/w/<key>?range=90d).
// App.vue drops the sidebar/frame when the route name is `widget-host`.
const route = useRoute()

const widgetKey = computed(() => String(route.params.widgetKey ?? ''))
const meta = computed(() => WIDGET_MAP.get(widgetKey.value))
const range = computed(() => {
  const r = route.query.range
  return isRangeKey(r) ? r : DEFAULT_RANGE
})
// ?discount=0 → pre-discount (gross) amounts, matching the dashboard checkbox;
// the widget's own checkbox can flip it for this embed (not persisted here).
const includeDiscount = ref(!['0', 'false'].includes(String(route.query.discount ?? '')))
// Widget settings aren't persisted from an embed — the widget falls back to its
// own defaults, and a change lives only for this page's lifetime.
const options = ref<Record<string, unknown> | null>(null)
</script>

<template>
  <div class="widget-host">
    <component
      :is="meta.component"
      v-if="meta"
      :range="range"
      :include-discount="meta.supportsDiscount ? includeDiscount : undefined"
      :options="meta.supportsOptions ? options : undefined"
      @update:include-discount="includeDiscount = $event"
      @update:options="options = $event"
    />
    <div v-else class="card">
      <p class="muted">Невідомий віджет: {{ widgetKey }}</p>
    </div>
  </div>
</template>

<style scoped>
.widget-host {
  padding: 12px;
  max-width: 960px;
  margin: 0 auto;
}
</style>
