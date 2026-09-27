<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { api } from '@/lib/api'
import LoyaltyCodeDisplay from '@/components/LoyaltyCodeDisplay.vue'
import { storeFallbackIcon, storeLogoUrl } from '@/lib/storeIcons'
import type { Account, CollectionRun, LoyaltyCode, Store } from '@/types/api'
import { fmtDateTime } from '@/lib/datetime'

const router = useRouter()

const accounts = ref<Account[]>([])
const stores = ref<Store[]>([])
const loading = ref(true)
const showCatalog = ref(false)
const showCreate = ref(false)
const integrationSearch = ref('')
const integrationClass = ref('all')
const collecting = ref<string | null>(null)
const resettingError = ref<string | null>(null)

// Результат останнього ручного збору по акаунту. Без нього кнопка просто
// блимала: сторінка перезавантажувалась, а скільки чеків приїхало (і чи
// приїхало взагалі) видно не було.
type RunResult = { kind: 'ok' | 'empty' | 'warn' | 'error'; text: string }
const runResults = ref<Record<string, RunResult>>({})

function describeRun(run: CollectionRun): RunResult {
  if (run.status === 'error') {
    return { kind: 'error', text: run.error_message || 'Збір завершився помилкою' }
  }
  const seen = run.receipts_found ? `, переглянуто ${run.receipts_found}` : ''
  if (run.status === 'partial') {
    return {
      kind: 'warn',
      text: `Частково: +${run.receipts_new}${seen}. ${run.error_message ?? ''}`.trim(),
    }
  }
  if (!run.receipts_new) return { kind: 'empty', text: `Нових чеків немає${seen}` }
  return { kind: 'ok', text: `+${run.receipts_new} нових${seen}` }
}

const RESULT_BADGE: Record<RunResult['kind'], string> = {
  ok: 'badge-active',
  empty: 'badge-pending_otp',
  warn: 'badge-warning',
  error: 'badge-error',
}

// Bulk "refresh receipts" — runs the existing per-account collect sequentially.
const selectedIds = ref<Set<string>>(new Set())
const bulkRunning = ref(false)
const bulkDone = ref(0)
const bulkTotal = ref(0)
const bulkCurrent = ref('')
const bulkSummary = ref('')

// Mass actions for the "Потребують уваги" block. They deliberately reuse the
// existing per-account endpoints and run sequentially so the provider is not
// hit by 20 simultaneous retries.
type ProblemAction = 'reset' | 'retry'
type ProblemActionResult = { kind: 'ok' | 'error'; text: string }
type ProblemActionResultItem = ProblemActionResult & { id: string; label: string }
const selectedProblemIds = ref<Set<string>>(new Set())
const problemActionRunning = ref(false)
const problemAction = ref<ProblemAction | null>(null)
const problemActionDone = ref(0)
const problemActionTotal = ref(0)
const problemActionCurrent = ref('')
const problemActionSummary = ref('')
const problemActionResults = ref<Record<string, ProblemActionResult>>({})
const problemActionResultItems = ref<ProblemActionResultItem[]>([])

const form = ref({ store_code: '', label: '', phone_number: '' })

const codeModalAccount = ref<Account | null>(null)
const loyaltyCode = ref<LoyaltyCode | null>(null)
const loyaltyLoading = ref(false)
const loyaltyError = ref('')

// Purely cosmetic grouping/iconography — not a backend concept, just how the
// catalog visually organizes stores that otherwise all look the same in a
// flat list. An unrecognized new store falls into "other" with a generic
// icon, so nothing here *must* be updated just to register one (see
// registry.py/seed.py) — but it's worth adding for a nicer catalog entry.
type Category = 'fuel' | 'diy' | 'electronics' | 'home' | 'pet' | 'clothing' | 'marketplace' | 'grocery' | 'pharmacy' | 'manual' | 'other'

const CATEGORY_LABELS: Record<Category, string> = {
  fuel: 'АЗС',
  diy: 'Строймагазины',
  electronics: 'Магазины техники',
  home: 'Магазины для дома',
  pet: 'Зоомагазины',
  clothing: 'Магазины одежды',
  marketplace: 'Маркетплейсы',
  grocery: 'Продуктовые магазины',
  pharmacy: 'Аптеки',
  manual: 'Ручное добавление',
  other: 'Другое',
}
const CATEGORY_ORDER: Category[] = ['fuel', 'diy', 'electronics', 'home', 'pet', 'clothing', 'marketplace', 'grocery', 'pharmacy', 'manual', 'other']

const STORE_CATEGORY: Record<string, Category> = {
  wog: 'fuel', ukrnafta: 'fuel', bars: 'fuel', brsm: 'fuel', upg: 'fuel', klo: 'fuel',
  epicentrk: 'diy', 'dnipro-m': 'diy',
  foxtrot: 'electronics', vencon: 'electronics', brain: 'electronics',
  prom: 'marketplace', rozetka: 'marketplace', aliexpress: 'marketplace',
  atb: 'grocery', fozzy: 'grocery', fora: 'grocery', silpo: 'grocery', metro: 'grocery',
  scanner: 'manual',
}

// Logo URLs/emoji fallback live in lib/storeIcons.ts, shared with ReceiptsView.

function storeCategory(store: Store): Category {
  const known = STORE_CATEGORY[store.code]
  if (known) return known
  if (store.capabilities.manual_receipts) return 'manual'
  if (store.capabilities.fuel_prices || store.auth_type === 'card_number') return 'fuel'
  if (store.network_type === 'azs') return 'fuel'
  if (store.network_type === 'tech') return 'electronics'
  if (store.network_type === 'home') return 'home'
  if (store.network_type === 'supermarket') return 'grocery'
  if (store.network_type === 'pharmacy') return 'pharmacy'
  return 'other'
}

const normalizedSearch = computed(() => integrationSearch.value.trim().toLocaleLowerCase('uk-UA'))

function matchesSearch(store: Store, query: string) {
  if (!query) return true
  return `${store.name} ${store.code}`.toLocaleLowerCase('uk-UA').includes(query)
}

function matchesClass(store: Store) {
  return integrationClass.value === 'all' || storeCategory(store) === integrationClass.value
}

function matchesAccount(account: Account) {
  return matchesSearch(account.store, normalizedSearch.value)
    || account.label.toLocaleLowerCase('uk-UA').includes(normalizedSearch.value)
}

const filteredStores = computed(() => stores.value.filter((s) => matchesClass(s) && matchesSearch(s, normalizedSearch.value)))
const filteredAccounts = computed(() => accounts.value.filter((a) => matchesClass(a.store) && matchesAccount(a)))

const storesByCategory = computed(() => {
  const groups = new Map<Category, Store[]>()
  for (const s of filteredStores.value) {
    const cat = storeCategory(s)
    if (!groups.has(cat)) groups.set(cat, [])
    groups.get(cat)!.push(s)
  }
  return CATEGORY_ORDER.filter((c) => groups.has(c)).map((c) => ({
    category: c,
    stores: groups.get(c)!.sort((a, b) => a.name.localeCompare(b.name, 'uk')),
  }))
})

const accountsByCategory = computed(() => {
  const groups = new Map<Category, Account[]>()
  for (const a of filteredAccounts.value) {
    const cat = storeCategory(a.store)
    if (!groups.has(cat)) groups.set(cat, [])
    groups.get(cat)!.push(a)
  }
  return CATEGORY_ORDER.filter((c) => groups.has(c)).map((c) => ({
    category: c,
    accounts: groups.get(c)!.sort((a, b) => a.store.name.localeCompare(b.store.name, 'uk') || a.label.localeCompare(b.label, 'uk')),
  }))
})

const availableClasses = computed(() => CATEGORY_ORDER.filter((c) => stores.value.some((s) => storeCategory(s) === c)))

async function showCode(account: Account) {
  codeModalAccount.value = account
  loyaltyCode.value = null
  loyaltyError.value = ''
  loyaltyLoading.value = true
  try {
    loyaltyCode.value = await api.accounts.loyaltyCode(account.id)
  } catch (e) {
    loyaltyError.value = (e as Error).message
  } finally {
    loyaltyLoading.value = false
  }
}

function closeCodeModal() {
  codeModalAccount.value = null
}

// silent=true — оновлення даних без «миготіння» сторінки (таблиця ховається
// під v-if="loading"), щоб після ручного збору результат лишався на екрані.
async function load(silent = false) {
  if (!silent) loading.value = true
  ;[accounts.value, stores.value] = await Promise.all([api.accounts.list(), api.stores.list()])
  loading.value = false
}

onMounted(() => load())

// Підпис поля логіну для магазина, обраного у формі створення: адаптер сам
// каже, що він приймає (див. BaseAdapter.login_field).
const createLoginField = computed(
  () => stores.value.find((s) => s.code === form.value.store_code)?.login_field ?? 'phone',
)
const CREATE_LOGIN_LABELS: Record<string, string> = {
  email: 'Email',
  phone_or_email: 'Телефон або email',
  phone: 'Телефон (+380...)',
}
const createLoginLabel = computed(
  () => CREATE_LOGIN_LABELS[createLoginField.value] ?? 'Телефон (+380...)',
)

function openCreate(storeCode = '') {
  form.value = { store_code: storeCode, label: '', phone_number: '' }
  showCreate.value = true
  showCatalog.value = false
}

async function createAccount() {
  if (!form.value.store_code || !form.value.label) return
  const account = await api.accounts.create({
    store_code: form.value.store_code,
    label: form.value.label,
    phone_number: form.value.phone_number || undefined,
  })
  showCreate.value = false
  // Одразу відкриваємо нову інтеграцію: далі майже завжди йде логін/OTP.
  router.push({ name: 'account-detail', params: { id: account.id } })
}

async function removeAccount(id: string) {
  if (!confirm('Видалити акаунт разом з усіма зібраними чеками?')) return
  await api.accounts.remove(id)
  await load()
}

async function collectNow(id: string) {
  collecting.value = id
  runResults.value = { ...runResults.value, [id]: { kind: 'warn', text: 'Збираємо…' } }
  try {
    const started = await api.accounts.collectNow(id)
    const run = await waitForRun(id, started.id)
    runResults.value = { ...runResults.value, [id]: describeRun(run) }
    await load(true)
  } catch (e) {
    runResults.value = { ...runResults.value, [id]: { kind: 'error', text: (e as Error).message } }
  } finally {
    collecting.value = null
  }
}

async function waitForRun(accountId: string, runId: string): Promise<CollectionRun> {
  for (let attempt = 0; attempt < 600; attempt += 1) {
    const current = (await api.accounts.runs(accountId)).find((run) => run.id === runId)
    if (current && current.status !== 'running') return current
    await new Promise((resolve) => setTimeout(resolve, 500))
  }
  throw new Error('Збір ще виконується — перевірте історію запусків пізніше.')
}

async function resetError(id: string) {
  resettingError.value = id
  try {
    await api.accounts.resetError(id)
    await load()
  } catch (e) {
    alert((e as Error).message)
  } finally {
    resettingError.value = null
  }
}

async function resetAndRetry(id: string) {
  resettingError.value = id
  try {
    await api.accounts.resetError(id)
  } catch (e) {
    alert((e as Error).message)
    return
  } finally {
    resettingError.value = null
  }
  await collectNow(id)
}

function toggleProblemSelected(id: string) {
  const next = new Set(selectedProblemIds.value)
  next.has(id) ? next.delete(id) : next.add(id)
  selectedProblemIds.value = next
}

function toggleAllProblems() {
  const ids = problemAccounts.value.map((a) => a.id)
  const allSelected = ids.length > 0 && ids.every((id) => selectedProblemIds.value.has(id))
  selectedProblemIds.value = allSelected ? new Set() : new Set(ids)
}

const selectedProblemCount = computed(() =>
  problemAccounts.value.filter((a) => selectedProblemIds.value.has(a.id)).length,
)

async function runProblemAction(action: ProblemAction, ids: string[]) {
  if (problemActionRunning.value || !ids.length) return
  problemActionRunning.value = true
  problemAction.value = action
  problemActionDone.value = 0
  problemActionTotal.value = ids.length
  problemActionCurrent.value = ''
  problemActionSummary.value = ''
  problemActionResults.value = {}
  problemActionResultItems.value = []
  let succeeded = 0
  let failed = 0

  for (const id of ids) {
    const account = accounts.value.find((a) => a.id === id)
    problemActionCurrent.value = account?.label ?? ''
    try {
      await api.accounts.resetError(id)
      if (action === 'retry') {
        const started = await api.accounts.collectNow(id)
        const run = await waitForRun(id, started.id)
        const result = describeRun(run)
        if (run.status === 'error') {
          failed++
          const item = { id, label: account?.label ?? id, kind: 'error' as const, text: result.text }
          problemActionResults.value = {
            ...problemActionResults.value,
            [id]: item,
          }
          problemActionResultItems.value = [...problemActionResultItems.value, item]
        } else {
          succeeded++
          const item = { id, label: account?.label ?? id, kind: 'ok' as const, text: result.text }
          problemActionResults.value = {
            ...problemActionResults.value,
            [id]: item,
          }
          problemActionResultItems.value = [...problemActionResultItems.value, item]
        }
      } else {
        succeeded++
        const item = { id, label: account?.label ?? id, kind: 'ok' as const, text: 'Помилку скинуто' }
        problemActionResults.value = {
          ...problemActionResults.value,
          [id]: item,
        }
        problemActionResultItems.value = [...problemActionResultItems.value, item]
      }
    } catch (e) {
      failed++
      const item = { id, label: account?.label ?? id, kind: 'error' as const, text: (e as Error).message }
      problemActionResults.value = {
        ...problemActionResults.value,
        [id]: item,
      }
      problemActionResultItems.value = [...problemActionResultItems.value, item]
    }
    problemActionDone.value++
  }

  problemActionRunning.value = false
  problemActionCurrent.value = ''
  problemActionSummary.value = failed
    ? `Готово: успішно — ${succeeded}, з помилками — ${failed} з ${ids.length}.`
    : `Готово: оброблено ${succeeded} з ${ids.length}.`
  selectedProblemIds.value = new Set()
  await load(true)
}

function resetSelectedProblems() {
  runProblemAction('reset', [...selectedProblemIds.value])
}

function retrySelectedProblems() {
  runProblemAction('retry', [...selectedProblemIds.value])
}

function resetAllProblems() {
  runProblemAction('reset', problemAccounts.value.map((a) => a.id))
}

function retryAllProblems() {
  runProblemAction('retry', problemAccounts.value.map((a) => a.id))
}

const collectableAccounts = computed(() =>
  filteredAccounts.value.filter((a) => a.store.capabilities.receipts && a.status === 'active'),
)

function toggleSelected(id: string) {
  const next = new Set(selectedIds.value)
  next.has(id) ? next.delete(id) : next.add(id)
  selectedIds.value = next
}

function toggleGroup(ids: string[]) {
  const next = new Set(selectedIds.value)
  const allOn = ids.every((id) => next.has(id))
  for (const id of ids) allOn ? next.delete(id) : next.add(id)
  selectedIds.value = next
}

function groupChecked(ids: string[]) {
  return ids.length > 0 && ids.every((id) => selectedIds.value.has(id))
}

async function collectMany(ids: string[], forceAll = false) {
  if (bulkRunning.value || !ids.length) return
  bulkRunning.value = true
  bulkTotal.value = ids.length
  bulkDone.value = 0
  bulkSummary.value = ''
  runResults.value = {}
  let failed = 0
  let added = 0
  for (const id of ids) {
    const acc = accounts.value.find((a) => a.id === id)
    bulkCurrent.value = acc?.label ?? ''
    collecting.value = id
    runResults.value = { ...runResults.value, [id]: { kind: 'warn', text: 'Збираємо…' } }
    try {
      const started = forceAll ? await api.accounts.collectAll(id) : await api.accounts.collectNow(id)
      const run = await waitForRun(id, started.id)
      runResults.value = { ...runResults.value, [id]: describeRun(run) }
      added += run.receipts_new
      if (run.status === 'error') failed++
    } catch (e) {
      runResults.value = { ...runResults.value, [id]: { kind: 'error', text: (e as Error).message } }
      failed++
    }
    bulkDone.value++
  }
  collecting.value = null
  bulkRunning.value = false
  bulkCurrent.value = ''
  bulkSummary.value = failed
    ? `Готово: +${added} нових чеків, з помилками — ${failed} з ${ids.length}. Деталі в рядках нижче.`
    : `Готово: +${added} нових чеків з ${ids.length} акаунтів.`
  await load(true)
}

function collectAll() {
  collectMany(collectableAccounts.value.map((a) => a.id))
}

function collectAllHistory() {
  collectMany(collectableAccounts.value.map((a) => a.id), true)
}

function collectSelected() {
  const ids = collectableAccounts.value.map((a) => a.id).filter((id) => selectedIds.value.has(id))
  collectMany(ids)
}

function addReceiptFor(accountId: string) {
  router.push({ name: 'receipt-new', query: { account_id: accountId } })
}

const statusLabel: Record<string, string> = {
  active: 'активний',
  pending_otp: 'очікує код',
  needs_reauth: 'потрібен вхід',
  error: 'помилка',
}

// Integrations that need the user: the badge in the menu counts exactly these,
// and this is the page it points at, so the list has to be here too.
const problemAccounts = computed(() =>
  accounts.value.filter(
    (a) =>
      // Manual stores (сканер/OCR) have nothing to collect and nothing to log
      // into — an old failed run there is not something the user can fix.
      a.store.capabilities.receipts &&
      (a.status === 'needs_reauth' || a.status === 'error' || (a.last_run_failed && !!a.last_error)),
  ),
)
function problemReason(a: Account): string {
  if (a.status === 'needs_reauth' || a.status === 'error') {
    return a.last_error || 'Потрібен повторний вхід'
  }
  return a.last_failed_error || 'Останній збір завершився помилкою'
}

function accountStatusLabel(account: Account) {
  if (account.store.auth_type === 'phone_password' && account.status === 'pending_otp') return 'потрібен вхід'
  return statusLabel[account.status] ?? account.status
}

function fmtDate(v: string | null) {
  return fmtDateTime(v)
}

function fmtMoney(v: number) {
  return new Intl.NumberFormat('uk-UA', { style: 'currency', currency: 'UAH' }).format(v)
}
</script>

<template>
  <div class="toolbar">
    <h2 style="margin: 0; flex: 1">Підключення</h2>
    <button @click="showCatalog = !showCatalog">{{ showCatalog ? 'Закрити каталог' : '+ Додати застосунок' }}</button>
  </div>

  <div class="card" style="margin: 16px 0; display: flex; flex-wrap: wrap; gap: 10px; align-items: center">
    <input
      v-model="integrationSearch"
      placeholder="Пошук інтеграції або акаунта…"
      aria-label="Пошук інтеграції або акаунта"
      style="min-width: 240px; flex: 1"
    />
    <select v-model="integrationClass" aria-label="Клас інтеграції" style="min-width: 190px">
      <option value="all">Усі класи</option>
      <option v-for="category in availableClasses" :key="category" :value="category">{{ CATEGORY_LABELS[category] }}</option>
    </select>
    <button v-if="integrationSearch || integrationClass !== 'all'" class="secondary" @click="integrationSearch = ''; integrationClass = 'all'">Скинути</button>
  </div>

  <div v-if="loading" class="muted">Завантаження…</div>
  <template v-else>
    <div v-if="showCatalog" class="card" style="margin-bottom: 24px">
      <h3 style="margin-top: 0">Каталог застосунків</h3>
      <p v-if="!filteredStores.length" class="muted">Нічого не знайдено.</p>
      <div v-for="group in storesByCategory" :key="group.category" style="margin-bottom: 20px">
        <h4 style="margin-bottom: 10px">{{ CATEGORY_LABELS[group.category] }}</h4>
        <div class="card-grid">
          <button
            v-for="s in group.stores"
            :key="s.code"
            class="card"
            style="
              text-align: left;
              cursor: pointer;
              display: flex;
              align-items: center;
              gap: 12px;
              border: none;
              font: inherit;
              color: inherit;
            "
            @click="openCreate(s.code)"
          >
            <img
              v-if="storeLogoUrl(s.code)"
              :src="storeLogoUrl(s.code)!"
              alt=""
              width="28"
              height="28"
              style="border-radius: 6px; flex-shrink: 0"
            />
            <span v-else style="font-size: 28px; line-height: 1">{{ storeFallbackIcon(s.code) }}</span>
            <span style="flex: 1">
              <div style="font-weight: 600; display: flex; align-items: center; gap: 7px; flex-wrap: wrap">
                {{ s.name }}
                <span class="badge integration-class-badge">{{ CATEGORY_LABELS[storeCategory(s)] }}</span>
              </div>
              <div class="muted" style="font-size: 12px">+ Додати акаунт</div>
            </span>
          </button>
        </div>
      </div>
    </div>

    <h2 style="margin: 32px 0 12px">Мої акаунти</h2>
    <p v-if="!accounts.length" class="muted">Ще немає жодного акаунта — натисніть «Додати застосунок» вище.</p>
    <p v-else-if="!filteredAccounts.length" class="muted">За заданими фільтрами підключень не знайдено.</p>

    <div v-if="problemAccounts.length || problemActionSummary || problemActionResultItems.length" class="card" style="margin-bottom: 20px; border-color: var(--danger)">
      <div v-if="problemAccounts.length" style="display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 8px">
        <h3 style="margin: 0; flex: 1">Потребують уваги ({{ problemAccounts.length }})</h3>
        <label class="muted" style="display: inline-flex; align-items: center; gap: 5px; font-size: 12px">
          <input
            type="checkbox"
            style="width: auto"
            :checked="selectedProblemCount === problemAccounts.length"
            :disabled="problemActionRunning"
            @change="toggleAllProblems"
          />
          Вибрати всі
        </label>
        <button
          class="secondary"
          :disabled="problemActionRunning || !selectedProblemCount"
          @click="resetSelectedProblems"
        >
          Скинути вибрані ({{ selectedProblemCount }})
        </button>
        <button
          class="secondary"
          :disabled="problemActionRunning || !selectedProblemCount"
          @click="retrySelectedProblems"
        >
          Скинути й повторити ({{ selectedProblemCount }})
        </button>
        <button
          class="secondary"
          :disabled="problemActionRunning"
          @click="resetAllProblems"
        >
          Скинути всі
        </button>
        <button
          class="secondary"
          :disabled="problemActionRunning"
          @click="retryAllProblems"
        >
          Скинути й повторити всі
        </button>
      </div>
      <div v-if="problemActionRunning" style="margin: 8px 0; padding: 10px 12px; border-radius: 8px; background: var(--surface-2, rgba(255,255,255,.05))">
        <div style="display: flex; justify-content: space-between; gap: 10px; font-weight: 600">
          <span>{{ problemAction === 'retry' ? 'Скидання та повторна спроба' : 'Скидання помилок' }}</span>
          <span>{{ problemActionDone }} / {{ problemActionTotal }}</span>
        </div>
        <div class="muted" style="margin-top: 4px">
          Зараз обробляється:
          <strong>{{ problemActionCurrent || 'підготовка…' }}</strong>
        </div>
      </div>
      <div v-else-if="problemActionSummary" class="muted" style="margin-bottom: 8px">
        {{ problemActionSummary }}
      </div>
      <div v-if="problemActionResultItems.length" style="margin: 8px 0 10px; display: grid; gap: 4px">
        <strong style="font-size: 12px">Результати обробки</strong>
        <div
          v-for="(result, index) in problemActionResultItems"
          :key="`${result.id}-${index}`"
          class="muted"
          style="display: flex; gap: 8px; align-items: baseline; font-size: 12px"
        >
          <span :style="{ color: result.kind === 'ok' ? 'var(--success)' : 'var(--danger)' }">{{ result.kind === 'ok' ? '✓' : '✕' }}</span>
          <strong>{{ index + 1 }}. {{ result.label }}</strong>
          <span>— {{ result.text }}</span>
        </div>
      </div>
      <div
        v-for="a in problemAccounts"
        :key="a.id"
        style="display: flex; flex-wrap: wrap; row-gap: 4px; gap: 10px; align-items: baseline; padding: 5px 0; border-bottom: 1px solid var(--border)"
      >
        <input
          type="checkbox"
          style="width: auto; flex: none"
          :checked="selectedProblemIds.has(a.id)"
          :disabled="problemActionRunning"
          :aria-label="`Вибрати ${a.store.name} · ${a.label}`"
          @change="toggleProblemSelected(a.id)"
        />
        <RouterLink :to="`/accounts/${a.id}`" style="flex: 0 0 210px">{{ a.store.name }} · {{ a.label }}</RouterLink>
        <span class="muted" style="flex: 1 1 200px; font-size: 12px">{{ problemReason(a) }}</span>
        <span v-if="a.failed_runs_24h" class="muted" style="flex: none; font-size: 12px">
          невдалих за добу: {{ a.failed_runs_24h }}
        </span>
        <span class="badge" :class="`badge-${a.status}`" style="flex: none">{{ accountStatusLabel(a) }}</span>
        <span
          v-if="problemActionResults[a.id]"
          class="badge"
          :class="problemActionResults[a.id]!.kind === 'ok' ? 'badge-active' : 'badge-error'"
          :title="problemActionResults[a.id]!.text"
          style="flex: none"
        >
          {{ problemActionResults[a.id]!.text }}
        </span>
        <button
          class="secondary"
          style="flex: none; font-size: 12px; padding: 2px 8px"
          :disabled="resettingError === a.id || collecting === a.id || problemActionRunning"
          @click="resetError(a.id)"
        >
          {{ resettingError === a.id ? 'Скидаємо…' : 'Скинути помилку' }}
        </button>
        <button
          v-if="a.store.capabilities.receipts"
          class="secondary"
          style="flex: none; font-size: 12px; padding: 2px 8px"
          :disabled="resettingError === a.id || collecting === a.id || problemActionRunning"
          @click="resetAndRetry(a.id)"
        >
          {{ resettingError === a.id ? 'Скидаємо…' : collecting === a.id ? 'Збір…' : 'Скинути й повторити' }}
        </button>
      </div>
    </div>

    <div v-if="collectableAccounts.length" class="card" style="margin-bottom: 20px; display: flex; flex-wrap: wrap; align-items: center; gap: 10px">
      <strong>Оновити чеки</strong>
      <button :disabled="bulkRunning" @click="collectAll">Оновити всі ({{ collectableAccounts.length }})</button>
      <button class="secondary" :disabled="bulkRunning" @click="collectAllHistory">Завантажити всю історію</button>
      <button
        class="secondary"
        :disabled="bulkRunning || !selectedIds.size"
        @click="collectSelected"
      >
        Оновити вибрані ({{ collectableAccounts.filter((a) => selectedIds.has(a.id)).length }})
      </button>
      <span v-if="bulkRunning" class="muted">
        {{ bulkDone }} / {{ bulkTotal }} — {{ bulkCurrent }}…
      </span>
      <span v-else-if="bulkSummary">{{ bulkSummary }}</span>
      <span class="muted" style="flex-basis: 100%; font-size: 12px">
        Позначте акаунти галочками, щоб оновити лише частину. Оновлення йде послідовно, один за одним.
      </span>
    </div>

    <div v-for="group in accountsByCategory" :key="group.category" style="margin-bottom: 28px">
      <h3 style="margin-bottom: 10px; display: flex; align-items: center; gap: 8px">
        <input
          v-if="group.accounts.some((a) => a.store.capabilities.receipts && a.status === 'active')"
          type="checkbox"
          style="width: auto"
          :checked="groupChecked(group.accounts.filter((a) => a.store.capabilities.receipts && a.status === 'active').map((a) => a.id))"
          :disabled="bulkRunning"
          title="Позначити всі в групі"
          @change="toggleGroup(group.accounts.filter((a) => a.store.capabilities.receipts && a.status === 'active').map((a) => a.id))"
        />
        {{ CATEGORY_LABELS[group.category] }} <span class="muted" style="font-size: 12px">({{ group.accounts.length }})</span>
      </h3>
      <table>
        <thead>
          <tr>
            <th></th>
            <th>Назва</th>
            <th>Магазин</th>
            <th>Логін</th>
            <th>Статус</th>
            <th>Чеків</th>
            <th>Баланс бонусів</th>
            <th>Останній збір</th>
            <th>Розклад</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="a in group.accounts" :key="a.id">
            <td>
              <input
                v-if="a.store.capabilities.receipts && a.status === 'active'"
                type="checkbox"
                style="width: auto"
                :checked="selectedIds.has(a.id)"
                :disabled="bulkRunning"
                @change="toggleSelected(a.id)"
              />
            </td>
            <td>
              <RouterLink
                :to="`/accounts/${a.id}`"
                style="display: inline-flex; align-items: center; gap: 6px"
              >
                <img
                  v-if="storeLogoUrl(a.store.code)"
                  :src="storeLogoUrl(a.store.code)!"
                  alt=""
                  width="16"
                  height="16"
                  style="border-radius: 3px"
                />
                <span v-else>{{ storeFallbackIcon(a.store.code) }}</span>
                {{ a.label }}
              </RouterLink>
            </td>
            <td>
              <div style="display: flex; align-items: center; gap: 7px; flex-wrap: wrap">
                {{ a.store.name }}
                <span class="badge integration-class-badge">{{ CATEGORY_LABELS[storeCategory(a.store)] }}</span>
              </div>
            </td>
            <td>{{ a.phone_number ?? '—' }}</td>
            <td>
              <span class="badge" :class="`badge-${a.status}`">{{ accountStatusLabel(a) }}</span>
              <!-- A green status says nothing about scheduled runs: they can be
                   failing nightly while a manual collect keeps working. -->
              <RouterLink
                v-if="a.failed_runs_24h"
                to="/logs"
                class="badge"
                :class="a.last_run_failed ? 'badge-error' : 'badge-warning'"
                style="margin-left: 6px"
                :title="a.last_run_failed
                  ? `Останній збір впав. Невдалих за добу: ${a.failed_runs_24h}. ${a.last_failed_error ?? ''}`
                  : `Зараз працює, але за добу впало ${a.failed_runs_24h} запуск(ів). ${a.last_failed_error ?? ''}`"
              >
                ⚠ {{ a.failed_runs_24h }}
              </RouterLink>
            </td>
            <td>{{ a.receipt_count }}</td>
            <td>
              <template v-if="a.store.capabilities.balance">
                {{ a.bonus_balance !== null ? fmtMoney(a.bonus_balance) : '—' }}
                <span v-if="a.discount_percent !== null" class="muted">({{ a.discount_percent }}%)</span>
              </template>
              <template v-else>не підтримується</template>
            </td>
            <td class="muted">{{ a.store.capabilities.receipts ? fmtDate(a.last_collected_at) : '—' }}</td>
            <td class="muted">
              {{
                !a.store.capabilities.receipts ? '—' : a.schedule?.enabled ? a.schedule.cron_expression : 'вимкнено'
              }}
            </td>
            <td style="display: flex; gap: 6px; flex-wrap: wrap; align-items: center">
              <button
                v-if="a.last_error || a.status === 'needs_reauth' || a.status === 'error'"
                class="secondary"
                :disabled="resettingError === a.id || collecting === a.id"
                @click="resetError(a.id)"
              >
                {{ resettingError === a.id ? 'Скидаємо…' : 'Скинути помилку' }}
              </button>
              <button
                v-if="(a.last_error || a.status === 'needs_reauth' || a.status === 'error') && a.store.capabilities.receipts"
                class="secondary"
                :disabled="resettingError === a.id || collecting === a.id"
                @click="resetAndRetry(a.id)"
              >
                {{
                  resettingError === a.id ? 'Скидаємо…' : collecting === a.id ? 'Збір…' : 'Скинути й повторити'
                }}
              </button>
              <button
                v-if="a.store.capabilities.receipts && a.status === 'active'"
                class="secondary"
                :disabled="collecting === a.id"
                @click="collectNow(a.id)"
              >
                {{ collecting === a.id ? 'Збір…' : 'Зібрати зараз' }}
              </button>
              <span
                v-if="runResults[a.id]"
                class="badge"
                :class="RESULT_BADGE[runResults[a.id]!.kind]"
                :title="runResults[a.id]!.text"
                style="max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap"
              >
                {{ runResults[a.id]!.text }}
              </span>
              <button v-if="a.store.capabilities.manual_receipts" class="secondary" @click="addReceiptFor(a.id)">
                + Додати чек
              </button>
              <button
                v-if="a.status === 'active' && a.store.capabilities.loyalty_code"
                class="secondary"
                @click="showCode(a)"
              >
                Код картки
              </button>
              <button class="danger" @click="removeAccount(a.id)">Видалити</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </template>

  <div v-if="showCreate" class="modal-backdrop" @click.self="showCreate = false">
    <div class="modal">
      <h3 style="margin-top: 0">Новий акаунт</h3>
      <div class="form-row">
        <label>Магазин</label>
        <select v-model="form.store_code">
          <option value="" disabled>Оберіть…</option>
          <option v-for="s in stores" :key="s.code" :value="s.code">{{ s.name }}</option>
        </select>
      </div>
      <div class="form-row">
        <label>Назва (наприклад "АТБ Максим")</label>
        <input v-model="form.label" />
      </div>
      <div class="form-row">
        <label>{{ createLoginLabel }}</label>
        <input
          v-model="form.phone_number"
          :placeholder="createLoginField === 'email' ? 'you@example.com' : 'необов\'язково'"
        />
      </div>
      <div class="toolbar" style="margin-bottom: 0">
        <button @click="createAccount">Створити</button>
        <button class="secondary" @click="showCreate = false">Скасувати</button>
      </div>
    </div>
  </div>

  <div v-if="codeModalAccount" class="modal-backdrop" @click.self="closeCodeModal">
    <div class="modal">
      <h3 style="margin-top: 0">{{ codeModalAccount.label }} — картка магазину</h3>
      <p v-if="loyaltyLoading" class="muted">Завантаження…</p>
      <p v-else-if="loyaltyError" style="color: var(--danger)">{{ loyaltyError }}</p>
      <LoyaltyCodeDisplay v-else-if="loyaltyCode" :code="loyaltyCode" />
      <div class="toolbar" style="justify-content: center; margin-bottom: 0; margin-top: 12px">
        <button class="secondary" @click="closeCodeModal">Закрити</button>
      </div>
    </div>
  </div>
</template>
