<script setup lang="ts">
// One page for everything the system writes down. The backend merges the
// per-subsystem histories (сбор чеков, сканер, ДПС, AI, Telegram, дубли) with
// the `app_events` journal, so this view only filters and renders.
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { api } from '@/lib/api'
import { fmtDateTime } from '@/lib/datetime'
import type { LogEntry, Problem } from '@/types/api'

const PAGE = 100

const items = ref<LogEntry[]>([])
const problems = ref<Problem[]>([])
const sources = ref<{ code: string; name: string }[]>([])
const hasMore = ref(false)
const loading = ref(true)
const error = ref('')
const source = ref('all')
const level = ref('')
const query = ref('')
const autoRefresh = ref(false)
const expanded = ref<Set<string>>(new Set())
let timer: number | undefined

const levelLabel: Record<string, string> = { info: 'Инфо', warning: 'Предупреждение', error: 'Ошибка' }

async function loadProblems() {
  try {
    problems.value = (await api.logs.problems()).items
  } catch {
    problems.value = []
  }
}

async function load(more = false) {
  loading.value = true
  error.value = ''
  try {
    const page = await api.logs.list({
      source: source.value === 'all' ? undefined : source.value,
      level: level.value || undefined,
      q: query.value.trim() || undefined,
      limit: PAGE,
      offset: more ? items.value.length : 0,
    })
    items.value = more ? [...items.value, ...page.items] : page.items
    hasMore.value = page.has_more
    sources.value = page.sources
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

function toggle(entry: LogEntry) {
  const next = new Set(expanded.value)
  next.has(entry.id) ? next.delete(entry.id) : next.add(entry.id)
  expanded.value = next
}
const hasDetails = (entry: LogEntry) => Object.keys(entry.details || {}).length > 0
const detailLines = (entry: LogEntry) =>
  Object.entries(entry.details || {})
    .filter(([, v]) => v !== null && v !== undefined && v !== '')
    .map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`)

const counts = computed(() => {
  const map: Record<string, number> = {}
  for (const i of items.value) map[i.level] = (map[i.level] ?? 0) + 1
  return map
})

// Typing shouldn't fire a request per keystroke.
let debounce: number | undefined
watch(query, () => {
  window.clearTimeout(debounce)
  debounce = window.setTimeout(() => load(), 350)
})
watch([source, level], () => load())
watch(autoRefresh, (on) => {
  window.clearInterval(timer)
  if (on) timer = window.setInterval(() => load(), 15000)
})

onMounted(() => {
  void loadProblems()
  void load()
})
onBeforeUnmount(() => {
  window.clearInterval(timer)
  window.clearTimeout(debounce)
})
</script>

<template>
  <h2 style="margin-top: 0">Журнал</h2>
  <p class="muted" style="margin-top: -8px">
    Всё, что система записала: сбор чеков, сканер, очередь ДПС, AI, Telegram,
    поиск дублей и автоматические действия со списком покупок — в одной ленте.
  </p>

  <div v-if="error" class="card" style="border-color: var(--danger); color: var(--danger)">{{ error }}</div>

  <!-- Что сломано прямо сейчас — именно это число показывает бейдж в меню.
       Как только проблема решена (повторный вход, удачный прогон, обработанный
       документ), строка исчезает сама. -->
  <div v-if="problems.length" class="card" style="border-color: var(--danger); margin-bottom: 16px">
    <h3 style="margin-top: 0">Активные проблемы ({{ problems.length }})</h3>
    <div v-for="p in problems" :key="p.id" class="log-row" :class="p.level">
      <span class="log-ts muted">{{ p.since ? fmtDateTime(p.since) : '—' }}</span>
      <span class="log-source">{{ p.source_name }}</span>
      <span class="log-body">
        <span class="log-title">{{ p.title }}</span>
        <span v-if="p.message" class="log-message muted">{{ p.message }}</span>
      </span>
      <RouterLink v-if="p.link" :to="p.link" class="log-link muted" title="Перейти">↗</RouterLink>
    </div>
  </div>

  <div class="toolbar">
    <select v-model="source" title="Источник" style="min-width: 170px">
      <option value="all">Все источники</option>
      <option v-for="s in sources" :key="s.code" :value="s.code">{{ s.name }}</option>
    </select>
    <select v-model="level" title="Уровень" style="min-width: 150px">
      <option value="">Любой уровень</option>
      <option value="info">Инфо</option>
      <option value="warning">Предупреждения</option>
      <option value="error">Ошибки</option>
    </select>
    <input v-model="query" type="search" placeholder="Поиск по тексту…" style="min-width: 200px; flex: 1" />
    <button class="btn secondary" :disabled="loading" @click="load()">Оновити</button>
    <label class="muted" style="display: flex; align-items: center; gap: 6px; font-size: 12px">
      <input v-model="autoRefresh" type="checkbox" /> каждые 15 с
    </label>
  </div>

  <p class="muted" style="font-size: 12px">
    Показано {{ items.length }}<span v-if="counts.error"> · ошибок: {{ counts.error }}</span>
    <span v-if="counts.warning"> · предупреждений: {{ counts.warning }}</span>
  </p>

  <div class="card">
    <p v-if="!items.length && !loading" class="muted">Записей нет.</p>
    <div v-for="entry in items" :key="entry.id" class="log-row" :class="entry.level">
      <span class="log-ts muted">{{ fmtDateTime(entry.ts) }}</span>
      <span class="log-source">{{ entry.source_name }}</span>
      <span class="log-body">
        <span class="log-title">{{ entry.title }}</span>
        <span v-if="entry.message" class="log-message muted">{{ entry.message }}</span>
        <span v-if="expanded.has(entry.id)" class="log-details muted">
          <span v-for="line in detailLines(entry)" :key="line">{{ line }}</span>
        </span>
      </span>
      <RouterLink v-if="entry.link" :to="entry.link" class="log-link muted" title="Перейти к записи">↗</RouterLink>
      <button v-if="hasDetails(entry)" class="log-more" title="Подробности" @click="toggle(entry)">
        {{ expanded.has(entry.id) ? '−' : '⋯' }}
      </button>
    </div>
    <div v-if="hasMore" style="margin-top: 12px">
      <button class="btn secondary" :disabled="loading" @click="load(true)">Показать ещё</button>
    </div>
  </div>
</template>

<style scoped>
.log-row {
  display: flex;
  gap: 10px;
  align-items: baseline;
  padding: 6px 0 6px 8px;
  border-bottom: 1px solid var(--border);
  border-left: 3px solid transparent;
  font-size: 13px;
}
.log-row:last-child {
  border-bottom: none;
}
.log-row.warning {
  border-left-color: var(--warning);
}
.log-row.error {
  border-left-color: var(--danger);
}
.log-ts {
  flex: 0 0 140px;
  font-variant-numeric: tabular-nums;
  font-size: 12px;
}
.log-source {
  flex: 0 0 120px;
  font-size: 12px;
  opacity: 0.8;
}
.log-body {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.log-title {
  font-weight: 500;
}
.log-message {
  font-size: 12px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.log-details {
  font-size: 12px;
  display: flex;
  flex-direction: column;
  gap: 1px;
  opacity: 0.8;
}
.log-more,
.log-link {
  flex: 0 0 auto;
  background: none;
  border: none;
  color: inherit;
  opacity: 0.6;
  cursor: pointer;
  padding: 0 4px;
  text-decoration: none;
}
.log-more:hover,
.log-link:hover {
  opacity: 1;
}

@media (max-width: 720px) {
  .log-row {
    flex-wrap: wrap;
  }
  .log-ts,
  .log-source {
    flex: 0 0 auto;
  }
  .log-body {
    flex: 1 1 100%;
  }
}
</style>
