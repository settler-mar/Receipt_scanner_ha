<script setup lang="ts">
/**
 * Transient notification pinned to the top-right corner.
 *
 * Extracted from SettingsView, which grew this pattern first; the markup, the
 * colours and the auto-dismiss timings are its originals so both screens keep
 * announcing things the same way.
 */
import { ref, watch } from 'vue'

export type ToastKind = 'error' | 'success'

const visible = ref(false)
const message = ref('')
const kind = ref<ToastKind>('error')
let timer: number | null = null

function show(text: string, type: ToastKind = 'error') {
  if (!text) return
  message.value = text
  kind.value = type
  visible.value = true
  if (timer !== null) window.clearTimeout(timer)
  // An error is worth re-reading; a confirmation is not.
  timer = window.setTimeout(() => { visible.value = false }, type === 'error' ? 8000 : 3500)
}

function hide() {
  visible.value = false
  if (timer !== null) window.clearTimeout(timer)
}

defineExpose({ show, hide })

/** Convenience: bind a ref of error text and let it raise itself. */
const props = defineProps<{ error?: string }>()
watch(() => props.error, (text) => text && show(text, 'error'))
</script>

<template>
  <Transition name="toast">
    <div v-if="visible" class="toast" :class="`is-${kind}`" role="status" aria-live="polite">
      <span>{{ message }}</span>
      <button type="button" aria-label="Закрити повідомлення" @click="hide">×</button>
    </div>
  </Transition>
</template>

<style scoped>
.toast {
  position: fixed;
  z-index: 10000;
  top: 18px;
  right: 18px;
  display: flex;
  align-items: flex-start;
  gap: 14px;
  width: min(420px, calc(100vw - 36px));
  padding: 13px 14px;
  border: 1px solid var(--border);
  border-left: 4px solid var(--danger);
  border-radius: 10px;
  background: var(--surface, #172033);
  color: var(--text);
  box-shadow: 0 12px 36px rgba(0, 0, 0, 0.35);
}
.toast.is-success {
  border-left-color: #3fb950;
}
.toast span {
  flex: 1;
  line-height: 1.4;
  overflow-wrap: anywhere;
}
.toast button {
  padding: 0;
  border: 0;
  background: transparent;
  color: inherit;
  font-size: 22px;
  line-height: 1;
}
.toast-enter-active,
.toast-leave-active {
  transition: opacity 0.18s ease, transform 0.18s ease;
}
.toast-enter-from,
.toast-leave-to {
  opacity: 0;
  transform: translateY(-8px);
}
</style>
