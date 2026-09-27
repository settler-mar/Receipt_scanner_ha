<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { api } from '@/lib/api'
import { useShopSuggestions } from '@/composables/useShopSuggestions'
import type { Account } from '@/types/api'
import { nowKyivInput, kyivInputToISO } from '@/lib/datetime'

const router = useRouter()
const route = useRoute()
const accounts = ref<Account[]>([])
const loadingAccounts = ref(true)
const { shopNames, addressesFor, bestAddressFor } = useShopSuggestions()

function onShopNameInput() {
  // Don't clobber an address the user already typed/kept from the restored
  // draft — only offer the guess while the field is still empty.
  if (!form.value.shop_address) {
    const guess = bestAddressFor(form.value.shop_name)
    if (guess) form.value.shop_address = guess
  }
}

// Bump this if the shape below ever changes incompatibly, so an old stale
// draft from before the change doesn't get force-fed into a form that no
// longer expects it.
const DRAFT_KEY = 'receipt-draft-v1'

interface DraftItem {
  name: string
  product_code: string
  quantity: number
  unit: string
  unit_price: number | ''
  total_price: number
  discount: number
}

interface DraftForm {
  account_id: string
  purchased_at: string // datetime-local string
  shop_name: string
  shop_address: string
  receipt_url: string
  payment_type: string
  channel: 'offline' | 'online'
  items: DraftItem[]
}

function emptyItem(): DraftItem {
  return { name: '', product_code: '', quantity: 1, unit: '', unit_price: '', total_price: 0, discount: 0 }
}

function nowLocal(): string {
  return nowKyivInput()
}

function emptyForm(): DraftForm {
  return {
    account_id: '',
    purchased_at: nowLocal(),
    shop_name: '',
    shop_address: '',
    receipt_url: '',
    payment_type: '',
    channel: 'offline',
    items: [emptyItem()],
  }
}

function loadDraft(): DraftForm | null {
  try {
    const raw = localStorage.getItem(DRAFT_KEY)
    return raw ? (JSON.parse(raw) as DraftForm) : null
  } catch {
    return null
  }
}

const form = ref<DraftForm>(loadDraft() ?? emptyForm())
const restoredDraft = ref(loadDraft() !== null)

// Autosave on every change — the whole point is that an accidental tab close,
// back-button tap, or page refresh mid-entry doesn't lose what's already been
// typed. Only the form's own plain-data fields are persisted; a File object
// (the photo) can't go into localStorage and isn't worth base64-inflating a
// multi-MB photo into it either, so a chosen-but-not-yet-submitted photo does
// NOT survive a reload — the file input below says so.
watch(form, () => localStorage.setItem(DRAFT_KEY, JSON.stringify(form.value)), { deep: true })

function clearDraft() {
  localStorage.removeItem(DRAFT_KEY)
}

function discardDraft() {
  if (!confirm('Очистити чернетку і почати заново?')) return
  clearDraft()
  form.value = emptyForm()
  removePhoto()
  restoredDraft.value = false
}

onMounted(async () => {
  accounts.value = await api.accounts.list()
  loadingAccounts.value = false
  if (!form.value.account_id) {
    // Explicit ?account_id= wins (e.g. the "+ Додати чек" button on a specific
    // account in AccountsView/AccountDetailView) — only fall back to guessing
    // the scanner/manual-entry account when nothing was passed in.
    const requested = typeof route.query.account_id === 'string' ? route.query.account_id : ''
    if (requested && accounts.value.some((a) => a.id === requested)) {
      form.value.account_id = requested
    } else if (accounts.value.length) {
      const scanner = accounts.value.find((a) => a.store.code === 'scanner')
      form.value.account_id = scanner?.id ?? ''
    }
  }
})

function addItem() {
  form.value.items.push(emptyItem())
}
function removeItemRow(i: number) {
  form.value.items.splice(i, 1)
}
function recalcTotal(it: DraftItem) {
  if (it.unit_price !== '') {
    it.total_price = Math.round(Number(it.quantity || 0) * Number(it.unit_price) * 100) / 100
  }
}

const itemsTotal = computed(() => form.value.items.reduce((sum, it) => sum + (Number(it.total_price) || 0), 0))

const photo = ref<File | null>(null)
const photoPreviewUrl = ref<string | null>(null)

function onPhotoChange(e: Event) {
  const file = (e.target as HTMLInputElement).files?.[0]
  if (!file) return
  photo.value = file
  if (photoPreviewUrl.value) URL.revokeObjectURL(photoPreviewUrl.value)
  photoPreviewUrl.value = URL.createObjectURL(file)
}
function removePhoto() {
  photo.value = null
  if (photoPreviewUrl.value) URL.revokeObjectURL(photoPreviewUrl.value)
  photoPreviewUrl.value = null
}

const saving = ref(false)
const error = ref('')

async function submit() {
  error.value = ''
  if (!form.value.account_id) {
    error.value = 'Оберіть акаунт'
    return
  }
  const namedItems = form.value.items.filter((it) => it.name.trim())
  if (!namedItems.length) {
    error.value = 'Додайте хоча б одну позицію з назвою'
    return
  }

  saving.value = true
  try {
    const receipt = await api.receipts.create({
      account_id: form.value.account_id,
      purchased_at: kyivInputToISO(form.value.purchased_at),
      total_amount: itemsTotal.value,
      shop_name: form.value.shop_name || null,
      shop_address: form.value.shop_address || null,
      payment_type: form.value.payment_type || null,
      channel: form.value.channel,
      receipt_url: form.value.receipt_url || null,
      items: namedItems.map((it) => ({
        name: it.name,
        quantity: Number(it.quantity) || 1,
        unit: it.unit || null,
        unit_price: it.unit_price === '' ? null : Number(it.unit_price),
        total_price: Number(it.total_price) || 0,
        discount: Number(it.discount) || 0,
        barcode: null,
        product_code: it.product_code || null,
      })),
    })
    if (receipt.duplicate) {
      window.alert('Такий чек уже є в ручних завантаженнях. Відкриваємо наявний чек без створення дубля.')
    }
    if (photo.value) {
      await api.receipts.uploadPhoto(receipt.id, photo.value)
    }
    clearDraft()
    router.push(`/receipts/${receipt.id}`)
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    saving.value = false
  }
}

function cancel() {
  router.push('/receipts')
}

function fmtMoney(v: number) {
  return new Intl.NumberFormat('uk-UA', { style: 'currency', currency: 'UAH' }).format(v)
}
</script>

<template>
  <h2>Додати чек вручну</h2>

  <p v-if="restoredDraft" class="card" style="border-color: var(--accent-dim)">
    Відновлено незбережену чернетку.
    <button class="secondary" style="margin-left: 8px" @click="discardDraft">Очистити й почати заново</button>
  </p>
  <p v-if="error" style="color: var(--danger)">{{ error }}</p>

  <p class="card" style="border-color: var(--accent-dim)">
    Є фото чека? Завантажте його в <RouterLink to="/inbox">Інбокс</RouterLink> —
    система спробує розпізнати його автоматично (QR / шаблон / OCR).
  </p>

  <div class="card" style="margin-bottom: 24px">
    <div class="form-row">
      <label>Акаунт</label>
      <select v-model="form.account_id" :disabled="loadingAccounts">
        <option value="" disabled>Оберіть…</option>
        <option v-for="a in accounts" :key="a.id" :value="a.id">{{ a.label }} ({{ a.store.name }})</option>
      </select>
    </div>
    <div class="form-row">
      <label>Дата</label>
      <input type="datetime-local" v-model="form.purchased_at" />
    </div>
    <div class="form-row">
      <label>Магазин</label>
      <input v-model="form.shop_name" list="shop-names" placeholder="Назва магазину" @input="onShopNameInput" />
      <datalist id="shop-names">
        <option v-for="n in shopNames()" :key="n" :value="n" />
      </datalist>
    </div>
    <div class="form-row">
      <label>Адреса</label>
      <input v-model="form.shop_address" list="shop-addresses" placeholder="необов'язково" />
      <datalist id="shop-addresses">
        <option v-for="a in addressesFor(form.shop_name)" :key="a" :value="a" />
      </datalist>
    </div>
    <div class="form-row">
      <label>Посилання на чек (наприклад, з SMS)</label>
      <input v-model="form.receipt_url" placeholder="https://www.vencon.ua/receipt/..." />
    </div>
    <div class="form-row">
      <label>Тип оплати</label>
      <input v-model="form.payment_type" placeholder="необов'язково" />
    </div>
    <div class="form-row">
      <label>Канал</label>
      <select v-model="form.channel">
        <option value="offline">Офлайн</option>
        <option value="online">Онлайн</option>
      </select>
    </div>
  </div>

  <div class="card" style="margin-bottom: 24px">
    <h3 style="margin-top: 0">Фото чека</h3>
    <p class="muted" style="margin-top: -8px">
      Не зберігається в чернетці — якщо сторінку перезавантажити, оберіть фото ще раз перед збереженням.
    </p>
    <div v-if="photoPreviewUrl">
      <img :src="photoPreviewUrl" alt="Фото чека" style="max-width: 240px; border-radius: 8px; display: block; margin-bottom: 8px" />
      <button class="secondary" @click="removePhoto">Прибрати фото</button>
    </div>
    <input v-else type="file" accept="image/*" @change="onPhotoChange" />
  </div>

  <div class="card" style="margin-bottom: 24px">
    <div class="toolbar">
      <h3 style="margin: 0; flex: 1">Товари</h3>
      <button class="secondary" @click="addItem">+ Додати позицію</button>
    </div>
    <table>
      <thead>
        <tr>
          <th>Назва</th>
          <th>Код</th>
          <th>К-сть</th>
          <th>Од.</th>
          <th>Ціна</th>
          <th>Знижка</th>
          <th>Сума</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(it, i) in form.items" :key="i">
          <td><input v-model="it.name" style="width: 100%" /></td>
          <td><input v-model="it.product_code" placeholder="код" style="width: 100px" /></td>
          <td>
            <input
              type="number"
              step="0.001"
              v-model.number="it.quantity"
              style="width: 70px"
              @input="recalcTotal(it)"
            />
          </td>
          <td><input v-model="it.unit" placeholder="шт" style="width: 60px" /></td>
          <td>
            <input type="number" step="0.01" v-model="it.unit_price" style="width: 90px" @input="recalcTotal(it)" />
          </td>
          <td><input type="number" step="0.01" v-model.number="it.discount" style="width: 80px" /></td>
          <td><input type="number" step="0.01" v-model.number="it.total_price" style="width: 90px" /></td>
          <td><button class="danger" @click="removeItemRow(i)">✕</button></td>
        </tr>
      </tbody>
    </table>
    <p class="stat" style="margin-top: 12px; margin-bottom: 0">Разом: {{ fmtMoney(itemsTotal) }}</p>
  </div>

  <div class="toolbar">
    <button :disabled="saving" @click="submit">{{ saving ? 'Зберігаємо…' : 'Зберегти чек' }}</button>
    <button class="secondary" @click="cancel">Скасувати</button>
    <button class="secondary" @click="discardDraft">Очистити чернетку</button>
  </div>
</template>
