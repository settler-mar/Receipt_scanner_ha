<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { api, BASE } from '@/lib/api'
import { storeFallbackIcon, storeLogoUrl } from '@/lib/storeIcons'
import type { WarrantyProduct } from '@/types/api'
import { fmtDate as fmtDateOnly } from '@/lib/datetime'
import ZoomableImage from '@/components/ZoomableImage.vue'

const products = ref<WarrantyProduct[]>([])
const loading = ref(true)
const error = ref('')

onMounted(async () => {
  try {
    products.value = await api.warranties.list()
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
})

const activeCount = computed(() => products.value.filter((item) => !item.expired && item.warranty_expires_at).length)
const unknownCount = computed(() => products.value.filter((item) => !item.warranty_expires_at).length)
const expiredCount = computed(() => products.value.filter((item) => item.expired).length)

// Everything is filtered and sorted in the browser: /api/warranties returns the
// whole (small) set in one go, so a round trip per keystroke would buy nothing.
type Status = 'all' | 'active' | 'unknown' | 'expired'
type SortKey = 'expires' | 'purchased' | 'price' | 'name'

const filters = ref({ status: 'all' as Status, store: '', search: '', withPhoto: false, within: 0 })
const sortBy = ref<SortKey>('expires')

const storeOptions = computed(() => {
  const byCode = new Map<string, string>()
  for (const item of products.value) byCode.set(item.store_code, item.store_name)
  return [...byCode].sort((a, b) => a[1].localeCompare(b[1], 'uk'))
})

function matchesStatus(item: WarrantyProduct) {
  if (filters.value.status === 'active') return !item.expired && !!item.warranty_expires_at
  if (filters.value.status === 'unknown') return !item.warranty_expires_at
  if (filters.value.status === 'expired') return item.expired
  return true
}

/** Дні, що лишилися; null — коли дату не визначено. */
function daysLeft(item: WarrantyProduct): number | null {
  if (!item.warranty_expires_at) return null
  return Math.ceil((new Date(item.warranty_expires_at).getTime() - Date.now()) / 86400000)
}

const visible = computed(() => {
  const search = filters.value.search.trim().toLowerCase()
  const list = products.value.filter((item) => {
    if (!matchesStatus(item)) return false
    if (filters.value.store && item.store_code !== filters.value.store) return false
    if (filters.value.withPhoto && !item.file_urls?.length) return false
    if (search && !item.name.toLowerCase().includes(search)) return false
    if (filters.value.within) {
      // "Спливає протягом" has meaning only for a warranty that is still running.
      const left = daysLeft(item)
      if (item.expired || left === null || left > filters.value.within) return false
    }
    return true
  })
  const far = Number.POSITIVE_INFINITY
  return [...list].sort((a, b) => {
    if (sortBy.value === 'purchased') {
      return new Date(b.purchased_at).getTime() - new Date(a.purchased_at).getTime()
    }
    if (sortBy.value === 'price') return b.purchase_price - a.purchase_price
    if (sortBy.value === 'name') return a.name.localeCompare(b.name, 'uk')
    // За замовчуванням — як і бекенд: діючі спершу, найближчі до кінця вгорі.
    if (a.expired !== b.expired) return a.expired ? 1 : -1
    const left = (item: WarrantyProduct) =>
      item.warranty_expires_at ? new Date(item.warranty_expires_at).getTime() : far
    return left(a) - left(b)
  })
})

function resetFilters() {
  filters.value = { status: 'all', store: '', search: '', withPhoto: false, within: 0 }
}
const filtersActive = computed(
  () =>
    filters.value.status !== 'all' ||
    !!filters.value.store ||
    !!filters.value.search.trim() ||
    filters.value.withPhoto ||
    !!filters.value.within,
)

function pickStatus(status: Status) {
  filters.value.status = filters.value.status === status ? 'all' : status
}

function fmtMoney(value: number) {
  return new Intl.NumberFormat('uk-UA', { style: 'currency', currency: 'UAH' }).format(value)
}

function fmtDate(value: string | null) {
  return value ? fmtDateOnly(value) : 'дату не визначено'
}

function imageUrl(value: string | null) {
  return value?.startsWith('/') ? `${BASE}${value}` : value
}
</script>

<template>
  <div class="toolbar">
    <div style="flex: 1">
      <h2 style="margin-bottom: 4px">Гарантії</h2>
      <div class="muted">Натисніть на статус, щоб відфільтрувати.</div>
    </div>
  </div>

  <div v-if="!loading && !error && products.length" class="toolbar" style="flex-wrap: wrap">
    <button
      class="secondary"
      :style="filters.status === 'active' ? 'outline: 2px solid var(--accent)' : ''"
      @click="pickStatus('active')"
    >
      Діючі: {{ activeCount }}
    </button>
    <button
      class="secondary"
      :style="filters.status === 'unknown' ? 'outline: 2px solid var(--accent)' : ''"
      @click="pickStatus('unknown')"
    >
      Термін не визначено: {{ unknownCount }}
    </button>
    <button
      class="secondary"
      :style="filters.status === 'expired' ? 'outline: 2px solid var(--accent)' : ''"
      @click="pickStatus('expired')"
    >
      Закінчилися: {{ expiredCount }}
    </button>
  </div>

  <div v-if="!loading && !error && products.length" class="toolbar" style="flex-wrap: wrap">
    <input v-model="filters.search" placeholder="Пошук за назвою" style="min-width: 200px" />
    <select v-model="filters.store">
      <option value="">Всі магазини</option>
      <option v-for="[code, name] in storeOptions" :key="code" :value="code">{{ name }}</option>
    </select>
    <select v-model.number="filters.within">
      <option :value="0">Будь-який термін</option>
      <option :value="30">Спливає за 30 днів</option>
      <option :value="90">Спливає за 90 днів</option>
      <option :value="180">Спливає за 180 днів</option>
    </select>
    <label class="muted" style="display: flex; align-items: center; gap: 6px">
      <input type="checkbox" v-model="filters.withPhoto" />
      Лише з талоном/фото
    </label>
    <select v-model="sortBy">
      <option value="expires">Спочатку ті, що спливають</option>
      <option value="purchased">Спочатку новіші покупки</option>
      <option value="price">Спочатку дорожчі</option>
      <option value="name">За назвою</option>
    </select>
    <button v-if="filtersActive" class="secondary" @click="resetFilters">Скинути</button>
    <span class="muted">Знайдено: {{ visible.length }}</span>
  </div>

  <div v-if="loading" class="muted">Завантаження гарантій…</div>
  <div v-else-if="error" style="color: var(--danger)">{{ error }}</div>
  <div v-else-if="!products.length" class="muted">Товарів із знайденою гарантією поки немає.</div>

  <div v-else-if="!visible.length" class="muted">Нічого не знайдено — спробуйте змінити фільтри.</div>

  <div v-else style="display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 14px">
    <article
      v-for="item in visible"
      :key="item.item_id"
      class="card"
      :style="item.expired ? { borderColor: 'rgba(248, 113, 113, .55)' } : { borderColor: 'rgba(74, 222, 128, .45)' }"
    >
      <div style="display: flex; gap: 14px">
        <ZoomableImage
          v-if="item.image_url"
          :src="imageUrl(item.image_url)!"
          :alt="item.name"
          style="width: 88px; height: 88px; object-fit: contain; border-radius: 8px; background: white; flex: 0 0 auto"
        />
        <div style="min-width: 0">
          <span
            class="badge"
            :class="item.expired ? 'badge-warranty-expired' : item.warranty_expires_at ? 'badge-warranty-active' : 'badge-warranty-unknown'"
          >
            {{ item.expired ? 'Гарантія закінчилася' : item.warranty_expires_at ? 'На гарантії' : 'Термін не визначено' }}
          </span>
          <h3 style="font-size: 15px; margin: 10px 0 4px; line-height: 1.35">{{ item.name }}</h3>
          <div class="muted" style="display: flex; align-items: center; gap: 6px">
            <img
              v-if="storeLogoUrl(item.store_code)"
              :src="storeLogoUrl(item.store_code)!"
              :alt="item.store_name"
              width="20"
              height="20"
              style="border-radius: 4px; object-fit: contain; background: white"
            />
            <span v-else>{{ storeFallbackIcon(item.store_code) }}</span>
            <span>{{ item.store_name }} · {{ item.account_label }}</span>
          </div>
        </div>
      </div>

      <div style="margin-top: 16px; display: grid; grid-template-columns: 1fr 1fr; gap: 10px">
        <div>
          <div class="stat-label">Придбано</div>
          <div>{{ fmtDate(item.purchased_at) }}</div>
        </div>
        <div>
          <div class="stat-label">Ціна</div>
          <div>{{ fmtMoney(item.purchase_price) }}</div>
        </div>
        <div>
          <div class="stat-label">Термін</div>
          <div>{{ item.warranty_text ?? 'Гарантію знайдено' }}</div>
        </div>
        <div>
          <div class="stat-label">Діє до</div>
          <div :style="item.expired ? { color: 'var(--danger)' } : item.warranty_expires_at ? { color: 'var(--success)' } : {}">
            {{ fmtDate(item.warranty_expires_at) }}
          </div>
          <div v-if="!item.expired && daysLeft(item) !== null" class="muted" style="font-size: 12px">
            залишилось {{ daysLeft(item) }} дн.
          </div>
        </div>
      </div>

      <div class="toolbar" style="margin: 16px 0 0">
        <a v-if="item.product_url" :href="item.product_url" target="_blank" rel="noopener noreferrer">
          Картка товару
        </a>
        <RouterLink :to="`/receipts/${item.receipt_id}`">Відкрити чек</RouterLink>
      </div>
    </article>
  </div>
</template>
