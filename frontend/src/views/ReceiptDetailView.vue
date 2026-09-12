<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { api, BASE } from '@/lib/api'
import { useShopSuggestions } from '@/composables/useShopSuggestions'
import type { Category, Receipt, ReceiptItem } from '@/types/api'
import { fmtDateTime, isoToKyivInput, kyivInputToISO } from '@/lib/datetime'
import ZoomableImage from '@/components/ZoomableImage.vue'

const props = defineProps<{ id: string }>()
const router = useRouter()
const receipt = ref<Receipt | null>(null)
const loading = ref(true)
const reparsing = ref(false)
const { shopNames, addressesFor, bestAddressFor } = useShopSuggestions()

function onShopNameInput() {
  if (!receiptForm.value.shop_address) {
    const guess = bestAddressFor(receiptForm.value.shop_name)
    if (guess) receiptForm.value.shop_address = guess
  }
}

const categories = ref<Category[]>([])
// Flat, tree-ordered options with an em-dash indent per depth.
const categoryOptions = computed(() =>
  [...categories.value]
    .sort((a, b) => a.path.localeCompare(b.path))
    .map((c) => ({ id: c.id, label: `${'— '.repeat(c.path.split('/').length - 1)}${c.name}` })),
)

const loadError = ref('')

async function load() {
  // A receipt can be deleted from another tab, or the id can simply be wrong.
  // Without catching that, the failed request left the view stuck on
  // "Завантаження…" forever instead of saying the receipt is gone.
  loadError.value = ''
  try {
    receipt.value = await api.receipts.get(props.id)
  } catch (e) {
    receipt.value = null
    loadError.value = (e as Error).message || 'Не вдалося завантажити чек'
  } finally {
    loading.value = false
  }
}

const tagInput = ref('')
const allTags = ref<string[]>([])
async function loadTags() {
  allTags.value = (await api.receipts.tags().catch(() => [])).map((t) => t.tag)
}
async function addTag() {
  const t = tagInput.value.trim()
  if (!t || !receipt.value) return
  receipt.value = await api.receipts.setTags(props.id, [...receipt.value.tags, t])
  tagInput.value = ''
  loadTags()
}
async function removeTag(tag: string) {
  if (!receipt.value) return
  receipt.value = await api.receipts.setTags(props.id, receipt.value.tags.filter((x) => x !== tag))
}

onMounted(async () => {
  categories.value = await api.categories.list().catch(() => [])
  await Promise.all([load(), loadTags()])
})

function fmtMoney(v: number, currency = 'UAH') {
  return new Intl.NumberFormat('uk-UA', { style: 'currency', currency }).format(v)
}
function fmtExchangeRate() {
  if (!receipt.value?.exchange_rate_to_uah || receipt.value.currency === 'UAH') return null
  return `1 ${receipt.value.currency} = ${receipt.value.exchange_rate_to_uah.toFixed(4)} UAH`
}
function fmtTotalUah() {
  if (!receipt.value?.exchange_rate_to_uah || receipt.value.currency === 'UAH') return null
  return fmtMoney(receipt.value.total_amount * receipt.value.exchange_rate_to_uah, 'UAH')
}
function fmtItemUah(value: number | null | undefined) {
  if (value == null || !receipt.value?.exchange_rate_to_uah || receipt.value.currency === 'UAH') return null
  return fmtMoney(value * receipt.value.exchange_rate_to_uah, 'UAH')
}
function fmtDate(v: string) {
  return fmtDateTime(v)
}

function toLocalInput(v: string) {
  return isoToKyivInput(v)
}

const editingReceipt = ref(false)
const savingReceipt = ref(false)
const receiptForm = ref({
  purchased_at: '',
  total_amount: 0,
  shop_name: '',
  shop_address: '',
  payment_type: '',
  channel: 'offline' as 'offline' | 'online',
  total_discount: 0,
  bonuses: '' as number | '',
  bonuses_used: '' as number | '',
  receipt_url: '',
})

function startEditReceipt() {
  if (!receipt.value) return
  const r = receipt.value
  receiptForm.value = {
    purchased_at: toLocalInput(r.purchased_at),
    total_amount: r.total_amount,
    shop_name: r.shop_name ?? '',
    shop_address: r.shop_address ?? '',
    payment_type: r.payment_type ?? '',
    channel: r.channel,
    total_discount: r.total_discount,
    bonuses: r.bonuses ?? '',
    bonuses_used: r.bonuses_used ?? '',
    receipt_url: r.receipt_url ?? '',
  }
  editingReceipt.value = true
}

async function saveReceipt() {
  savingReceipt.value = true
  try {
    const f = receiptForm.value
    await api.receipts.update(props.id, {
      purchased_at: kyivInputToISO(f.purchased_at),
      total_amount: Number(f.total_amount),
      shop_name: f.shop_name || null,
      shop_address: f.shop_address || null,
      payment_type: f.payment_type || null,
      channel: f.channel,
      total_discount: Number(f.total_discount),
      bonuses: f.bonuses === '' ? null : Number(f.bonuses),
      bonuses_used: f.bonuses_used === '' ? null : Number(f.bonuses_used),
      receipt_url: f.receipt_url || null,
    })
    editingReceipt.value = false
    await load()
  } catch (e) {
    alert((e as Error).message)
  } finally {
    savingReceipt.value = false
  }
}

const editingItemId = ref<string | null>(null)
const itemForm = ref({
  name: '',
  quantity: 1,
  unit: '',
  unit_price: '' as number | '',
  total_price: 0,
  discount: 0,
  barcode: '',
  category_id: '' as string,
})

function startEditItem(it: ReceiptItem) {
  editingItemId.value = it.id
  itemForm.value = {
    name: it.name,
    quantity: it.quantity,
    unit: it.unit ?? '',
    unit_price: it.unit_price ?? '',
    total_price: it.total_price,
    discount: it.discount,
    barcode: it.barcode ?? '',
    category_id: it.category_id ?? '',
  }
}

async function saveItem() {
  if (!editingItemId.value) return
  const f = itemForm.value
  try {
    await api.receipts.updateItem(props.id, editingItemId.value, {
      name: f.name,
      quantity: Number(f.quantity),
      unit: f.unit || null,
      unit_price: f.unit_price === '' ? null : Number(f.unit_price),
      total_price: Number(f.total_price),
      discount: Number(f.discount),
      barcode: f.barcode || null,
      category_id: f.category_id || null,
    })
    editingItemId.value = null
    await load()
  } catch (e) {
    alert((e as Error).message)
  }
}

async function removeItem(itemId: string) {
  if (!confirm('Видалити цю позицію?')) return
  await api.receipts.removeItem(props.id, itemId)
  await load()
}

const addingItem = ref(false)
const newItemForm = ref({ name: '', quantity: 1, unit: '', unit_price: '' as number | '', total_price: 0, discount: 0 })

function startAddItem() {
  newItemForm.value = { name: '', quantity: 1, unit: '', unit_price: '', total_price: 0, discount: 0 }
  addingItem.value = true
}

async function saveNewItem() {
  const f = newItemForm.value
  if (!f.name) return
  try {
    await api.receipts.addItem(props.id, {
      name: f.name,
      quantity: Number(f.quantity),
      unit: f.unit || null,
      unit_price: f.unit_price === '' ? null : Number(f.unit_price),
      total_price: Number(f.total_price),
      discount: Number(f.discount),
      barcode: null,
    })
    addingItem.value = false
    await load()
  } catch (e) {
    alert((e as Error).message)
  }
}

const uploadingPhoto = ref(false)

async function onPhotoChange(e: Event) {
  const file = (e.target as HTMLInputElement).files?.[0]
  if (!file) return
  uploadingPhoto.value = true
  try {
    await api.receipts.uploadPhoto(props.id, file)
    await load()
  } catch (err) {
    alert((err as Error).message)
  } finally {
    uploadingPhoto.value = false
  }
}

async function removePhoto() {
  if (!confirm('Видалити фото чека?')) return
  await api.receipts.removePhoto(props.id)
  await load()
}

const uploadingWarranty = ref(false)

async function onWarrantyChange(e: Event) {
  const file = (e.target as HTMLInputElement).files?.[0]
  if (!file) return
  uploadingWarranty.value = true
  try {
    await api.receipts.uploadWarranty(props.id, file)
    await load()
  } catch (err) {
    alert((err as Error).message)
  } finally {
    uploadingWarranty.value = false
  }
}

async function removeWarranty() {
  if (!confirm('Видалити гарантійний талон?')) return
  await api.receipts.removeWarranty(props.id)
  await load()
}

async function removeReceipt() {
  if (!confirm('Видалити цей чек повністю? Цю дію не можна скасувати.')) return
  await api.receipts.remove(props.id)
  router.push('/receipts')
}

async function reparseReceipt() {
  if (!receipt.value?.receipt_url || reparsing.value) return
  reparsing.value = true
  try {
    const job = await api.fiscalQueue.add(receipt.value.receipt_url)
    const result = await api.fiscalQueue.run(job.id)
    if (result.status === 'imported') {
      await load()
      alert('Чек успішно перепарсено та оновлено.')
    } else {
      await router.push({ path: '/settings', hash: '#tax' })
    }
  } catch (e) {
    alert((e as Error).message)
  } finally {
    reparsing.value = false
  }
}
</script>

<template>
  <div v-if="loading" class="muted">Завантаження…</div>
  <template v-else-if="receipt">
    <div class="toolbar">
      <h2 style="margin: 0; flex: 1">{{ receipt.shop_name ?? receipt.store_code }}</h2>
      <span class="badge" :class="`badge-channel-${receipt.channel}`">
        {{ receipt.channel === 'online' ? 'Онлайн' : 'Офлайн' }}
      </span>
      <button class="secondary" @click="startEditReceipt">Редагувати</button>
      <button v-if="receipt.receipt_url?.includes('cabinet.tax.gov.ua')" class="secondary" :disabled="reparsing" @click="reparseReceipt">
        {{ reparsing ? 'Перепарсовуємо…' : 'Перепарсити' }}
      </button>
      <button class="danger" @click="removeReceipt">Видалити</button>
    </div>

    <div v-if="editingReceipt" class="card" style="margin-bottom: 24px">
      <h3 style="margin-top: 0">Редагування чека</h3>
      <div class="form-row">
        <label>Дата</label>
        <input type="datetime-local" v-model="receiptForm.purchased_at" />
      </div>
      <div class="form-row">
        <label>Сума</label>
        <input type="number" step="0.01" v-model.number="receiptForm.total_amount" />
      </div>
      <div class="form-row">
        <label>Знижка</label>
        <input type="number" step="0.01" v-model.number="receiptForm.total_discount" />
      </div>
      <div class="form-row">
        <label>Нараховано бонусів (порожньо — немає даних)</label>
        <input type="number" step="0.01" v-model="receiptForm.bonuses" />
      </div>
      <div class="form-row">
        <label>Використано бонусів (порожньо — немає даних)</label>
        <input type="number" step="0.01" v-model="receiptForm.bonuses_used" />
      </div>
      <div class="form-row">
        <label>Магазин</label>
        <input v-model="receiptForm.shop_name" list="edit-shop-names" @input="onShopNameInput" />
        <datalist id="edit-shop-names">
          <option v-for="n in shopNames()" :key="n" :value="n" />
        </datalist>
      </div>
      <div class="form-row">
        <label>Адреса</label>
        <input v-model="receiptForm.shop_address" list="edit-shop-addresses" />
        <datalist id="edit-shop-addresses">
          <option v-for="a in addressesFor(receiptForm.shop_name)" :key="a" :value="a" />
        </datalist>
      </div>
      <div class="form-row">
        <label>Тип оплати</label>
        <input v-model="receiptForm.payment_type" />
      </div>
      <div class="form-row">
        <label>Посилання на чек</label>
        <input v-model="receiptForm.receipt_url" placeholder="https://www.vencon.ua/receipt/..." />
      </div>
      <div class="form-row">
        <label>Канал</label>
        <select v-model="receiptForm.channel">
          <option value="offline">Офлайн</option>
          <option value="online">Онлайн</option>
        </select>
      </div>
      <div class="toolbar" style="margin-bottom: 0">
        <button :disabled="savingReceipt" @click="saveReceipt">
          {{ savingReceipt ? 'Зберігаємо…' : 'Зберегти' }}
        </button>
        <button class="secondary" @click="editingReceipt = false">Скасувати</button>
      </div>
    </div>

    <div class="card-grid">
      <div class="card">
        <div class="stat-label">Дата</div>
        <div>{{ fmtDate(receipt.purchased_at) }}</div>
      </div>
      <div class="card">
        <div class="stat-label">Сума</div>
        <div class="stat">{{ fmtMoney(receipt.total_amount, receipt.currency) }}</div>
        <div v-if="fmtExchangeRate()" class="muted" style="margin-top: 4px">{{ fmtExchangeRate() }}</div>
        <div v-if="fmtTotalUah()" class="muted" style="margin-top: 2px">≈ {{ fmtTotalUah() }}</div>
      </div>
      <div class="card">
        <div class="stat-label">Знижка</div>
        <div class="stat" :style="receipt.total_discount ? 'color: var(--success)' : ''">
          {{ receipt.total_discount ? fmtMoney(receipt.total_discount, receipt.currency) : '—' }}
        </div>
      </div>
      <div v-if="receipt.bonuses !== null" class="card">
        <div class="stat-label">Нараховано бонусів</div>
        <div class="stat" style="color: var(--accent)">{{ fmtMoney(receipt.bonuses) }}</div>
      </div>
      <div v-if="receipt.bonuses_used !== null" class="card">
        <div class="stat-label">Використано бонусів</div>
        <div class="stat">{{ fmtMoney(receipt.bonuses_used) }}</div>
      </div>
      <div class="card">
        <div class="stat-label">Акаунт</div>
        <div>{{ receipt.account_label }}</div>
      </div>
      <div class="card">
        <div class="stat-label">Адреса</div>
        <div>{{ receipt.shop_address ?? '—' }}</div>
      </div>
      <div class="card">
        <div class="stat-label">Номер чека</div>
        <div style="word-break: break-all">{{ receipt.external_id }}</div>
      </div>
      <div v-if="receipt.order_status" class="card">
        <div class="stat-label">Статус замовлення</div>
        <div>{{ receipt.order_status }}</div>
      </div>
      <div v-if="receipt.tracking_numbers.length" class="card">
        <div class="stat-label">Трек-номери</div>
        <div v-for="number in receipt.tracking_numbers" :key="number" style="word-break: break-all">
          {{ number }}
        </div>
      </div>
      <div v-if="receipt.receipt_url" class="card">
        <div class="stat-label">Посилання на чек</div>
        <div style="word-break: break-all">
          <a :href="receipt.receipt_url" target="_blank" rel="noopener noreferrer">{{ receipt.receipt_url }}</a>
        </div>
      </div>
      <div v-if="receipt.receipt_file_url" class="card">
        <div class="stat-label">Локальна копія чека</div>
        <a :href="`${BASE}${receipt.receipt_file_url}`" target="_blank" rel="noopener noreferrer">
          Відкрити збережений чек
        </a>
      </div>
      <div v-if="receipt.has_warranty" class="card">
        <div class="stat-label">Гарантія</div>
        <div><strong>{{ receipt.warranty_text ?? 'Гарантію знайдено' }}</strong></div>
        <div v-if="receipt.warranty_expires_at" class="muted" style="margin-top: 4px">
          Діє до {{ fmtDate(receipt.warranty_expires_at) }}
        </div>
      </div>
    </div>

    <div class="card" style="margin-bottom: 24px">
      <h3 style="margin-top: 0">Теги</h3>
      <div style="display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px">
        <span
          v-for="t in receipt.tags"
          :key="t"
          class="badge"
          style="background: var(--bg-hover); color: var(--text); display: inline-flex; gap: 6px; align-items: center"
        >
          #{{ t }}
          <span style="cursor: pointer; opacity: 0.6" title="Прибрати" @click="removeTag(t)">✕</span>
        </span>
        <span v-if="!receipt.tags.length" class="muted">Ще немає — напр. #поездка_львов_лето_2026</span>
      </div>
      <div class="toolbar" style="margin-bottom: 0">
        <input
          v-model="tagInput"
          list="all-tags"
          placeholder="#назва_тегу"
          style="flex: 1"
          @keyup.enter="addTag"
        />
        <button :disabled="!tagInput.trim()" @click="addTag">Додати</button>
        <datalist id="all-tags">
          <option v-for="t in allTags" :key="t" :value="'#' + t" />
        </datalist>
      </div>
    </div>

    <div class="card" style="margin-bottom: 24px">
      <h3 style="margin-top: 0">Фото чека</h3>
      <div v-if="receipt.photo_url">
        <img
          :src="`${BASE}${receipt.photo_url}`"
          alt="Фото чека"
          style="max-width: 320px; border-radius: 8px; display: block; margin-bottom: 8px"
        />
        <div class="toolbar" style="margin-bottom: 0">
          <label class="btn secondary" style="cursor: pointer">
            {{ uploadingPhoto ? 'Завантажуємо…' : 'Замінити' }}
            <input type="file" accept="image/*" style="display: none" :disabled="uploadingPhoto" @change="onPhotoChange" />
          </label>
          <button class="danger" @click="removePhoto">Видалити фото</button>
        </div>
      </div>
      <div v-else>
        <label class="btn" style="cursor: pointer; display: inline-block">
          {{ uploadingPhoto ? 'Завантажуємо…' : 'Завантажити фото' }}
          <input type="file" accept="image/*" style="display: none" :disabled="uploadingPhoto" @change="onPhotoChange" />
        </label>
      </div>
    </div>

    <div class="card" style="margin-bottom: 24px">
      <h3 style="margin-top: 0">Гарантійний талон</h3>
      <p v-if="!receipt.warranty_pdf_url" class="muted" style="margin-top: -8px">
        {{
          receipt.has_warranty
            ? 'Гарантію знайдено в картці товару — посилання для перегляду буде нижче. За потреби також можна завантажити PDF гарантійного талону.'
            : 'Якщо на цю покупку є гарантія (навіть якщо магазин про це не звітує) — завантажте PDF талону, дата закінчення розпізнається автоматично.'
        }}
      </p>
      <div v-if="receipt.warranty_pdf_url">
        <p style="margin-top: 0">
          <a :href="`${BASE}${receipt.warranty_pdf_url}`" target="_blank" rel="noopener noreferrer">
            Переглянути PDF
          </a>
        </p>
        <div class="toolbar" style="margin-bottom: 0">
          <label class="btn secondary" style="cursor: pointer">
            {{ uploadingWarranty ? 'Завантажуємо…' : 'Замінити' }}
            <input type="file" accept="application/pdf" style="display: none" :disabled="uploadingWarranty" @change="onWarrantyChange" />
          </label>
          <button class="danger" @click="removeWarranty">Видалити талон</button>
        </div>
      </div>
      <div v-else-if="receipt.has_warranty">
        <p style="margin-top: 0">
          <a :href="`${BASE}/api/receipts/${receipt.id}/warranty`" target="_blank" rel="noopener noreferrer">
            Переглянути гарантію з картки товару
          </a>
        </p>
        <label class="btn secondary" style="cursor: pointer; display: inline-block">
          {{ uploadingWarranty ? 'Завантажуємо…' : 'Завантажити PDF талону' }}
          <input type="file" accept="application/pdf" style="display: none" :disabled="uploadingWarranty" @change="onWarrantyChange" />
        </label>
      </div>
      <div v-else>
        <label class="btn" style="cursor: pointer; display: inline-block">
          {{ uploadingWarranty ? 'Завантажуємо…' : 'Завантажити PDF' }}
          <input type="file" accept="application/pdf" style="display: none" :disabled="uploadingWarranty" @change="onWarrantyChange" />
        </label>
      </div>
    </div>

    <div class="toolbar">
      <h3 style="margin: 0; flex: 1">Товари ({{ receipt.items.length }})</h3>
      <button class="secondary" @click="startAddItem">+ Додати позицію</button>
    </div>
    <table v-if="receipt.items.length || addingItem">
      <thead>
        <tr>
          <th>Назва</th>
          <th>Категорія</th>
          <th>К-сть</th>
          <th>Ціна</th>
          <th>Знижка</th>
          <th>Сума</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="it in receipt.items" :key="it.id">
          <template v-if="editingItemId === it.id">
            <td><input v-model="itemForm.name" style="width: 100%" /></td>
            <td>
              <select v-model="itemForm.category_id" style="max-width: 160px">
                <option value="">— без категорії —</option>
                <option v-for="o in categoryOptions" :key="o.id" :value="o.id">{{ o.label }}</option>
              </select>
            </td>
            <td style="display: flex; gap: 4px">
              <input type="number" step="0.001" v-model.number="itemForm.quantity" style="width: 70px" />
              <input v-model="itemForm.unit" placeholder="од." style="width: 60px" />
            </td>
            <td><input type="number" step="0.01" v-model="itemForm.unit_price" style="width: 90px" /></td>
            <td><input type="number" step="0.01" v-model.number="itemForm.discount" style="width: 80px" /></td>
            <td><input type="number" step="0.01" v-model.number="itemForm.total_price" style="width: 90px" /></td>
            <td style="display: flex; gap: 6px">
              <button @click="saveItem">Зберегти</button>
              <button class="secondary" @click="editingItemId = null">Скасувати</button>
            </td>
          </template>
          <template v-else>
            <td>
              <div style="display: flex; gap: 10px; align-items: flex-start">
                <ZoomableImage
                  v-if="it.product_image_url"
                  :src="it.product_image_url.startsWith('/') ? `${BASE}${it.product_image_url}` : it.product_image_url"
                  :alt="it.name"
                  style="width: 48px; height: 48px; object-fit: contain; border-radius: 6px"
                />
                <div>
                  <a v-if="it.product_url" :href="it.product_url" target="_blank" rel="noopener noreferrer">{{ it.name }}</a>
                  <span v-else>{{ it.name }}</span>
                  <div v-if="it.merchant_name" class="muted" style="font-size: 12px">{{ it.merchant_name }}</div>
                </div>
              </div>
            </td>
            <td class="muted" style="font-size: 12px">{{ it.category_path ?? '—' }}</td>
            <td>{{ it.quantity }} {{ it.unit ?? '' }}</td>
            <td>
              <div>{{ it.unit_price !== null ? fmtMoney(it.unit_price, receipt.currency) : '—' }}</div>
              <div v-if="fmtItemUah(it.unit_price)" class="muted" style="font-size: 12px">≈ {{ fmtItemUah(it.unit_price) }}</div>
            </td>
            <td class="muted">
              <div>{{ it.discount ? fmtMoney(it.discount, receipt.currency) : '—' }}</div>
              <div v-if="fmtItemUah(it.discount)" style="font-size: 12px">≈ {{ fmtItemUah(it.discount) }}</div>
            </td>
            <td>
              <div>{{ fmtMoney(it.total_price, receipt.currency) }}</div>
              <div v-if="fmtItemUah(it.total_price)" class="muted" style="font-size: 12px">≈ {{ fmtItemUah(it.total_price) }}</div>
            </td>
            <td style="display: flex; gap: 6px">
              <button class="secondary" @click="startEditItem(it)">Редагувати</button>
              <button class="danger" @click="removeItem(it.id)">Видалити</button>
            </td>
          </template>
        </tr>
        <tr v-if="addingItem">
          <td><input v-model="newItemForm.name" placeholder="Назва" style="width: 100%" /></td>
          <td class="muted" style="font-size: 12px">авто</td>
          <td style="display: flex; gap: 4px">
            <input type="number" step="0.001" v-model.number="newItemForm.quantity" style="width: 70px" />
            <input v-model="newItemForm.unit" placeholder="од." style="width: 60px" />
          </td>
          <td><input type="number" step="0.01" v-model="newItemForm.unit_price" style="width: 90px" /></td>
          <td><input type="number" step="0.01" v-model.number="newItemForm.discount" style="width: 80px" /></td>
          <td><input type="number" step="0.01" v-model.number="newItemForm.total_price" style="width: 90px" /></td>
          <td style="display: flex; gap: 6px">
            <button @click="saveNewItem">Додати</button>
            <button class="secondary" @click="addingItem = false">Скасувати</button>
          </td>
        </tr>
      </tbody>
    </table>
    <p v-else class="muted">Товарні позиції для цього чеку відсутні в джерелі.</p>
  </template>
  <div v-else class="card">
    <h2 style="margin-top: 0">Чек не знайдено</h2>
    <p class="muted">
      Чек <code>{{ id }}</code> не існує — найімовірніше його видалили.
    </p>
    <p v-if="loadError" class="muted">{{ loadError }}</p>
    <div class="toolbar">
      <RouterLink to="/receipts"><button class="secondary">До списку чеків</button></RouterLink>
      <RouterLink to="/inbox"><button class="secondary">До інбоксу</button></RouterLink>
    </div>
  </div>
</template>
