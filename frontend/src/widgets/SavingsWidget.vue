<script setup lang="ts">
import { computed, ref } from 'vue'
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
import { fmtShare } from '@/composables/useChartTooltip'
import DiscountToggle from '@/components/DiscountToggle.vue'
import type { DashboardRangeKey, SavingsReceipt, StoreBreakdown } from '@/types/api'

// Where the "Заощаджено"/"бонуси" stat cards come from, broken down per store —
// and, one click deeper, per receipt (/api/dashboard/savings/receipts), so a
// total that looks wrong can be traced to the receipts that produced it.
const props = withDefaults(
  defineProps<{ range: DashboardRangeKey; includeDiscount?: boolean }>(),
  { includeDiscount: true },
)
const emit = defineEmits<{ 'update:includeDiscount': [boolean] }>()

const { data, status } = useWidgetData(
  () => api.dashboard.byStore(rangeParams(props.range, props.includeDiscount)),
  () => [props.range, props.includeDiscount],
)

const dateFromShort = computed(() => rangeDateFromShort(props.range))

/** Pre-discount spend — `total_spent` is already gross when the toggle is off. */
function gross(s: StoreBreakdown): number {
  return props.includeDiscount ? s.total_spent + s.total_discount : s.total_spent
}

const rows = computed(() =>
  (data.value ?? [])
    .filter((s) => s.total_discount || s.total_bonuses || s.total_bonuses_used)
    .map((s) => ({ ...s, gross: gross(s) }))
    .sort((a, b) => b.total_discount + b.total_bonuses_used - (a.total_discount + a.total_bonuses_used)),
)

const totals = computed(() => {
  const all = data.value ?? []
  const sum = (pick: (s: StoreBreakdown) => number) => all.reduce((acc, s) => acc + pick(s), 0)
  return {
    discount: sum((s) => s.total_discount),
    bonuses: sum((s) => s.total_bonuses),
    bonusesUsed: sum((s) => s.total_bonuses_used),
    gross: sum(gross),
  }
})

// ---- drill-down: the receipts behind a number --------------------------- //
type SortKey = 'discount' | 'bonuses' | 'bonuses_used' | 'date'

// Label halves: the modal title reads "чеки з найбільшою <…>", the buttons "За <…>".
const SORT_LABELS: Record<SortKey, string> = {
  discount: 'знижкою',
  bonuses: 'нарахованими бонусами',
  bonuses_used: 'використаними бонусами',
  date: 'датою',
}

const drill = ref<{ store: StoreBreakdown | null; sort: SortKey } | null>(null)
const drillItems = ref<SavingsReceipt[]>([])
const drillStatus = ref<'loading' | 'ready' | 'error'>('loading')

async function loadDrill() {
  const d = drill.value
  if (!d) return
  drillStatus.value = 'loading'
  try {
    drillItems.value = await api.dashboard.savingsReceipts({
      ...rangeParams(props.range, props.includeDiscount),
      ...(d.store ? { store_code: d.store.store_code } : {}),
      sort: d.sort,
      limit: '50',
    })
    drillStatus.value = 'ready'
  } catch {
    drillStatus.value = 'error'
  }
}

function openDrill(store: StoreBreakdown | null, sort: SortKey) {
  drill.value = { store, sort }
  drillItems.value = []
  void loadDrill()
}

function setSort(sort: SortKey) {
  if (!drill.value || drill.value.sort === sort) return
  drill.value = { ...drill.value, sort }
  void loadDrill()
}

const drillTitle = computed(() => {
  const d = drill.value
  if (!d) return ''
  const store = d.store ? d.store.store_name : 'Усі магазини'
  return d.sort === 'date'
    ? `${store} — останні чеки зі знижкою чи бонусами`
    : `${store} — чеки з найбільшою ${SORT_LABELS[d.sort]}`
})

const fmtDay = (iso: string) =>
  new Date(iso).toLocaleDateString('uk-UA', { day: '2-digit', month: '2-digit', year: '2-digit' })

/** Loyalty as a share of the receipt — the sanity check for "бонуси = 50% витрат". */
function pctOfReceipt(r: SavingsReceipt, key: SortKey): string {
  const value =
    key === 'discount' ? r.total_discount : key === 'bonuses_used' ? r.bonuses_used : r.bonuses
  const base = r.total_amount + (props.includeDiscount ? r.total_discount : 0)
  return fmtShare(value, base)
}
</script>

<template>
  <div class="card">
    <div class="widget-head">
      <h3>Заощадження та бонуси</h3>
      <DiscountToggle
        :model-value="includeDiscount"
        @update:model-value="emit('update:includeDiscount', $event)"
      />
    </div>
    <p v-if="status === 'loading'" class="muted">Завантаження…</p>
    <p v-else-if="status === 'error'" class="muted">Не вдалося завантажити.</p>
    <template v-else-if="rows.length">
      <div class="totals">
        <button class="total-card" @click="openDrill(null, 'discount')">
          <div class="stat" :style="{ color: METRIC_COLORS.total_discount }">{{ fmtMoneyFull(totals.discount) }}</div>
          <div class="stat-label">
            Заощаджено на знижках — {{ fmtShare(totals.discount, totals.gross) }} від ціни без знижки
          </div>
        </button>
        <button class="total-card" @click="openDrill(null, 'bonuses')">
          <div class="stat" :style="{ color: METRIC_COLORS.total_bonuses }">{{ fmtMoneyFull(totals.bonuses) }}</div>
          <div class="stat-label">Нараховано бонусів</div>
        </button>
        <button class="total-card" @click="openDrill(null, 'bonuses_used')">
          <div class="stat" :style="{ color: METRIC_COLORS.total_bonuses_used }">{{ fmtMoneyFull(totals.bonusesUsed) }}</div>
          <div class="stat-label">Використано бонусів як оплату</div>
        </button>
      </div>

      <table>
        <thead>
          <tr>
            <th>Магазин</th>
            <th class="num">Знижка</th>
            <th class="num">% знижки</th>
            <th class="num">Нараховано</th>
            <th class="num">Використано</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in rows" :key="s.store_code">
            <td>
              <RouterLink
                :to="{ path: '/receipts', query: { store_code: s.store_code, date_from: dateFromShort } }"
                style="color: inherit"
              >
                {{ s.store_name }}
              </RouterLink>
            </td>
            <td class="num">
              <button class="cell-link" :style="{ color: METRIC_COLORS.total_discount }" @click="openDrill(s, 'discount')">
                {{ fmtMoney(s.total_discount) }}
              </button>
            </td>
            <td class="num muted">{{ fmtShare(s.total_discount, s.gross) }}</td>
            <td class="num">
              <button class="cell-link" :style="{ color: METRIC_COLORS.total_bonuses }" @click="openDrill(s, 'bonuses')">
                {{ fmtMoney(s.total_bonuses) }}
              </button>
            </td>
            <td class="num">
              <button
                class="cell-link"
                :style="{ color: METRIC_COLORS.total_bonuses_used }"
                @click="openDrill(s, 'bonuses_used')"
              >
                {{ fmtMoney(s.total_bonuses_used) }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
      <p class="muted hint">
        Клік по сумі — чеки, які її дають. Знижка — сума знижок у чеках (не залежить від перемикача).
        Бонуси показані в гривнях: мережі, які рахують їх у балах (WOG, UPG), перераховуються
        адаптером. Аномалію видно по колонці «% від чека» у списку.
      </p>
    </template>
    <p v-else class="muted">Немає знижок і бонусів за обраний період.</p>

    <!-- drill-down: receipts behind one number -->
    <div v-if="drill" class="modal-backdrop" @click.self="drill = null">
      <div class="modal" style="width: min(760px, 94vw); max-height: 85vh; overflow: auto">
        <div class="toolbar" style="flex-wrap: nowrap; margin-bottom: 4px">
          <strong style="flex: 1">{{ drillTitle }}</strong>
          <button class="secondary" style="padding: 2px 8px" @click="drill = null">×</button>
        </div>
        <div class="toolbar" style="margin: 0 0 10px">
          <button
            v-for="(label, key) in SORT_LABELS"
            :key="key"
            class="secondary"
            :style="drill.sort === key ? 'background: var(--accent-dim); color: white' : ''"
            @click="setSort(key as SortKey)"
          >
            За {{ label }}
          </button>
        </div>

        <p v-if="drillStatus === 'loading'" class="muted">Завантаження…</p>
        <p v-else-if="drillStatus === 'error'" class="muted">Не вдалося завантажити.</p>
        <p v-else-if="!drillItems.length" class="muted">Немає чеків із такими сумами.</p>
        <table v-else>
          <thead>
            <tr>
              <th>Дата</th>
              <th>Магазин</th>
              <th class="num">Чек</th>
              <th class="num">Знижка</th>
              <th class="num">Нараховано</th>
              <th class="num">Використано</th>
              <th class="num">% від чека</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in drillItems" :key="r.receipt_id">
              <td class="muted" style="white-space: nowrap">{{ fmtDay(r.purchased_at) }}</td>
              <td>
                <RouterLink :to="`/receipts/${r.receipt_id}`" style="color: inherit">
                  {{ r.shop_name || r.store_name }}
                </RouterLink>
              </td>
              <td class="num">{{ fmtMoney(r.total_amount) }}</td>
              <td class="num" :style="{ color: METRIC_COLORS.total_discount }">{{ fmtMoney(r.total_discount) }}</td>
              <td class="num" :style="{ color: METRIC_COLORS.total_bonuses }">{{ fmtMoney(r.bonuses) }}</td>
              <td class="num" :style="{ color: METRIC_COLORS.total_bonuses_used }">{{ fmtMoney(r.bonuses_used) }}</td>
              <td class="num muted">{{ pctOfReceipt(r, drill.sort) }}</td>
            </tr>
          </tbody>
        </table>
        <p class="muted hint">Показано до 50 найбільших чеків за обраний період. Клік по магазину — сам чек.</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.totals {
  display: flex;
  flex-wrap: wrap;
  gap: 24px;
  margin-bottom: 16px;
}

.total-card {
  background: none;
  border: none;
  padding: 0;
  text-align: left;
  color: inherit;
  cursor: pointer;
}

.num {
  text-align: right;
  white-space: nowrap;
}

.cell-link {
  background: none;
  border: none;
  padding: 0;
  font: inherit;
  cursor: pointer;
  text-decoration: underline dotted;
  text-underline-offset: 3px;
}

.hint {
  font-size: 12px;
  margin-bottom: 0;
}
</style>
