<script setup lang="ts">
import { computed, ref } from 'vue'
// The package's root export is condition-only (browser/node/electron), which
// resolves at bundle time but not for the type-checker; the explicit browser
// subpath is the same module with types attached.
import bwipjs, { type RenderOptions } from 'bwip-js/browser'
import type { LoyaltyCode } from '@/types/api'
import { fmtDateTime } from '@/lib/datetime'

const props = defineProps<{ code: LoyaltyCode }>()

// The backend says how the real app renders it, but any code is just a string —
// rendering it another way often works too, so let the user pick whichever their
// register/pump scanner reads best (some are QR-only, some are 1D-only; PDF417
// scanners are usually the omnidirectional imagers that also read the other two).
const format = ref<'qr' | 'barcode' | 'pdf417'>(props.code.display)

// One renderer (bwip-js, SVG) for all three — canvas rendering of this library
// came out solid black in this browser (confirmed: same params render a correct
// image via its Node/buffer backend, so it's specific to the browser canvas
// path here) — SVG output sidesteps that entirely. A single renderer also means
// one consistent box size instead of each format's own canvas pixel dimensions
// yanking the layout around when switching.
const BCID: Record<'qr' | 'barcode' | 'pdf417', string> = {
  qr: 'qrcode',
  barcode: 'code128',
  pdf417: 'pdf417',
}

const svg = computed(() => {
  // bwip-js's option validator chokes on an explicitly-undefined `height` key
  // (present-but-undefined isn't the same as absent) — only 1D/2D-stacked
  // formats want it set at all, so build the object conditionally rather than
  // passing `height: undefined` for QR.
  const opts: RenderOptions = {
    bcid: BCID[format.value],
    text: props.code.value,
    scale: format.value === 'qr' ? 5 : 3,
    includetext: false,
  }
  if (format.value !== 'qr') opts.height = 12
  try {
    return bwipjs.toSVG(opts)
  } catch {
    return ''
  }
})

function fmtDate(v: string) {
  return fmtDateTime(v)
}
</script>

<template>
  <div style="text-align: center">
    <div class="code-box" :class="{ square: format === 'qr' }">
      <!-- eslint-disable-next-line vue/no-v-html -- svg is generated locally by bwip-js, not user input -->
      <div class="code-svg" v-html="svg"></div>
    </div>
    <p class="muted" style="margin-bottom: 4px">{{ code.value }}</p>
    <p v-if="code.expires_at" class="muted" style="margin-top: 0; font-size: 12px">
      Дійсний до {{ fmtDate(code.expires_at) }}
    </p>
    <div class="toolbar" style="justify-content: center; margin-bottom: 0">
      <button
        class="secondary"
        :style="format === 'qr' ? 'background: var(--accent-dim); color: white' : ''"
        @click="format = 'qr'"
      >
        QR
      </button>
      <button
        class="secondary"
        :style="format === 'barcode' ? 'background: var(--accent-dim); color: white' : ''"
        @click="format = 'barcode'"
      >
        Штрихкод
      </button>
      <button
        class="secondary"
        :style="format === 'pdf417' ? 'background: var(--accent-dim); color: white' : ''"
        @click="format = 'pdf417'"
      >
        PDF417
      </button>
    </div>
  </div>
</template>

<style scoped>
.code-box {
  background: #fff;
  padding: 16px;
  border-radius: 8px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 260px;
  height: 120px;
}
.code-box.square {
  width: 220px;
  height: 220px;
}
.code-svg {
  display: flex;
  width: 100%;
  height: 100%;
}
.code-svg :deep(svg) {
  display: block;
  width: 100%;
  height: 100%;
}
</style>
