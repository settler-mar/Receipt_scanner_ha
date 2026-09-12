<script setup lang="ts">
import { ref } from 'vue'
import { RANGES, DEFAULT_RANGE } from '@/lib/dashboardFmt'
import StoreCategoryWidget from '@/widgets/StoreCategoryWidget.vue'
import type { DashboardRangeKey } from '@/types/api'

const range = ref<DashboardRangeKey>(DEFAULT_RANGE)
</script>

<template>
  <div class="toolbar">
    <h2 style="margin: 0; flex: 1">Магазини × категорії</h2>
    <button
      v-for="r in RANGES"
      :key="r.key"
      class="secondary"
      :style="range === r.key ? 'background: var(--accent-dim); color: white' : ''"
      @click="range = r.key"
    >
      {{ r.label }}
    </button>
  </div>

  <StoreCategoryWidget :range="range" />
</template>
