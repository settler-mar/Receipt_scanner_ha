<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import { api } from '@/lib/api'
import { fmtMoney } from '@/lib/dashboardFmt'
import { fmtDateTime } from '@/lib/datetime'
import { useWidgetData } from '@/composables/useWidgetData'

// Not date-range scoped — always the latest 8 receipts.
const { data, status } = useWidgetData(() => api.receipts.list({ limit: 8 }))
const recent = computed(() => data.value?.items ?? [])
</script>

<template>
  <div class="card">
    <h3 style="margin-top: 0">Останні чеки</h3>
    <p v-if="status === 'loading'" class="muted">Завантаження…</p>
    <p v-else-if="status === 'error'" class="muted">Не вдалося завантажити.</p>
    <template v-else>
      <table v-if="recent.length">
        <thead>
          <tr>
            <th>Дата</th>
            <th>Акаунт</th>
            <th>Магазин</th>
            <th>Сума</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in recent" :key="r.id">
            <td><RouterLink :to="`/receipts/${r.id}`">{{ fmtDateTime(r.purchased_at) }}</RouterLink></td>
            <td>{{ r.account_label }}</td>
            <td>{{ r.shop_name ?? r.store_code }}</td>
            <td>{{ fmtMoney(r.total_amount) }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted">Ще немає жодного чека.</p>
    </template>
  </div>
</template>
