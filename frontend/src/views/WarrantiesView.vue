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
      <div class="muted">
        Діючі: {{ activeCount }} · Термін не визначено: {{ unknownCount }} · Закінчилися: {{ expiredCount }}
      </div>
    </div>
  </div>

  <div v-if="loading" class="muted">Завантаження гарантій…</div>
  <div v-else-if="error" style="color: var(--danger)">{{ error }}</div>
  <div v-else-if="!products.length" class="muted">Товарів із знайденою гарантією поки немає.</div>

  <div v-else style="display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 14px">
    <article
      v-for="item in products"
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
