<script setup lang="ts">
// One action bar for both list blocks ("До покупки" and "Куплено"). Assignment
// happens on select change — "снять" is an option inside the same dropdown
// instead of a second button, which keeps the bar to four controls per block.
import { ref } from 'vue'

const props = defineProps<{
  count: number
  mode: 'pending' | 'bought'
  busy: boolean
  stores: { code: string; name: string }[]
  categories: { id: string; name: string }[]
}>()

const emit = defineEmits<{
  (e: 'status'): void
  (e: 'assign', patch: { category_id?: string | null; target_store_code?: string | null; tag?: string | null }): void
  (e: 'remove'): void
  (e: 'clear'): void
}>()

const tag = ref('')

// Each select is a one-shot command, so it resets to its placeholder after use.
function onStore(e: Event) {
  const el = e.target as HTMLSelectElement
  const value = el.value
  el.value = ''
  if (value) emit('assign', { target_store_code: value === '__clear__' ? null : value })
}
function onCategory(e: Event) {
  const el = e.target as HTMLSelectElement
  const value = el.value
  el.value = ''
  if (value) emit('assign', { category_id: value === '__clear__' ? null : value })
}
function applyTag() {
  const value = tag.value.trim()
  emit('assign', { tag: value || null })
  tag.value = ''
}
function remove() {
  const word = props.count === 1 ? 'позицию' : 'позиций'
  if (window.confirm(`Удалить ${props.count} ${word}? Действие необратимо.`)) emit('remove')
}
</script>

<template>
  <div class="card bulk-bar">
    <span class="muted">Выбрано: {{ count }}</span>
    <button class="btn" :disabled="busy" @click="emit('status')">
      {{ mode === 'pending' ? 'Отметить купленным' : 'Вернуть в список' }}
    </button>
    <select :disabled="busy" title="Магазин для выбранных" style="width: 165px" @change="onStore">
      <option value="">Магазин…</option>
      <option value="__clear__">— снять магазин —</option>
      <option v-for="store in stores" :key="store.code" :value="store.code">{{ store.name }}</option>
    </select>
    <select :disabled="busy" title="Категория для выбранных" style="width: 175px" @change="onCategory">
      <option value="">Категория…</option>
      <option value="__clear__">— снять категорию —</option>
      <option v-for="category in categories" :key="category.id" :value="category.id">{{ category.name }}</option>
    </select>
    <input
      v-model="tag"
      type="text"
      placeholder="тег (пусто = снять)"
      title="Тег для выбранных"
      style="width: 150px"
      @keydown.enter.prevent="applyTag"
    />
    <button class="btn secondary" :disabled="busy" @click="applyTag">Проставить тег</button>
    <button class="btn danger" :disabled="busy" @click="remove">Удалить</button>
    <button class="btn secondary" :disabled="busy" @click="emit('clear')">Снять выбор</button>
  </div>
</template>

<style scoped>
.bulk-bar {
  margin: 0 0 12px;
  padding: 10px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
</style>
