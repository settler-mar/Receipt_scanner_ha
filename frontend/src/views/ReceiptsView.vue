<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { api } from '@/lib/api'
import { storeFallbackIcon, storeLogoUrl } from '@/lib/storeIcons'
import type { Account, Receipt, ReceiptProblemAction, Store } from '@/types/api'
import { fmtDateTime } from '@/lib/datetime'

const route = useRoute()
const receipts = ref<Receipt[]>([])
const total = ref(0)
const accounts = ref<Account[]>([])
const stores = ref<Store[]>([])
const loading = ref(true)

// Pre-filled from the URL so links from Dashboard (e.g. "усі чеки Сільпо цього
// місяця") land here already filtered, not just on the unfiltered list.
const q = route.query
const filters = ref({
  account_id: '',
  store_code: typeof q.store_code === 'string' ? q.store_code : '',
  channel: typeof q.channel === 'string' ? q.channel : '',
  date_from: typeof q.date_from === 'string' ? q.date_from : '',
  date_to: typeof q.date_to === 'string' ? q.date_to : '',
  tag: typeof q.tag === 'string' ? q.tag : '',
  problem: typeof q.problem === 'string' ? q.problem : '',
  sort_by: typeof q.sort_by === 'string' ? q.sort_by : 'date',
  sort_dir: q.sort_dir === 'asc' ? 'asc' : 'desc',
})
const tagStats = ref<import('@/types/api').TagSummary[]>([])
const limit = 50
const offset = ref(0)
const selected = ref<Set<string>>(new Set())
const bulkBusy = ref(false)
const bulkMessage = ref('')

async function load() {
  loading.value = true
  // Expand plain YYYY-MM-DD to day boundaries — otherwise date_to (parsed as
  // that day's midnight) would silently exclude the rest of its own day.
  const dateFrom = filters.value.date_from ? `${filters.value.date_from.slice(0, 10)}T00:00:00` : undefined
  const dateTo = filters.value.date_to ? `${filters.value.date_to.slice(0, 10)}T23:59:59` : undefined
  const res = await api.receipts.list({
    account_id: filters.value.account_id || undefined,
    store_code: filters.value.store_code || undefined,
    channel: filters.value.channel || undefined,
    tag: filters.value.tag || undefined,
    problem: filters.value.problem || undefined,
    sort_by: filters.value.sort_by || undefined,
    sort_dir: filters.value.sort_dir || undefined,
    date_from: dateFrom,
    date_to: dateTo,
    limit,
    offset: offset.value,
  })
  receipts.value = res.items
  total.value = res.total
  selected.value = new Set()
  loading.value = false
}

const visibleIds = computed(() => receipts.value.map((receipt) => receipt.id))
const allVisibleSelected = computed(() =>
  visibleIds.value.length > 0 && visibleIds.value.every((id) => selected.value.has(id)),
)
const selectedCount = computed(() => selected.value.size)

function toggleSelected(id: string) {
  const next = new Set(selected.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  selected.value = next
}

function toggleAllVisible() {
  selected.value = allVisibleSelected.value ? new Set() : new Set(visibleIds.value)
}

async function runBulk(action: ReceiptProblemAction, confirmation: string) {
  const ids = [...selected.value]
  if (!ids.length || bulkBusy.value) return
  if (!confirm(`${confirmation} для ${ids.length} чеків?`)) return
  bulkBusy.value = true
  bulkMessage.value = ''
  try {
    const result = await api.receipts.bulkProblemAction(ids, action)
    bulkMessage.value = action === 'delete'
      ? `Видалено чеків: ${result.deleted}`
      : `Оброблено чеків: ${result.updated}`
    await load()
  } catch (e) {
    bulkMessage.value = (e as Error).message
  } finally {
    bulkBusy.value = false
  }
}

async function loadTagStats() {
  tagStats.value = await api.receipts.tags().catch(() => [])
}
function pickTag(tag: string) {
  filters.value.tag = filters.value.tag === tag ? '' : tag
}

onMounted(async () => {
  ;[accounts.value, stores.value] = await Promise.all([api.accounts.list(), api.stores.list()])
  await Promise.all([load(), loadTagStats()])
})

watch(filters, () => {
  offset.value = 0
  load()
}, { deep: true })

function fmtMoney(v: number) {
  return new Intl.NumberFormat('uk-UA', { style: 'currency', currency: 'UAH' }).format(v)
}
function fmtDate(v: string) {
  return fmtDateTime(v)
}

function nextPage() {
  if (offset.value + limit >= total.value) return
  offset.value += limit
  load()
}
function prevPage() {
  offset.value = Math.max(0, offset.value - limit)
  load()
}
</script>

<template>
  <div class="toolbar">
    <h2 style="margin: 0; flex: 1">Чеки</h2>
    <RouterLink to="/receipts/new" class="btn">+ Додати чек вручну</RouterLink>
  </div>

  <div class="toolbar">
    <select v-model="filters.account_id">
      <option value="">Всі акаунти</option>
      <option v-for="a in accounts" :key="a.id" :value="a.id">{{ a.label }}</option>
    </select>
    <select v-model="filters.store_code">
      <option value="">Всі магазини</option>
      <option v-for="s in stores" :key="s.code" :value="s.code">{{ s.name }}</option>
    </select>
    <select v-model="filters.channel">
      <option value="">Всі канали</option>
      <option value="offline">Офлайн</option>
      <option value="online">Онлайн</option>
    </select>
    <input v-model="filters.date_from" type="date" />
    <input v-model="filters.date_to" type="date" />
    <select v-model="filters.problem">
      <option value="">Проблеми: всі чеки</option>
      <option value="open">Тільки проблемні</option>
      <option value="ignored">Проблемні з ігнором</option>
    </select>
    <select v-model="filters.sort_by" aria-label="Сортувати за">
      <option value="date">Сортувати: дата</option>
      <option value="account">Сортувати: акаунт</option>
      <option value="channel">Сортувати: канал</option>
      <option value="store">Сортувати: магазин</option>
      <option value="address">Сортувати: адреса</option>
      <option value="tags">Сортувати: теги</option>
      <option value="amount">Сортувати: сума</option>
      <option value="discount">Сортувати: знижка</option>
      <option value="items_total">Сортувати: сума позицій</option>
      <option value="difference">Сортувати: розбіжність</option>
      <option value="problem_ignored_at">Сортувати: дата ігнорування</option>
    </select>
    <select v-model="filters.sort_dir" aria-label="Напрямок сортування">
      <option value="desc">За спаданням ↓</option>
      <option value="asc">За зростанням ↑</option>
    </select>
    <span class="muted">Знайдено: {{ total }}</span>
  </div>

  <div v-if="selectedCount" class="card bulk-bar" style="margin-bottom: 16px">
    <strong>Вибрано: {{ selectedCount }}</strong>
    <button class="secondary" :disabled="bulkBusy" @click="runBulk('recalculate_amount', 'Оновити суму')">
      Оновити суму
    </button>
    <button class="secondary" :disabled="bulkBusy" @click="runBulk('recalculate_discount', 'Оновити знижку')">
      Оновити знижку
    </button>
    <button class="secondary" :disabled="bulkBusy" @click="runBulk('recalculate_both', 'Оновити суму та знижку')">
      Оновити все
    </button>
    <button
      v-if="filters.problem !== 'ignored'"
      class="secondary"
      :disabled="bulkBusy"
      @click="runBulk('ignore', 'Ігнорувати розбіжності')"
    >
      Ігнорувати проблему
    </button>
    <button
      v-if="filters.problem === 'ignored'"
      class="secondary"
      :disabled="bulkBusy"
      @click="runBulk('unignore', 'Повернути в проблеми')"
    >
      Скасувати ігнор
    </button>
    <button class="danger" :disabled="bulkBusy" @click="runBulk('delete', 'Видалити')">Видалити</button>
    <button class="secondary" :disabled="bulkBusy" @click="selected = new Set()">Зняти вибір</button>
    <span v-if="bulkBusy" class="muted">Обробляємо…</span>
    <span v-else-if="bulkMessage" class="muted">{{ bulkMessage }}</span>
  </div>
  <p v-else-if="bulkMessage" class="muted">{{ bulkMessage }}</p>

  <div v-if="tagStats.length" class="card" style="margin-bottom: 16px">
    <div class="stat-label" style="margin-bottom: 8px">Витрати за тегами</div>
    <div style="display: flex; flex-wrap: wrap; gap: 8px">
      <button
        v-for="t in tagStats"
        :key="t.tag"
        class="secondary"
        :style="filters.tag === t.tag ? 'outline: 2px solid var(--accent)' : ''"
        style="padding: 5px 10px; font-size: 12px"
        @click="pickTag(t.tag)"
      >
        #{{ t.tag }} · {{ fmtMoney(t.total_amount) }} <span class="muted">({{ t.receipt_count }})</span>
      </button>
    </div>
  </div>

  <div v-if="loading" class="muted">Завантаження…</div>
  <table v-else>
    <thead>
      <tr>
        <th><input type="checkbox" :checked="allVisibleSelected" @change="toggleAllVisible" /></th>
        <th>Дата</th>
        <th>Акаунт</th>
        <th>Канал</th>
        <th>Магазин</th>
        <th>Адреса</th>
        <th>Теги</th>
        <th>Сума</th>
        <th>Знижка</th>
        <th>Звірка</th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="r in receipts" :key="r.id">
        <td><input type="checkbox" :checked="selected.has(r.id)" @change="toggleSelected(r.id)" /></td>
        <td style="white-space: nowrap">
          <RouterLink :to="`/receipts/${r.id}`">{{ fmtDate(r.purchased_at) }}</RouterLink>
          <span v-if="r.has_warranty" title="Є гарантійний талон" style="margin-left: 4px">🛡️</span>
        </td>
        <td>{{ r.account_label }}</td>
        <td><span class="badge" :class="`badge-channel-${r.channel}`">{{ r.channel === 'online' ? 'Онлайн' : 'Офлайн' }}</span></td>
        <td>
          <span style="display: flex; align-items: center; gap: 6px">
            <img
              v-if="storeLogoUrl(r.store_code)"
              :src="storeLogoUrl(r.store_code)!"
              alt=""
              width="16"
              height="16"
              style="border-radius: 3px; flex-shrink: 0"
            />
            <span v-else>{{ storeFallbackIcon(r.store_code) }}</span>
            {{ r.shop_name ?? '—' }}
          </span>
        </td>
        <td class="muted">{{ r.shop_address ?? '—' }}</td>
        <td>
          <span
            v-for="t in r.tags"
            :key="t"
            class="badge"
            style="background: var(--bg-hover); color: var(--text); cursor: pointer; margin: 0 3px 3px 0; font-size: 11px"
            @click="pickTag(t)"
          >#{{ t }}</span>
          <span v-if="!r.tags.length" class="muted">—</span>
        </td>
        <td style="white-space: nowrap">{{ fmtMoney(r.total_amount) }}</td>
        <td class="muted" style="white-space: nowrap">{{ r.total_discount ? fmtMoney(r.total_discount) : '—' }}</td>
        <td style="white-space: nowrap">
          <span v-if="r.has_total_mismatch && !r.reconciliation_ignored_at" class="badge badge-danger">
            ⚠ {{ fmtMoney(Math.abs(r.total_delta)) }}
          </span>
          <span v-else-if="r.has_total_mismatch && r.reconciliation_ignored_at" class="badge">
            Ігноровано
            <span class="muted" style="display: block; font-size: 11px">{{ fmtDate(r.reconciliation_ignored_at) }}</span>
          </span>
          <span v-else class="muted">OK</span>
        </td>
      </tr>
      <tr v-if="!receipts.length">
        <td colspan="10" class="muted">Нічого не знайдено</td>
      </tr>
    </tbody>
  </table>

  <div class="toolbar" style="margin-top: 16px">
    <button class="secondary" :disabled="offset === 0" @click="prevPage">← Назад</button>
    <button class="secondary" :disabled="offset + limit >= total" @click="nextPage">Далі →</button>
  </div>
</template>

<style scoped>
.bulk-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.badge-danger {
  color: var(--danger, #c0392b);
  border: 1px solid color-mix(in srgb, var(--danger, #c0392b) 40%, transparent);
  background: color-mix(in srgb, var(--danger, #c0392b) 10%, transparent);
}
</style>
