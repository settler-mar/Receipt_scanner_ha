<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { BASE } from '@/lib/api'
import { RANGES, DEFAULT_RANGE } from '@/lib/dashboardFmt'
import { useDashboardLayout, type LayoutEntry } from '@/composables/useDashboardLayout'
import { WIDGET_MAP } from '@/widgets/registry'
import DiscountToggle from '@/components/DiscountToggle.vue'
import type { DashboardRangeKey } from '@/types/api'

const range = ref<DashboardRangeKey>(DEFAULT_RANGE) // shared, session-only (not persisted)
const configOpen = ref(false)

const {
  entries,
  includeDiscount,
  loaded,
  load,
  visible,
  toggle,
  move,
  setRange,
  setOptions,
  setIncludeDiscount,
} = useDashboardLayout()
onMounted(load)

function effectiveRange(entry: LayoutEntry): DashboardRangeKey {
  return entry.range ?? range.value
}

// ---- "Вивести в Home Assistant" -------------------------------------------
const haWidgetKey = ref<string | null>(null)
const haCopied = ref('')

// BASE is "<proto>//<host>:8001"; the SPA lives at its root, hash-routed.
const originFor = computed(() => BASE.replace(/\/$/, ''))

const haWidgetTitle = computed(() =>
  haWidgetKey.value ? WIDGET_MAP.get(haWidgetKey.value)?.title ?? haWidgetKey.value : '',
)

const haIframeYaml = computed(() => {
  const key = haWidgetKey.value
  if (!key) return ''
  return [
    '# Lovelace: візуальний віджет як iframe-картка',
    'type: iframe',
    `url: "${originFor.value}/#/w/${key}?range=${range.value}${includeDiscount.value ? '' : '&discount=0'}"`,
    'aspect_ratio: 60%',
  ].join('\n')
})

const haSensorYaml = computed(() => {
  const key = haWidgetKey.value
  if (!key) return ''
  return [
    '# configuration.yaml: числові показники віджета як rest-сенсор',
    'sensor:',
    '  - platform: rest',
    `    name: "Receipt Scanner — ${haWidgetTitle.value}"`,
    `    resource: "${originFor.value}/api/widgets/${key}${includeDiscount.value ? '' : '?include_discount=false'}"`,
    '    value_template: "{{ (value_json.summary.values() | list | first) | default(0) }}"',
    '    json_attributes_path: "$.summary"',
    '    json_attributes:',
    '      - total_spent',
    '      - total_receipts',
    '    scan_interval: 3600',
  ].join('\n')
})

async function copyHa(text: string, which: string) {
  try {
    await navigator.clipboard.writeText(text)
    haCopied.value = which
    setTimeout(() => (haCopied.value = ''), 1500)
  } catch {
    haCopied.value = ''
  }
}

function openHa(key: string) {
  haWidgetKey.value = key
  haCopied.value = ''
}

const orderedEntries = computed(() => entries.value)
</script>

<template>
  <div class="toolbar">
    <h2 style="margin: 0; flex: 1">Дашборд</h2>
    <button
      v-for="r in RANGES"
      :key="r.key"
      class="secondary"
      :style="range === r.key ? 'background: var(--accent-dim); color: white' : ''"
      @click="range = r.key"
    >
      {{ r.label }}
    </button>
    <DiscountToggle :model-value="includeDiscount" @update:model-value="setIncludeDiscount" />
    <button class="secondary" :class="{ active: configOpen }" @click="configOpen = !configOpen">
      ⚙ Налаштувати
    </button>
  </div>

  <div v-if="configOpen" class="card config-panel">
    <h3 style="margin-top: 0">Віджети дашборда</h3>
    <p class="muted" style="margin-top: -6px">
      Виберіть, що показувати та в якому порядку. Кнопка «HA» дає готовий сніппет для панелі Home Assistant.
    </p>
    <ul class="config-list">
      <li v-for="(entry, i) in orderedEntries" :key="entry.key">
        <label class="config-visible">
          <input type="checkbox" :checked="entry.visible" @change="toggle(entry.key)" />
          <span>{{ WIDGET_MAP.get(entry.key)?.title ?? entry.key }}</span>
        </label>
        <select
          v-if="WIDGET_MAP.get(entry.key)?.supportsRange"
          :value="entry.range ?? ''"
          @change="setRange(entry.key, ($event.target as HTMLSelectElement).value as DashboardRangeKey || null)"
        >
          <option value="">— як на дашборді —</option>
          <option v-for="r in RANGES" :key="r.key" :value="r.key">{{ r.label }}</option>
        </select>
        <span class="config-actions">
          <button class="secondary" :disabled="i === 0" title="Вгору" @click="move(entry.key, -1)">↑</button>
          <button
            class="secondary"
            :disabled="i === orderedEntries.length - 1"
            title="Вниз"
            @click="move(entry.key, 1)"
          >
            ↓
          </button>
          <button class="secondary" title="Вивести в Home Assistant" @click="openHa(entry.key)">HA</button>
        </span>
      </li>
    </ul>
  </div>

  <p v-if="!loaded" class="muted">Завантаження…</p>
  <template v-else>
    <component
      :is="meta.component"
      v-for="{ entry, meta } in visible"
      :key="entry.key"
      :range="effectiveRange(entry)"
      :include-discount="meta.supportsDiscount ? includeDiscount : undefined"
      :options="meta.supportsOptions ? entry.options : undefined"
      @update:include-discount="setIncludeDiscount"
      @update:options="setOptions(entry.key, $event)"
      class="dashboard-widget"
    />
  </template>

  <div v-if="haWidgetKey" class="modal-backdrop" @click.self="haWidgetKey = null">
    <div class="modal ha-modal">
      <h3 style="margin-top: 0">Home Assistant — {{ haWidgetTitle }}</h3>
      <p class="muted" style="margin-top: -6px">
        Скопіюйте один зі сніппетів у свою конфігурацію Home Assistant.
      </p>

      <div class="ha-snippet">
        <pre>{{ haIframeYaml }}</pre>
        <button class="secondary" @click="copyHa(haIframeYaml, 'iframe')">
          {{ haCopied === 'iframe' ? 'Скопійовано ✓' : 'Копіювати картку' }}
        </button>
      </div>

      <div class="ha-snippet">
        <pre>{{ haSensorYaml }}</pre>
        <button class="secondary" @click="copyHa(haSensorYaml, 'sensor')">
          {{ haCopied === 'sensor' ? 'Скопійовано ✓' : 'Копіювати сенсор' }}
        </button>
      </div>

      <div class="toolbar" style="margin: 12px 0 0; justify-content: flex-end">
        <button class="secondary" @click="haWidgetKey = null">Закрити</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.dashboard-widget {
  display: block;
  margin-bottom: 24px;
}

.config-panel {
  margin-bottom: 24px;
}

.config-list {
  list-style: none;
  padding: 0;
  margin: 0;
}

.config-list li {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 6px 0;
  border-top: 1px solid var(--border);
}

.config-list li:first-child {
  border-top: none;
}

.config-visible {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  cursor: pointer;
}

.config-visible input {
  accent-color: var(--accent);
  width: 15px;
  height: 15px;
}

.config-actions {
  display: inline-flex;
  gap: 4px;
}

.config-actions button {
  padding: 4px 9px;
}

button.secondary.active {
  background: var(--accent-dim);
  color: white;
}

.ha-modal {
  width: min(560px, 92vw);
}

.ha-snippet {
  margin-bottom: 14px;
}

.ha-snippet pre {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
  font-size: 12px;
  overflow-x: auto;
  white-space: pre;
  margin: 0 0 6px;
}
</style>
