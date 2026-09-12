<script setup lang="ts">
import { computed } from 'vue'
import type { TooltipRow } from '@/composables/useChartTooltip'

// Cursor-following tooltip for the dashboard charts. Teleported to <body> so an
// SVG/overflow-hidden chart never clips it, and flipped near the viewport edges.
const props = defineProps<{
  x: number
  y: number
  title: string
  subtitle?: string
  rows: TooltipRow[]
  hint?: string
}>()

const WIDTH = 240
const OFFSET = 14

const style = computed(() => {
  const flipX = props.x + OFFSET + WIDTH > window.innerWidth
  const flipY = props.y > window.innerHeight - 180
  return {
    width: `${WIDTH}px`,
    left: `${flipX ? props.x - OFFSET - WIDTH : props.x + OFFSET}px`,
    top: `${flipY ? props.y - OFFSET : props.y + OFFSET}px`,
    transform: flipY ? 'translateY(-100%)' : 'none',
  }
})
</script>

<template>
  <Teleport to="body">
    <div class="chart-tooltip" :style="style">
      <div class="tt-title">{{ title }}</div>
      <div v-if="subtitle" class="tt-subtitle">{{ subtitle }}</div>
      <div v-for="row in rows" :key="row.label" class="tt-row">
        <span class="tt-label">{{ row.label }}</span>
        <span class="tt-value" :class="{ strong: row.strong }" :style="row.color ? { color: row.color } : {}">
          {{ row.value }}
        </span>
      </div>
      <div v-if="hint" class="tt-hint">{{ hint }}</div>
    </div>
  </Teleport>
</template>

<style scoped>
.chart-tooltip {
  position: fixed;
  z-index: 300;
  pointer-events: none;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 10px;
  box-shadow: 0 8px 24px rgb(0 0 0 / 45%);
  font-size: 12px;
  color: var(--text);
}

.tt-title {
  font-weight: 600;
  margin-bottom: 2px;
  word-break: break-word;
}

.tt-subtitle {
  color: var(--text-dim);
  margin-bottom: 6px;
}

.tt-row {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  line-height: 1.6;
}

.tt-label {
  color: var(--text-dim);
}

.tt-value.strong {
  font-weight: 600;
}

.tt-hint {
  margin-top: 6px;
  padding-top: 5px;
  border-top: 1px solid var(--border);
  color: var(--text-dim);
}
</style>
