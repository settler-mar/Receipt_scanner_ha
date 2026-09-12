<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { api } from '@/lib/api'
import LoyaltyCodeDisplay from '@/components/LoyaltyCodeDisplay.vue'
import { storeFallbackIcon, storeLogoUrl } from '@/lib/storeIcons'
import type { Account, LoyaltyCode, Store } from '@/types/api'
import { fmtDateTime } from '@/lib/datetime'

const router = useRouter()

const accounts = ref<Account[]>([])
const stores = ref<Store[]>([])
const loading = ref(true)
const showCatalog = ref(false)
const showCreate = ref(false)
const collecting = ref<string | null>(null)

// Bulk "refresh receipts" — runs the existing per-account collect sequentially.
const selectedIds = ref<Set<string>>(new Set())
const bulkRunning = ref(false)
const bulkDone = ref(0)
const bulkTotal = ref(0)
const bulkCurrent = ref('')

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
type Category = 'grocery' | 'marketplace' | 'diy' | 'wholesale' | 'fuel' | 'manual' | 'other'

const CATEGORY_LABELS: Record<Category, string> = {
  grocery: 'Продукти',
  marketplace: 'Маркетплейси',
  diy: 'Будівництво і DIY',
  wholesale: 'Гурт',
  fuel: 'АЗС',
  manual: 'Ручне введення',
  other: 'Інше',
}
const CATEGORY_ORDER: Category[] = ['grocery', 'marketplace', 'diy', 'wholesale', 'fuel', 'manual', 'other']

const STORE_CATEGORY: Record<string, Category> = {
  atb: 'grocery',
  fozzy: 'grocery',
  fora: 'grocery',
  silpo: 'grocery',
  prom: 'marketplace',
  epicentrk: 'diy',
  'dnipro-m': 'diy',
  vencon: 'diy',
  metro: 'wholesale',
  wog: 'fuel',
  ukrnafta: 'fuel',
  bars: 'fuel',
  upg: 'fuel',
  brsm: 'fuel',
  klo: 'fuel',
  scanner: 'manual',
}

// Logo URLs/emoji fallback live in lib/storeIcons.ts, shared with ReceiptsView.

function storeCategory(store: Store): Category {
  if (STORE_CATEGORY[store.code]) return STORE_CATEGORY[store.code]
  if (store.capabilities.manual_receipts) return 'manual'
  if (store.capabilities.fuel_prices || store.auth_type === 'card_number') return 'fuel'
  return 'other'
}

const storesByCategory = computed(() => {
  const groups = new Map<Category, Store[]>()
  for (const s of stores.value) {
    const cat = storeCategory(s)
    if (!groups.has(cat)) groups.set(cat, [])
    groups.get(cat)!.push(s)
  }
  return CATEGORY_ORDER.filter((c) => groups.has(c)).map((c) => ({ category: c, stores: groups.get(c)! }))
})

const accountsByCategory = computed(() => {
  const groups = new Map<Category, Account[]>()
  for (const a of accounts.value) {
    const cat = storeCategory(a.store)
    if (!groups.has(cat)) groups.set(cat, [])
    groups.get(cat)!.push(a)
  }
  return CATEGORY_ORDER.filter((c) => groups.has(c)).map((c) => ({ category: c, accounts: groups.get(c)! }))
})

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

async function load() {
  loading.value = true
  ;[accounts.value, stores.value] = await Promise.all([api.accounts.list(), api.stores.list()])
  loading.value = false
}

onMounted(load)

function openCreate(storeCode = '') {
  form.value = { store_code: storeCode, label: '', phone_number: '' }
  showCreate.value = true
  showCatalog.value = false
}

async function createAccount() {
  if (!form.value.store_code || !form.value.label) return
  await api.accounts.create({
    store_code: form.value.store_code,
    label: form.value.label,
    phone_number: form.value.phone_number || undefined,
  })
  showCreate.value = false
  await load()
}

async function removeAccount(id: string) {
  if (!confirm('Видалити акаунт разом з усіма зібраними чеками?')) return
  await api.accounts.remove(id)
  await load()
}

async function collectNow(id: string) {
  collecting.value = id
  try {
    await api.accounts.collectNow(id)
    await load()
  } catch (e) {
    alert((e as Error).message)
  } finally {
    collecting.value = null
  }
}

const collectableAccounts = computed(() =>
  accounts.value.filter((a) => a.store.capabilities.receipts && a.status === 'active'),
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

async function collectMany(ids: string[]) {
  if (bulkRunning.value || !ids.length) return
  bulkRunning.value = true
  bulkTotal.value = ids.length
  bulkDone.value = 0
  const errors: string[] = []
  for (const id of ids) {
    const acc = accounts.value.find((a) => a.id === id)
    bulkCurrent.value = acc?.label ?? ''
    collecting.value = id
    try {
      await api.accounts.collectNow(id)
    } catch (e) {
      errors.push(`${acc?.label ?? id}: ${(e as Error).message}`)
    }
    bulkDone.value++
  }
  collecting.value = null
  bulkRunning.value = false
  bulkCurrent.value = ''
  await load()
  if (errors.length) alert(`Оновлення завершено з помилками:\n\n${errors.join('\n')}`)
}

function collectAll() {
  collectMany(collectableAccounts.value.map((a) => a.id))
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
    <h2 style="margin: 0; flex: 1">Застосунки</h2>
    <button @click="showCatalog = !showCatalog">{{ showCatalog ? 'Закрити каталог' : '+ Додати застосунок' }}</button>
  </div>

  <div v-if="loading" class="muted">Завантаження…</div>
  <template v-else>
    <div v-if="showCatalog" class="card" style="margin-bottom: 24px">
      <h3 style="margin-top: 0">Каталог застосунків</h3>
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
              <div style="font-weight: 600">{{ s.name }}</div>
              <div class="muted" style="font-size: 12px">+ Додати акаунт</div>
            </span>
          </button>
        </div>
      </div>
    </div>

    <h2 style="margin: 32px 0 12px">Мої акаунти</h2>
    <p v-if="!accounts.length" class="muted">Ще немає жодного акаунта — натисніть "Додати застосунок" вище.</p>

    <div v-if="collectableAccounts.length" class="card" style="margin-bottom: 20px; display: flex; flex-wrap: wrap; align-items: center; gap: 10px">
      <strong>Оновити чеки</strong>
      <button :disabled="bulkRunning" @click="collectAll">Оновити всі ({{ collectableAccounts.length }})</button>
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
        {{ CATEGORY_LABELS[group.category] }}
      </h3>
      <table>
        <thead>
          <tr>
            <th></th>
            <th>Назва</th>
            <th>Магазин</th>
            <th>Телефон</th>
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
            <td>{{ a.store.name }}</td>
            <td>{{ a.phone_number ?? '—' }}</td>
            <td><span class="badge" :class="`badge-${a.status}`">{{ accountStatusLabel(a) }}</span></td>
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
            <td style="display: flex; gap: 6px">
              <button
                v-if="a.store.capabilities.receipts && a.status === 'active'"
                class="secondary"
                :disabled="collecting === a.id"
                @click="collectNow(a.id)"
              >
                {{ collecting === a.id ? 'Збір…' : 'Зібрати зараз' }}
              </button>
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
        <label>Телефон (+380...)</label>
        <input v-model="form.phone_number" placeholder="необов'язково" />
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
