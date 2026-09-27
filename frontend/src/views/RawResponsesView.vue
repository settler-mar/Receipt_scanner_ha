<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '@/lib/api'
import type { RawResponseSettings, RawResponseSummary } from '@/types/api'
import { fmtDateTime } from '@/lib/datetime'

const summary = ref<RawResponseSummary | null>(null)
const settings = ref<RawResponseSettings>({ retention_days: 90, auto_cleanup: false })
const loading = ref(true)
const busy = ref('')
const message = ref('')

function formatBytes(value: number) {
  if (value < 1024) return `${value} Б`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} КБ`
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} МБ`
  return `${(value / 1024 / 1024 / 1024).toFixed(2)} ГБ`
}

async function load() {
  loading.value = true
  try {
    ;[summary.value, settings.value] = await Promise.all([
      api.rawResponses.summary(),
      api.rawResponses.settings(),
    ])
  } finally {
    loading.value = false
  }
}

async function saveSettings() {
  busy.value = 'settings'
  message.value = ''
  try {
    settings.value = await api.rawResponses.saveSettings(settings.value)
    message.value = 'Налаштування збережено.'
  } catch (e) {
    message.value = (e as Error).message
  } finally {
    busy.value = ''
  }
}

async function cleanup(mode: 'all' | 'retention') {
  const question = mode === 'all'
    ? 'Видалити всі збережені raw-відповіді?'
    : `Залишити тільки raw-відповіді за останні ${settings.value.retention_days} днів?`
  if (!confirm(question)) return
  busy.value = mode
  message.value = ''
  try {
    const result = await api.rawResponses.cleanup(mode)
    message.value = `Видалено файлів: ${result.removed}.`
    await load()
  } catch (e) {
    message.value = (e as Error).message
  } finally {
    busy.value = ''
  }
}

onMounted(load)
</script>

<template>
  <div class="toolbar">
    <div style="flex: 1">
      <h2 style="margin: 0">Сырые ответы серверов</h2>
      <p class="muted" style="margin: 4px 0 0">
        Ответы провайдеров сохраняются по магазинам для отладки импорта и анализа цен,
        скидок, доставки и сборов.
      </p>
    </div>
  </div>

  <div v-if="loading" class="muted">Завантаження…</div>
  <template v-else>
    <div class="card-grid">
      <div class="card">
        <div class="stat-label">Всего ответов</div>
        <div class="stat">{{ summary?.total_count ?? 0 }}</div>
      </div>
      <div class="card">
        <div class="stat-label">Общий размер</div>
        <div class="stat">{{ formatBytes(summary?.total_bytes ?? 0) }}</div>
      </div>
      <div class="card">
        <div class="stat-label">Магазинов</div>
        <div class="stat">{{ summary?.stores.length ?? 0 }}</div>
      </div>
    </div>

    <section class="card" style="margin-top: 16px">
      <h3 style="margin-top: 0">Хранение</h3>
      <p class="muted" style="margin-top: 0">
        По умолчанию очистка выключена. Период используется кнопкой ручного удаления
        старых ответов и автоматической очисткой, если её включить.
      </p>
      <div class="form-row">
        <label>Хранить дней</label>
        <input v-model.number="settings.retention_days" type="number" min="1" max="3650" style="width: 120px" />
      </div>
      <label style="display: flex; gap: 8px; align-items: center; margin-bottom: 12px">
        <input v-model="settings.auto_cleanup" type="checkbox" style="width: auto" />
        Включить автоматическую очистку
      </label>
      <button :disabled="busy === 'settings'" @click="saveSettings">Сохранить настройки</button>
      <button class="secondary" :disabled="!!busy" style="margin-left: 8px" @click="cleanup('retention')">
        Оставить последние {{ settings.retention_days }} дней
      </button>
      <button class="danger" :disabled="!!busy" style="margin-left: 8px" @click="cleanup('all')">
        Очистить всё
      </button>
      <span v-if="message" class="muted" style="margin-left: 10px">{{ message }}</span>
    </section>

    <section class="card" style="margin-top: 16px">
      <h3 style="margin-top: 0">Сводка по магазинам</h3>
      <table v-if="summary?.stores.length">
        <thead>
          <tr>
            <th>Магазин</th>
            <th>Ответов</th>
            <th>Размер</th>
            <th>Период</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="store in summary.stores" :key="store.store_code">
            <td>{{ store.store_name || store.store_code }}</td>
            <td>{{ store.count }}</td>
            <td>{{ formatBytes(store.bytes) }}</td>
            <td class="muted">
              {{ store.oldest_at ? fmtDateTime(store.oldest_at) : '—' }}
              →
              {{ store.newest_at ? fmtDateTime(store.newest_at) : '—' }}
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted">Сырых ответов пока нет. Они появятся после следующего импорта.</p>
    </section>
  </template>
</template>
