<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'

defineOptions({ inheritAttrs: false })

const props = withDefaults(
  defineProps<{
    src: string
    alt?: string
    /** Size of the hover preview popup, in px. */
    previewSize?: number
  }>(),
  { alt: '', previewSize: 340 },
)

const hovering = ref(false)
const lightbox = ref(false)
const popupStyle = ref<Record<string, string>>({})

function onEnter(e: MouseEvent) {
  positionPopup(e)
  hovering.value = true
}

function positionPopup(e: MouseEvent) {
  const margin = 12
  const size = props.previewSize
  let left = e.clientX + margin
  let top = e.clientY + margin
  if (left + size > window.innerWidth) left = e.clientX - size - margin
  if (top + size > window.innerHeight) top = window.innerHeight - size - margin
  if (top < margin) top = margin
  popupStyle.value = { left: `${Math.max(margin, left)}px`, top: `${top}px`, width: `${size}px`, height: `${size}px` }
}

function openLightbox() {
  hovering.value = false
  lightbox.value = true
  window.addEventListener('keydown', onKey)
}

function closeLightbox() {
  lightbox.value = false
  window.removeEventListener('keydown', onKey)
}

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') closeLightbox()
}

onBeforeUnmount(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <img
    v-bind="$attrs"
    :src="src"
    :alt="alt"
    class="zoomable-image"
    @mouseenter="onEnter"
    @mousemove="hovering && positionPopup($event)"
    @mouseleave="hovering = false"
    @click="openLightbox"
  />

  <Teleport to="body">
    <div v-if="hovering" class="zoomable-image__popup" :style="popupStyle">
      <img :src="src" :alt="alt" />
    </div>

    <div v-if="lightbox" class="zoomable-image__lightbox" @click="closeLightbox">
      <img :src="src" :alt="alt" @click.stop />
      <button class="zoomable-image__close" type="button" aria-label="Закрити" @click="closeLightbox">×</button>
    </div>
  </Teleport>
</template>

<style scoped>
.zoomable-image {
  cursor: zoom-in;
}

.zoomable-image__popup {
  position: fixed;
  z-index: 9998;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg, #fff);
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.35);
  padding: 6px;
  pointer-events: none;
}

.zoomable-image__popup img {
  width: 100%;
  height: 100%;
  object-fit: contain;
}

.zoomable-image__lightbox {
  position: fixed;
  inset: 0;
  z-index: 9999;
  display: grid;
  place-items: center;
  padding: 32px;
  background: rgba(0, 0, 0, 0.8);
  cursor: zoom-out;
}

.zoomable-image__lightbox img {
  max-width: 90vw;
  max-height: 90vh;
  object-fit: contain;
  border-radius: 8px;
  background: #fff;
}

.zoomable-image__close {
  position: fixed;
  top: 16px;
  right: 20px;
  width: 40px;
  height: 40px;
  font-size: 24px;
  line-height: 1;
  border-radius: 50%;
  border: none;
  cursor: pointer;
}

@media (hover: none) {
  .zoomable-image__popup {
    display: none;
  }
}
</style>
