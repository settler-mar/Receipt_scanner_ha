<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { api } from '@/lib/api'
import type { Account, BankTransaction, Category, Receipt } from '@/types/api'
import { fmtDateTime } from '@/lib/datetime'
import { IGNORE_PRESETS } from '@/lib/bankTransactionPresets'

const transactions = ref<BankTransaction[]>([])
const bankAccounts = ref<Account[]>([])
const categories = ref<Category[]>([])
const accountFilter = ref('')
const loading = ref(true)
const error = ref('')

// Flat, tree-ordered options with an em-dash indent per depth — same
// convention as ReceiptDetailView's item-category picker.
const categoryOptions = computed(() =>
  [...categories.value]
    .sort((a, b) => a.path.localeCompare(b.path))
    .map((c) => ({ id: c.id, label: `${'— '.repeat(c.path.split('/').length - 1)}${c.name}` })),
)

type Tab = 'all' | 'needs_review' | 'unmatched' | 'matched' | 'ignored'
const tab = ref<Tab>('needs_review')
// The first load picks whichever tab actually has something in it (in this
// priority order) instead of always opening on "needs_review" — that tab is
// empty far more often than not (it's only the ambiguous-match leftovers),
// so a fresh page load looked like "no data arrived" when it was really just
// showing an empty tab while unmatched/matched had rows.
let tabPicked = false
const TAB_PRIORITY: Tab[] = ['needs_review', 'unmatched', 'matched', 'ignored']

const TAB_LABELS: Record<Tab, string> = {
  all: 'Усі',
  needs_review: 'Потребують перевірки',
  unmatched: 'Без чека',
  matched: "Пов'язані",
  ignored: 'Ігноровані',
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [txns, accounts, cats] = await Promise.all([
      api.bankTransactions.list({ accountId: accountFilter.value || undefined }),
      bankAccounts.value.length ? Promise.resolve(bankAccounts.value) : api.accounts.list(),
      categories.value.length ? Promise.resolve(categories.value) : api.categories.list(),
    ])
    transactions.value = txns
    bankAccounts.value = accounts.filter((a) => a.store.capabilities.transactions)
    categories.value = cats
    if (!tabPicked) {
      tabPicked = true
      const firstNonEmpty = TAB_PRIORITY.find((key) => transactions.value.some((t) => t.status === key))
      if (firstNonEmpty) tab.value = firstNonEmpty
    }
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

onMounted(load)

const counts = computed(() => {
  const result: Record<Tab, number> = { all: 0, needs_review: 0, unmatched: 0, matched: 0, ignored: 0 }
  for (const t of transactions.value) { result[t.status as Tab]++; result.all++ }
  return result
})

const visible = computed(() =>
  transactions.value
    .filter((t) => tab.value === 'all' || t.status === tab.value)
    .sort((a, b) => new Date(b.occurred_at).getTime() - new Date(a.occurred_at).getTime()),
)

// --- manual link picker ----------------------------------------------------
const pickerFor = ref<BankTransaction | null>(null)
const pickerResults = ref<Receipt[]>([])
const pickerSearch = ref('')
const pickerBusy = ref(false)

async function openPicker(txn: BankTransaction) {
  pickerFor.value = txn
  pickerSearch.value = ''
  await searchReceipts(txn)
}

async function searchReceipts(txn: BankTransaction) {
  pickerBusy.value = true
  try {
    const occurred = new Date(txn.occurred_at)
    const from = new Date(occurred.getTime() - 3 * 86400000).toISOString()
    const to = new Date(occurred.getTime() + 3 * 86400000).toISOString()
    const list = await api.receipts.list({
      search: pickerSearch.value || undefined,
      date_from: pickerSearch.value ? undefined : from,
      date_to: pickerSearch.value ? undefined : to,
      limit: 20,
    })
    pickerResults.value = list.items
  } finally {
    pickerBusy.value = false
  }
}

function closePicker() {
  pickerFor.value = null
  pickerResults.value = []
}

async function link(receiptId: string) {
  if (!pickerFor.value) return
  const updated = await api.bankTransactions.link(pickerFor.value.id, receiptId)
  applyUpdate(updated)
  closePicker()
}

async function unlink(txn: BankTransaction) {
  applyUpdate(await api.bankTransactions.unlink(txn.id))
}

async function ignore(txn: BankTransaction, reason: string) {
  applyUpdate(await api.bankTransactions.ignore(txn.id, reason))
}

async function setCategory(txn: BankTransaction, categoryId: string) {
  applyUpdate(await api.bankTransactions.setCategory(txn.id, categoryId || null))
}

function applyUpdate(updated: BankTransaction) {
  const idx = transactions.value.findIndex((t) => t.id === updated.id)
  if (idx >= 0) transactions.value[idx] = updated
}

function fmtMoney(value: number) {
  return new Intl.NumberFormat('uk-UA', { style: 'currency', currency: 'UAH' }).format(value)
}
</script>

<template>
  <div class="toolbar">
    <div style="flex: 1">
      <h2 style="margin-bottom: 4px">Банківські транзакції</h2>
      <div class="muted">
        Виписки банків, окремо від чеків — пов'яжіть транзакцію з чеком або позначте
        її як таку, що ніколи чек не отримає (комунальні, поповнення, кишенькові).
        Отримання виписки й розклад налаштовуються на сторінці акаунта.
      </div>
    </div>
    <RouterLink to="/bank-transactions/rules">Правила автоматизації →</RouterLink>
  </div>

  <div v-if="!loading && !bankAccounts.length" class="muted">
    Ще немає жодного банківського підключення. Додайте його на сторінці
    <RouterLink to="/accounts">«Застосунки»</RouterLink> (магазин «Monobank»).
  </div>

  <div v-if="bankAccounts.length > 1" class="toolbar">
    <select v-model="accountFilter" @change="load">
      <option value="">Усі підключення</option>
      <option v-for="a in bankAccounts" :key="a.id" :value="a.id">{{ a.label }}</option>
    </select>
  </div>

  <div class="toolbar" style="flex-wrap: wrap">
    <button
      v-for="key in (['all', 'needs_review', 'unmatched', 'matched', 'ignored'] as Tab[])"
      :key="key"
      class="secondary"
      :style="tab === key ? 'outline: 2px solid var(--accent)' : ''"
      @click="tab = key"
    >
      {{ TAB_LABELS[key] }}: {{ counts[key] }}
    </button>
  </div>

  <div v-if="loading" class="muted">Завантаження…</div>
  <div v-else-if="error" style="color: var(--danger)">{{ error }}</div>
  <div v-else-if="!visible.length" class="muted">Тут поки що порожньо.</div>

  <table v-else class="table">
    <thead>
      <tr>
        <th>Дата</th>
        <th v-if="bankAccounts.length > 1">Підключення</th>
        <th>Опис</th>
        <th>Сума</th>
        <th>Категорія</th>
        <th>Дії</th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="txn in visible" :key="txn.id">
        <td>{{ fmtDateTime(txn.occurred_at) }}</td>
        <td v-if="bankAccounts.length > 1">{{ txn.account_label }}</td>
        <td>
          {{ txn.description || '—' }}
          <div v-if="txn.counterparty_raw?.comment" class="muted" style="font-size: 12px">
            {{ txn.counterparty_raw.comment }}
          </div>
        </td>
        <td>{{ fmtMoney(txn.amount) }}</td>
        <td>
          <select
            :value="txn.category_override_id ?? ''"
            style="max-width: 160px"
            @change="setCategory(txn, ($event.target as HTMLSelectElement).value)"
          >
            <option value="">— не задано —</option>
            <option v-for="o in categoryOptions" :key="o.id" :value="o.id">{{ o.label }}</option>
          </select>
        </td>
        <td>
          <div class="toolbar" style="flex-wrap: wrap; gap: 6px">
            <template v-if="txn.status === 'matched'">
              <RouterLink :to="`/receipts/${txn.linked_receipt_id}`">Відкрити чек</RouterLink>
              <button class="secondary" @click="unlink(txn)">Відв'язати</button>
            </template>
            <template v-else>
              <button class="secondary" @click="openPicker(txn)">Пов'язати з чеком</button>
              <select @change="ignore(txn, ($event.target as HTMLSelectElement).value)">
                <option value="" disabled selected>Ігнорувати як…</option>
                <option v-for="preset in IGNORE_PRESETS" :key="preset.value" :value="preset.value">
                  {{ preset.label }}
                </option>
              </select>
            </template>
          </div>
        </td>
      </tr>
    </tbody>
  </table>

  <div v-if="pickerFor" class="modal-backdrop" @click.self="closePicker">
    <div class="card" style="max-width: 560px; margin: 40px auto">
      <h3 style="margin-top: 0">Пов'язати з чеком</h3>
      <div class="toolbar">
        <input
          v-model="pickerSearch"
          placeholder="Пошук за назвою магазину/товару"
          @keyup.enter="searchReceipts(pickerFor)"
        />
        <button class="secondary" @click="searchReceipts(pickerFor)">Пошук</button>
      </div>
      <div v-if="pickerBusy" class="muted">Пошук…</div>
      <div v-else-if="!pickerResults.length" class="muted">Нічого не знайдено поруч за датою.</div>
      <ul v-else style="list-style: none; padding: 0; margin: 0">
        <li
          v-for="r in pickerResults"
          :key="r.id"
          style="display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid var(--border)"
        >
          <span>{{ r.shop_name || r.store_code }} · {{ fmtDateTime(r.purchased_at) }}</span>
          <span style="display: flex; gap: 10px; align-items: center">
            {{ fmtMoney(r.total_amount) }}
            <button @click="link(r.id)">Обрати</button>
          </span>
        </li>
      </ul>
      <div class="toolbar" style="margin-top: 12px">
        <button class="secondary" @click="closePicker">Закрити</button>
      </div>
    </div>
  </div>
</template>
