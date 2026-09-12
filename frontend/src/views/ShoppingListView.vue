<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '@/lib/api'
import { shoppingApi } from '@/lib/shoppingApi'
import type { LiveSuggestStore, ShoppingItem, SuggestStore } from '@/types/shopping'
import { fmtDate } from '@/lib/datetime'

const items = ref<ShoppingItem[]>([])
const suggestions = ref<SuggestStore[]>([])
const liveSuggestions = ref<LiveSuggestStore[] | null>(null)
const liveLoading = ref(false)
const catName = ref<Record<string, string>>({})
const loading = ref(true)
const error = ref('')
const busy = ref(false)

const newText = ref('')
const newQty = ref<number>(1)
const newNote = ref('')

const pending = computed(() => items.value.filter((i) => i.status === 'pending'))

// Pending items grouped by category, "Без категорії" last.
const pendingGroups = computed(() => {
  const map = new Map<string, ShoppingItem[]>()
  for (const i of pending.value) {
    const key = (i.category_id && catName.value[i.category_id]) || ''
    map.set(key, [...(map.get(key) ?? []), i])
  }
  return [...map.entries()].sort(([a], [b]) => {
    if (!a) return 1
    if (!b) return -1
    return a.localeCompare(b, 'uk')
  })
})
const candidates = computed(() => items.value.filter((i) => i.status === 'bought' && i.auto_matched))
const bought = computed(() => items.value.filter((i) => i.status === 'bought' && !i.auto_matched))

function fmtMoney(v: number) {
  return new Intl.NumberFormat('uk-UA', { style: 'currency', currency: 'UAH' }).format(v)
}
function fmtQty(v: number) {
  return String(v)
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [list, sugg, cats] = await Promise.all([
      shoppingApi.list(),
      shoppingApi.suggestStore(),
      api.categories.list().catch(() => []),
    ])
    items.value = list
    suggestions.value = sugg
    catName.value = Object.fromEntries(cats.map((c) => [c.id, c.name]))
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

async function add() {
  const text = newText.value.trim()
  if (!text || busy.value) return
  busy.value = true
  try {
    const lines = text.split('\n').map((l) => l.trim()).filter(Boolean)
    for (const [idx, line] of lines.entries()) {
      // qty / note apply to a single typed item; a pasted multi-line list adds
      // plain rows the user can then adjust inline.
      await shoppingApi.create({
        raw_text: line,
        qty: lines.length === 1 ? Number(newQty.value) || 1 : 1,
        note: lines.length === 1 && idx === 0 ? newNote.value.trim() || null : null,
      })
    }
    newText.value = ''
    newQty.value = 1
    newNote.value = ''
    await load()
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}

async function act(fn: () => Promise<unknown>) {
  busy.value = true
  try {
    await fn()
    await load()
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}

// Inline qty/note edits — patch without a full reload so the input keeps focus.
async function patchField(i: ShoppingItem, patch: { qty?: number; note?: string | null }) {
  try {
    const updated = await shoppingApi.update(i.id, patch)
    Object.assign(i, updated)
  } catch (e) {
    error.value = (e as Error).message
  }
}
const onQty = (i: ShoppingItem, e: Event) =>
  patchField(i, { qty: Number((e.target as HTMLInputElement).value) || 1 })
const onNote = (i: ShoppingItem, e: Event) =>
  patchField(i, { note: (e.target as HTMLInputElement).value.trim() || null })

const markBought = (i: ShoppingItem) => act(() => shoppingApi.update(i.id, { status: 'bought' }))
const markPending = (i: ShoppingItem) => act(() => shoppingApi.update(i.id, { status: 'pending' }))
const remove = (i: ShoppingItem) => act(() => shoppingApi.remove(i.id))
const confirm = (i: ShoppingItem) => act(() => shoppingApi.confirm(i.id))
const reject = (i: ShoppingItem) => act(() => shoppingApi.reject(i.id))
const rescan = () => act(() => shoppingApi.rescan())

async function checkLive() {
  liveLoading.value = true
  error.value = ''
  try {
    liveSuggestions.value = await shoppingApi.suggestStoreLive()
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    liveLoading.value = false
  }
}

onMounted(load)
</script>

<template>
  <h2 style="margin-top: 0">Список покупок</h2>
  <p class="muted" style="margin-top: -8px">
    Ведіть список; після збору чека куплені позиції самі позначаються як «ймовірно куплено» —
    підтвердіть або відхиліть. Підбір магазину рахується з історії ваших чеків, без звернень до AI.
  </p>

  <div v-if="error" class="card" style="border-color: var(--danger); color: var(--danger)">
    {{ error }}
  </div>

  <div class="toolbar" style="align-items: flex-start">
    <textarea
      v-model="newText"
      rows="1"
      placeholder="Назва позиції (можна кілька рядків)…"
      style="flex: 1; min-width: 220px; resize: vertical"
      @keydown.enter.exact.prevent="add"
    />
    <input
      v-model.number="newQty"
      type="number"
      min="0"
      step="0.001"
      title="Кількість"
      style="width: 80px"
    />
    <input
      v-model="newNote"
      type="text"
      placeholder="Примітка"
      style="width: 160px"
      @keydown.enter.prevent="add"
    />
    <button class="btn" :disabled="busy || !newText.trim()" @click="add">Додати</button>
    <button class="btn secondary" :disabled="busy" @click="rescan">Перевірити по чеках</button>
  </div>

  <div v-if="loading" class="muted">Завантаження…</div>

  <template v-else>
    <div v-if="candidates.length" class="card" style="margin-bottom: 16px; border-color: var(--warning)">
      <h3 style="margin-top: 0">Ймовірно куплено — підтвердіть</h3>
      <div
        v-for="i in candidates"
        :key="i.id"
        style="display: flex; gap: 8px; align-items: center; padding: 6px 0; border-bottom: 1px solid var(--border)"
      >
        <span style="flex: 1">
          {{ i.raw_text }}
          <span v-if="i.qty && i.qty !== 1" class="muted">× {{ fmtQty(i.qty) }}</span>
          <span v-if="i.note" class="muted"> · {{ i.note }}</span>
        </span>
        <span class="muted" style="font-size: 12px">{{ fmtDate(i.bought_at) }}</span>
        <button class="btn" :disabled="busy" @click="confirm(i)">Так</button>
        <button class="btn danger" :disabled="busy" @click="reject(i)">Ні</button>
      </div>
    </div>

    <div class="card" style="margin-bottom: 16px">
      <h3 style="margin-top: 0">До покупки ({{ pending.length }})</h3>
      <p v-if="!pending.length" class="muted">Список порожній.</p>
      <template v-for="[cat, rows] in pendingGroups" :key="cat || '_'">
        <div v-if="pendingGroups.length > 1" class="muted" style="font-size: 12px; margin-top: 10px">
          {{ cat || 'Без категорії' }}
        </div>
        <div
          v-for="i in rows"
          :key="i.id"
          style="display: flex; gap: 8px; align-items: center; padding: 6px 0; border-bottom: 1px solid var(--border)"
        >
          <input type="checkbox" :disabled="busy" @change="markBought(i)" />
          <span style="flex: 1">{{ i.raw_text }}</span>
          <input
            :value="i.qty"
            type="number"
            min="0"
            step="0.001"
            title="Кількість"
            style="width: 70px"
            @change="onQty(i, $event)"
          />
          <input
            :value="i.note ?? ''"
            type="text"
            placeholder="примітка"
            style="width: 150px"
            @change="onNote(i, $event)"
          />
          <button class="btn danger" :disabled="busy" @click="remove(i)">✕</button>
        </div>
      </template>
    </div>

    <div v-if="bought.length" class="card" style="margin-bottom: 16px">
      <h3 style="margin-top: 0">Куплено ({{ bought.length }})</h3>
      <div
        v-for="i in bought"
        :key="i.id"
        style="display: flex; gap: 8px; align-items: center; padding: 6px 0; border-bottom: 1px solid var(--border)"
      >
        <input type="checkbox" checked :disabled="busy" @change="markPending(i)" />
        <span style="flex: 1; text-decoration: line-through" class="muted">
          {{ i.raw_text }}
          <span v-if="i.qty && i.qty !== 1">× {{ fmtQty(i.qty) }}</span>
          <span v-if="i.note"> · {{ i.note }}</span>
        </span>
        <button class="btn danger" :disabled="busy" @click="remove(i)">✕</button>
      </div>
    </div>

    <div class="card">
      <div style="display: flex; justify-content: space-between; align-items: baseline; gap: 12px">
        <h3 style="margin-top: 0">Де вигідніше купити</h3>
        <button class="btn secondary" :disabled="liveLoading || !pending.length" @click="checkLive">
          {{ liveLoading ? 'Перевіряємо…' : 'Перевірити наживо' }}
        </button>
      </div>
      <p class="muted" style="margin-top: -4px; font-size: 12px">
        За історією чеків. «Наживо» опитує каталоги магазинів зараз; вільні фрази
        розкладаються під кожну мережу через AI і кешуються.
      </p>
      <p v-if="!suggestions.length" class="muted">
        Замало історії покупок для позицій зі списку.
      </p>
      <table v-else>
        <thead>
          <tr>
            <th>Мережа</th>
            <th>Покриття</th>
            <th>Орієнтовний кошик</th>
            <th>Остання покупка</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in suggestions" :key="s.store_code">
            <td>{{ s.store_name }}</td>
            <td>{{ s.covered_items }} / {{ s.total_items }}</td>
            <td>{{ fmtMoney(s.approx_basket) }}</td>
            <td class="muted">{{ fmtDate(s.last_purchase_at) }}</td>
          </tr>
        </tbody>
      </table>

      <template v-if="liveSuggestions">
        <h4 style="margin: 16px 0 4px">Наживо</h4>
        <p v-if="!liveSuggestions.length" class="muted">
          Жоден магазин з живим каталогом не має позицій зі списку.
        </p>
        <div v-for="s in liveSuggestions" :key="s.store_code" style="margin-bottom: 12px">
          <div style="display: flex; justify-content: space-between">
            <strong>{{ s.store_name }}</strong>
            <span>{{ s.covered_items }} / {{ s.total_items }} · {{ fmtMoney(s.approx_basket) }}</span>
          </div>
          <table>
            <tbody>
              <tr v-for="it in s.items" :key="it.raw_text">
                <td>{{ it.raw_text }}</td>
                <td class="muted" style="font-size: 12px">
                  {{ it.query }}<span v-if="it.query_source === 'ai'"> · AI</span>
                </td>
                <td class="muted">{{ it.matched_name ?? '—' }}</td>
                <td>{{ it.last_price != null ? fmtMoney(it.last_price) : '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
    </div>
  </template>
</template>
