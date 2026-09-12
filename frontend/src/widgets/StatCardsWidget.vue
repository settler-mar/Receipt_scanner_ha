<script setup lang="ts">
import { RouterLink } from 'vue-router'
import { api } from '@/lib/api'
import { fmtMoneyFull, rangeParams } from '@/lib/dashboardFmt'
import { fmtDateTime } from '@/lib/datetime'
import { useWidgetData } from '@/composables/useWidgetData'
import type { DashboardRangeKey } from '@/types/api'

const props = withDefaults(
  defineProps<{ range: DashboardRangeKey; includeDiscount?: boolean }>(),
  { includeDiscount: true },
)

const { data: stats, status } = useWidgetData(
  () => api.dashboard.stats(rangeParams(props.range, props.includeDiscount)),
  () => [props.range, props.includeDiscount],
)
</script>

<template>
  <div v-if="status === 'ready' && stats">
   <div class="card-grid">
    <div class="card">
      <div class="stat">{{ stats.total_receipts }}</div>
      <div class="stat-label">Всього чеків</div>
    </div>
    <div class="card">
      <div class="stat">{{ fmtMoneyFull(stats.total_spent) }}</div>
      <div class="stat-label">{{ includeDiscount ? 'Загальна сума' : 'Сума без знижки' }}</div>
    </div>
    <div class="card">
      <div class="stat" style="color: var(--success)">{{ fmtMoneyFull(stats.total_discount) }}</div>
      <div class="stat-label">Заощаджено (знижки)</div>
    </div>
    <div class="card">
      <div class="stat" style="color: var(--accent)">{{ fmtMoneyFull(stats.total_bonuses) }}</div>
      <div class="stat-label">Нараховано бонусів</div>
    </div>
    <div class="card">
      <div class="stat">{{ fmtMoneyFull(stats.total_bonuses_used) }}</div>
      <div class="stat-label">Використано бонусів</div>
    </div>
    <RouterLink to="/accounts" class="card" style="color: inherit">
      <div class="stat">{{ stats.accounts_active }}</div>
      <div class="stat-label">Активних акаунтів</div>
    </RouterLink>
    <RouterLink to="/accounts" class="card" style="color: inherit">
      <div class="stat" :style="stats.accounts_needs_attention ? 'color: var(--danger)' : ''">
        {{ stats.accounts_needs_attention }}
      </div>
      <div class="stat-label">Потребують уваги</div>
    </RouterLink>
   </div>
   <p class="muted" style="margin: -8px 0 0">Останній збір: {{ fmtDateTime(stats.last_run_at) }}</p>
  </div>
  <div v-else class="card">
    <p class="muted">{{ status === 'error' ? 'Не вдалося завантажити показники.' : 'Завантаження…' }}</p>
  </div>
</template>
