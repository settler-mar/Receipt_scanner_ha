import { computed, ref } from 'vue'
import { api } from '@/lib/api'
import { WIDGETS, WIDGET_MAP } from '@/widgets/registry'
import type { DashboardLayout, DashboardRangeKey } from '@/types/api'

const LAYOUT_KEY = 'dashboard.layout'

export interface LayoutEntry {
  key: string
  visible: boolean
  range: DashboardRangeKey | null // null = follow the dashboard's shared range
  /** Per-widget settings the widget itself defines (spend-trend: overlay series). */
  options: Record<string, unknown> | null
}

function defaults(): LayoutEntry[] {
  return WIDGETS.map((w) => ({ key: w.key, visible: w.defaultVisible, range: null, options: null }))
}

// Drop unknown/duplicate keys from the stored layout, then append any widget
// that's new since it was saved (visible by default) — same tolerant pattern as
// the fuel-block config.
function reconcile(stored: DashboardLayout['widgets'] | null | undefined): LayoutEntry[] {
  const known = new Set(WIDGETS.map((w) => w.key))
  const seen = new Set<string>()
  const out: LayoutEntry[] = []
  for (const e of stored ?? []) {
    if (!e || !known.has(e.key) || seen.has(e.key)) continue
    seen.add(e.key)
    out.push({
      key: e.key,
      visible: e.visible !== false,
      range: e.range ?? null,
      options: e.options ?? null,
    })
  }
  for (const w of WIDGETS) {
    if (!seen.has(w.key)) out.push({ key: w.key, visible: w.defaultVisible, range: null, options: null })
  }
  return out
}

export function useDashboardLayout() {
  const entries = ref<LayoutEntry[]>(defaults())
  // false = every widget asks the backend for pre-discount amounts.
  const includeDiscount = ref(true)
  const loaded = ref(false)

  async function load() {
    try {
      const { value } = await api.settings.get<DashboardLayout>(LAYOUT_KEY)
      entries.value = reconcile(value?.widgets)
      includeDiscount.value = value?.include_discount !== false
    } catch {
      entries.value = defaults()
    }
    loaded.value = true
  }

  function save() {
    const payload: DashboardLayout = {
      version: 1,
      widgets: entries.value,
      include_discount: includeDiscount.value,
    }
    void api.settings.set(LAYOUT_KEY, payload)
  }

  function setIncludeDiscount(value: boolean) {
    includeDiscount.value = value
    save()
  }

  /** Visible widgets in stored order, paired with their registry meta. */
  const visible = computed(() =>
    entries.value
      .filter((e) => e.visible && WIDGET_MAP.has(e.key))
      .map((e) => ({ entry: e, meta: WIDGET_MAP.get(e.key)! })),
  )

  function toggle(key: string) {
    const e = entries.value.find((x) => x.key === key)
    if (e) {
      e.visible = !e.visible
      save()
    }
  }

  function move(key: string, dir: -1 | 1) {
    const arr = entries.value
    const i = arr.findIndex((x) => x.key === key)
    const j = i + dir
    const a = arr[i]
    const b = arr[j]
    if (i === -1 || !a || !b) return
    arr[i] = b
    arr[j] = a
    save()
  }

  function setRange(key: string, range: DashboardRangeKey | null) {
    const e = entries.value.find((x) => x.key === key)
    if (e) {
      e.range = range
      save()
    }
  }

  /** Replace a widget's own settings blob (widgets own its shape). */
  function setOptions(key: string, options: Record<string, unknown>) {
    const e = entries.value.find((x) => x.key === key)
    if (e) {
      e.options = options
      save()
    }
  }

  return {
    entries,
    includeDiscount,
    loaded,
    load,
    save,
    visible,
    toggle,
    move,
    setRange,
    setOptions,
    setIncludeDiscount,
  }
}
