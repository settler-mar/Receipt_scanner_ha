<script setup lang="ts">
// "Враховувати знижку" — the same switch the dashboard toolbar has, repeated in
// each chart header so it's reachable where the numbers are being read. All
// copies drive one shared, persisted value (dashboard.layout.include_discount).
defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [boolean] }>()
</script>

<template>
  <label class="discount-toggle" title="Знято — суми показуються до застосування знижок">
    <input
      type="checkbox"
      :checked="modelValue"
      @change="emit('update:modelValue', ($event.target as HTMLInputElement).checked)"
    />
    <span>Враховувати знижку</span>
  </label>
</template>
