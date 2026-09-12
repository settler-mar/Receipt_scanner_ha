<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { api } from '@/lib/api'
import { BASE } from '@/lib/api'
import ToastMessage from '@/components/ToastMessage.vue'
import ZoomableImage from '@/components/ZoomableImage.vue'
import { fmtDateTime } from '@/lib/datetime'
import type {
  DriveFolder,
  DriveStatus,
  InboxDocument,
  ScanJob,
  ScanStats,
  UploadResult,
  VoiceMessage,
} from '@/types/api'

const docs = ref<InboxDocument[]>([])
const voices = ref<VoiceMessage[]>([])
const voiceBusy = ref('')
const scans = ref<ScanJob[]>([])
const stats = ref<ScanStats | null>(null)
const drive = ref<DriveStatus | null>(null)
const loading = ref(true)
const uploading = ref(false)
const error = ref('')
const names = ref<Record<string, string>>({})
const folders = ref<Record<string, { id: string; name: string; color: string | null }>>({})

// One shared folder browser, opened for whichever document is being filed.
const pickerFor = ref<string | null>(null)
const pickerStack = ref<{ id: string | null; name: string }[]>([{ id: null, name: 'Мій диск' }])
const pickerFolders = ref<DriveFolder[]>([])

// A receipt longer than the camera frame is shot in overlapping parts; with this
// on, a multi-file pick is merged into one receipt instead of becoming several.
const seriesMode = ref(false)
const showArchived = ref(false)
// The document queue hides finished files the same way the scan journal hides
// archived rows — both lists are working queues, not history.
const showDoneDocs = ref(false)

async function refresh() {
  loading.value = true
  try {
    ;[docs.value, scans.value, stats.value, drive.value, voices.value] = await Promise.all([
      api.inbox.list(showDoneDocs.value),
      api.inbox.scans(showArchived.value),
      api.inbox.scanStats(),
      api.inbox.driveStatus(),
      api.voice.list(),
    ])
    // Rows that are gone (deleted, or filtered out of this listing) must not
    // stay selected: a later action would target ids the journal no longer has.
    const live = new Set(scans.value.map((j) => j.id))
    if ([...selected.value].some((id) => !live.has(id))) {
      selected.value = new Set([...selected.value].filter((id) => live.has(id)))
    }
    // A freshly listed document gets its name pre-filled from the uploaded file,
    // so filing it is usually a click rather than retyping what is already known.
    for (const d of docs.value) {
      if (names.value[d.id] === undefined) names.value[d.id] = suggestName(d)
    }
  } finally {
    loading.value = false
  }
}

/** Original filename without its extension — the usual starting point for a name. */
function suggestName(doc: InboxDocument): string {
  const raw = (doc.original_filename || '').trim()
  if (!raw) return ''
  return raw.replace(/\.[a-z0-9]{1,5}$/i, '')
}

// Recognition is a separate step from keeping the recording: a message can be
// stored with speech-to-text off and read later, or read again after the AI
// settings change (re-reading an existing transcript costs no provider call).
async function recognizeVoice(row: VoiceMessage, language?: string) {
  voiceBusy.value = row.id
  try {
    const next = await api.voice.transcribe(row.id, language)
    voices.value = voices.value.map((v) => (v.id === next.id ? next : v))
    ;[scans.value, stats.value] = await Promise.all([
      api.inbox.scans(showArchived.value),
      api.inbox.scanStats(),
    ])
  } catch (e) {
    error.value = (e as Error).message
    voices.value = await api.voice.list()
  } finally {
    voiceBusy.value = ''
  }
}

async function applyVoice(row: VoiceMessage) {
  voiceBusy.value = row.id
  try {
    const next = await api.voice.apply(row.id)
    voices.value = voices.value.map((v) => (v.id === next.id ? next : v))
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    voiceBusy.value = ''
  }
}

async function removeVoice(row: VoiceMessage) {
  if (!confirm('Видалити голосове повідомлення разом із записом?')) return
  voiceBusy.value = row.id
  try {
    await api.voice.remove(row.id)
    voices.value = voices.value.filter((v) => v.id !== row.id)
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    voiceBusy.value = ''
  }
}

/** Languages worth offering as a retry: everything except the one already used. */
function otherLanguages(row: VoiceMessage) {
  return row.languages.filter((l) => l.code !== row.language)
}

function languageLabel(row: VoiceMessage) {
  return row.languages.find((l) => l.code === row.language)?.label ?? row.language
}

function voiceLength(row: VoiceMessage) {
  const parts = []
  if (row.duration_s) parts.push(`${row.duration_s} с`)
  if (row.size_bytes) parts.push(`${Math.round(row.size_bytes / 1024)} КБ`)
  return parts.join(' · ')
}

onMounted(() => {
  refresh()
  window.addEventListener('paste', onPaste)
})
onUnmounted(() => window.removeEventListener('paste', onPaste))
watch([showArchived, showDoneDocs], refresh)

async function openPicker(docId: string) {
  pickerFor.value = pickerFor.value === docId ? null : docId
  if (!pickerFor.value) return
  const base = drive.value?.root_folder_id
    ? { id: drive.value.root_folder_id, name: drive.value.root_folder_name || 'Коренева папка' }
    : { id: null as string | null, name: 'Мій диск' }
  pickerStack.value = [base]
  await browse(base, 0)
}

async function browse(f: { id: string | null; name: string }, depth: number) {
  pickerStack.value = pickerStack.value.slice(0, depth + 1)
  if (pickerStack.value[depth]?.id !== f.id) pickerStack.value.push(f)
  try {
    pickerFolders.value = await api.inbox.driveFolders(f.id ?? undefined)
  } catch (e) {
    error.value = (e as Error).message
  }
}

function chooseFolder(f: DriveFolder) {
  if (pickerFor.value) folders.value[pickerFor.value] = { id: f.id, name: f.name, color: f.color }
  pickerFor.value = null
}

// --- Upload -----------------------------------------------------------------
// Files arrive by drop, by paste or from a picker, one or many at a time; all
// three land here so the outcome is reported the same way for each file.
interface UploadEntry {
  id: number
  name: string
  state: 'busy' | 'ok' | 'warn' | 'error'
  text: string
  receiptId?: string | null
}
const uploads = ref<UploadEntry[]>([])
const dragOver = ref(false)
let uploadSeq = 0

function patchUpload(id: number, patch: Partial<UploadEntry>) {
  uploads.value = uploads.value.map((e) => (e.id === id ? { ...e, ...patch } : e))
}

async function uploadFiles(list: FileList | File[] | null | undefined) {
  const files = Array.from(list || []).filter((f) => f.type.startsWith('image/'))
  if (!files.length) return
  if (seriesMode.value && files.length > 1) return uploadSeries(files)
  error.value = ''
  uploading.value = true
  // Newest first: during a long batch the file being worked on stays on screen.
  const pending = files.map((file) => ({
    file,
    entry: {
      id: ++uploadSeq,
      name: file.name || 'зображення',
      state: 'busy',
      text: 'Завантаження…',
    } as UploadEntry,
  }))
  uploads.value = [...pending.map((p) => p.entry), ...uploads.value].slice(0, 20)
  try {
    for (const { file, entry } of pending) {
      try {
        const r = await api.inbox.upload(file)
        patchUpload(entry.id, { state: uploadState(r), text: resultText(r), receiptId: r.receipt_id })
      } catch (err) {
        patchUpload(entry.id, { state: 'error', text: (err as Error).message })
      }
    }
    await refresh()
  } finally {
    uploading.value = false
  }
}

/** One long receipt shot in parts: the server merges the frames by their
 *  overlapping lines and leaves a draft to check. */
async function uploadSeries(files: File[], scanJobId?: string) {
  error.value = ''
  uploading.value = true
  const entry: UploadEntry = {
    id: ++uploadSeq,
    name: `${files.length} фото · один чек`,
    state: 'busy',
    text: 'Склеювання…',
  }
  uploads.value = [entry, ...uploads.value].slice(0, 20)
  try {
    const batch = await api.inbox.uploadSeries(files, scanJobId)
    const first = batch.results[0]
    if (batch.needs_choice) {
      // The QR codes did not settle it — the row asks, nothing is recognised yet.
      patchUpload(entry.id, { state: 'warn', text: batch.note })
    } else if (batch.merged && first) {
      patchUpload(entry.id, {
        state: uploadState(first),
        text: resultText(first),
        receiptId: first.receipt_id,
      })
    } else {
      // Different receipts photographed in one go: say so, and list them.
      patchUpload(entry.id, {
        state: batch.results.every((r) => r.receipt_id) ? 'ok' : 'warn',
        text: `${batch.note || 'Різні чеки'}: ${batch.results.map(resultText).join(' · ')}`,
      })
    }
    await refresh()
    if (batch.merged && first?.scan_job_id) goToScan(first.scan_job_id)
  } catch (err) {
    patchUpload(entry.id, { state: 'error', text: (err as Error).message })
  } finally {
    uploading.value = false
  }
}

/** The two buttons on a batch row: one long receipt, or separate receipts. */
async function resolveSeries(job: ScanJob, choice: 'one' | 'separate') {
  busyScan.value = job.id
  try {
    const batch = await api.inbox.resolveSeries(job.id, choice)
    noteRow(job.id, batch.note || (choice === 'one' ? 'Склеєно в один чек' : 'Розділено на окремі чеки'))
    ;[scans.value, stats.value] = await Promise.all([
      api.inbox.scans(showArchived.value),
      api.inbox.scanStats(),
    ])
    if (batch.results[0]?.scan_job_id) goToScan(batch.results[0].scan_job_id!)
  } catch (e) {
    noteRow(job.id, (e as Error).message, true)
  } finally {
    busyScan.value = null
  }
}

/** "Add one more photo" on a draft row — the frame joins that scan. */
function addSeriesPhoto(job: ScanJob, event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files || [])
  input.value = ''
  if (files.length) return uploadSeries(files, job.id)
}

function uploadState(r: UploadResult): UploadEntry['state'] {
  if (r.kind === 'document' || r.receipt_id) return 'ok'
  return r.needs_review ? 'warn' : 'error'
}

function onFile(e: Event) {
  const input = e.target as HTMLInputElement
  const files = input.files
  // Clear it first so picking the same file again still fires a change event.
  input.value = ''
  return uploadFiles(files)
}

function onDrop(e: DragEvent) {
  dragOver.value = false
  return uploadFiles(e.dataTransfer?.files)
}

function onPaste(e: ClipboardEvent) {
  // Only screenshots and copied images; pasting into an input must stay typing.
  const target = e.target as HTMLElement | null
  if (target && /^(INPUT|TEXTAREA)$/.test(target.tagName)) return
  const files = Array.from(e.clipboardData?.files || []).filter((f) => f.type.startsWith('image/'))
  if (files.length) uploadFiles(files)
}

async function processDoc(doc: InboxDocument) {
  const final_name = (names.value[doc.id] || '').trim()
  if (!final_name) {
    error.value = 'Вкажіть імʼя файлу'
    return
  }
  error.value = ''
  try {
    await api.inbox.process(doc.id, {
      final_name,
      target_folder_id: folders.value[doc.id]?.id || null,
    })
    delete folders.value[doc.id]
    await refresh()
  } catch (err) {
    error.value = (err as Error).message
  }
}

async function removeDoc(doc: InboxDocument) {
  if (!confirm('Видалити документ?')) return
  await api.inbox.remove(doc.id)
  await refresh()
}

// Which scan row currently has an action running, so its buttons can disable.
const busyScan = ref<string | null>(null)
// --- Group actions over the journal -----------------------------------------
const selected = ref<Set<string>>(new Set())
const bulkBusy = ref('')
const bulkNote = ref('')
const busyLabel = ref('')
const toast = ref<InstanceType<typeof ToastMessage> | null>(null)
// Outcome of the last action on a given row. A toast announces it, and this
// keeps it readable next to the row once the toast has gone.
const rowNote = ref<Record<string, { text: string; failed: boolean }>>({})
// The rarely-used actions of one row, revealed on demand so the common path
// (accept the draft / open the receipt) is not buried among five buttons.
const openMenu = ref<string | null>(null)

async function runScanAction(
  jobId: string,
  action: () => Promise<unknown>,
  label = 'Обробка…',
) {
  error.value = ''
  busyScan.value = jobId
  busyLabel.value = label
  delete rowNote.value[jobId]
  try {
    await action()
    await refresh()
  } catch (err) {
    const text = (err as Error).message
    error.value = text
    rowNote.value[jobId] = { text, failed: true }
  } finally {
    busyScan.value = null
    busyLabel.value = ''
  }
}

function noteRow(jobId: string, text: string, failed = false) {
  rowNote.value[jobId] = { text, failed }
  toast.value?.show(text, failed ? 'error' : 'success')
}

function removeScan(job: ScanJob) {
  // Clearing a bad scan out of the journal is pointless while the wrong receipt
  // it created stays in the reports, so the two go together.
  const withReceipt = !!job.receipt_id
  const question = withReceipt
    ? 'Видалити збережений чек разом зі скануванням і зображенням? Цю дію не можна скасувати.'
    : 'Видалити сканування разом із завантаженим зображенням?'
  if (!confirm(question)) return
  return runScanAction(job.id, () => api.inbox.removeScan(job.id, withReceipt), 'Видаляю…')
}

function archiveScan(job: ScanJob) {
  return runScanAction(job.id, () => api.inbox.archiveScan(job.id, !job.archived_at))
}

function fiscalUrl(job: ScanJob): string {
  return typeof job.raw_ocr?.fiscal_url === 'string' ? job.raw_ocr.fiscal_url : ''
}

// A decoded QR gives the ДПС document id; fetching the document is a separate
// step. This goes through the token-authenticated public API only — never the
// browser flow, which is the one that runs into reCAPTCHA. When the API still
// refuses, the XML saved from the ДПС page in a browser is the way through.
async function requestFromDps(job: ScanJob) {
  const url = fiscalUrl(job)
  if (!url) return
  await runScanAction(job.id, async () => {
    const result = await api.fiscalQueue.resolve(url, job.id)
    if (result?.receipt_id) noteRow(job.id, result.duplicate
      ? 'Це дубль — наявний чек доповнено даними з ДПС.'
      : 'Чек отримано з API ДПС і збережено.')
    else noteRow(job.id,
      `API ДПС не віддав чек: ${result?.error || 'причина невідома'}. `
      + 'Відкрийте посилання ДПС у браузері, збережіть XML-відповідь і завантажте її тут.',
      true)
  }, 'Запит до ДПС…')
}

async function uploadDpsXml(job: ScanJob, event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  await runScanAction(job.id, async () => {
    // Pass the row's id so the import lands on this scan rather than beside it.
    const result = await api.fiscalQueue.importResponseFile(file, job.id)
    if (result?.receipt_id) noteRow(job.id, result.duplicate
      ? `Це дубль — наявний чек доповнено даними з ${file.name}.`
      : `Чек із ${file.name} збережено.`)
    else noteRow(job.id, `${file.name} прийнято, але чек не створено.`, true)
  }, 'Читаю XML…')
  // Clear it so picking the same file again still fires a change event.
  input.value = ''
}

function rescan(job: ScanJob, ai: boolean) {
  // Rerunning a scan that already produced a receipt rewrites saved data, so it
  // is never done on a single click.
  const replace = !!job.receipt_id
  if (replace && !confirm(
    'Цей чек уже збережено. Пересканувати фото і замінити збережений чек новим результатом?',
  )) return

  return runScanAction(job.id, async () => {
    const r = await api.inbox.rescan(job.id, ai, replace)
    if (r.replaced_receipt_id) noteRow(job.id, '♻️ Попередній чек замінено новим результатом.')
    else if (r.kept_receipt_id) noteRow(job.id,
      'Пересканування не дало кращого результату — збережений чек залишено без змін.', true)
    else noteRow(job.id, resultText(r), !r.receipt_id)
  }, ai ? 'Розпізнаю через AI…' : 'Пересканую…')
}

interface DraftItem {
  name?: string
  quantity?: number
  unit_price?: number | null
  total_price?: number
  discount?: number
}
interface Draft {
  shop_name?: string | null
  purchased_at?: string | null
  total_amount?: number | null
  total_discount?: number | null
  items?: DraftItem[]
}

function draftOf(job: ScanJob): Draft | null {
  const draft = job.raw_ocr?.draft
  return draft && typeof draft === 'object' ? (draft as Draft) : null
}

/** Does the draft's own arithmetic add up? The mismatch is what to look at. */
function draftBalance(draft: Draft): { sum: number; delta: number } | null {
  const items = draft.items ?? []
  if (!items.length || draft.total_amount == null) return null
  const sum = items.reduce((acc, i) => acc + (i.total_price ?? 0), 0)
  return { sum, delta: +(sum - draft.total_amount).toFixed(2) }
}

// Accepting the draft creates the receipt; fixing it is then the receipt page's
// job, which already edits shop, date, totals and lines.
function saveDraft(job: ScanJob) {
  return runScanAction(job.id, async () => {
    const saved = await api.inbox.saveDraft(job.id)
    noteRow(job.id, saved.duplicate_of
      ? 'Це дубль — наявний чек доповнено даними чернетки.'
      : 'Чек створено з чернетки — відкрийте його, щоб виправити деталі.')
  }, 'Зберігаю чек…')
}

/** The scan that first filed this receipt, if it is in the current list. */
function originalScan(job: ScanJob): ScanJob | undefined {
  return scans.value.find((s) => s.id === job.duplicate_of)
}

// Set briefly after following a duplicate link. The target is often already on
// screen, where scrolling alone changes nothing and the link reads as dead.
// A scan whose stored image is gone (deleted from disk) would otherwise render
// as a broken-image icon; those rows fall back to the plain filename.
const brokenThumbs = ref<Record<string, boolean>>({})

const highlightedScan = ref<string | null>(null)
let highlightTimer: number | null = null

async function goToScan(jobId: string) {
  // A filter that hides the target would make the link do nothing visible, so
  // clear the filters before scrolling to it.
  if (!visibleScans.value.some((s) => s.id === jobId)) {
    scanFilter.value = 'all'
    scanQuery.value = ''
    await nextTick()
  }
  // Instant, not smooth: a smooth scroll silently does nothing whenever the page
  // isn't animating frames, and the flash below is what actually points the row
  // out anyway.
  document.getElementById(`scan-${jobId}`)?.scrollIntoView({ block: 'center' })

  if (highlightTimer !== null) window.clearTimeout(highlightTimer)
  // Clicking again while the previous flash is still on would leave the class
  // untouched, and a CSS animation only restarts when it is re-applied — so
  // take it off and let the DOM update before putting it back.
  highlightedScan.value = null
  await nextTick()
  highlightedScan.value = jobId
  highlightTimer = window.setTimeout(() => { highlightedScan.value = null }, 2200)
}

function qrText(job: ScanJob): string {
  if (job.raw_ocr?.fiscal_url) return 'QR ✅ фіскальний'
  switch (job.raw_ocr?.qr_state) {
    case 'decoded': return 'QR 🔶 не фіскальний'
    case 'unreadable': return 'QR 🔶 видно, але не читається — зніміть QR зблизька'
    case 'none': return 'QR ❌ не знайдено'
    // Scans made before the state was recorded say nothing rather than guess.
    default: return 'QR —'
  }
}

function isSelected(id: string) {
  return selected.value.has(id)
}

function toggleSelected(id: string) {
  const next = new Set(selected.value)
  next.has(id) ? next.delete(id) : next.add(id)
  selected.value = next
}

const allVisibleSelected = computed(
  () => visibleScans.value.length > 0 && visibleScans.value.every((j) => selected.value.has(j.id)),
)

function toggleAllVisible() {
  // Acts on what is on screen: with a filter applied, "select all" must not
  // quietly pick up rows the user cannot see.
  const next = new Set(selected.value)
  if (allVisibleSelected.value) visibleScans.value.forEach((j) => next.delete(j.id))
  else visibleScans.value.forEach((j) => next.add(j.id))
  selected.value = next
}

function clearSelection() {
  selected.value = new Set()
  bulkNote.value = ''
}

const selectedJobs = computed(() => scans.value.filter((j) => selected.value.has(j.id)))
const selectedWithReceipt = computed(() => selectedJobs.value.filter((j) => !!j.receipt_id).length)

async function runBulk(
  action: 'archive' | 'unarchive' | 'delete' | 'rescan' | 'rescan_ai',
  label: string,
) {
  const ids = [...selected.value]
  if (!ids.length) return
  let withReceipt = false
  if (action === 'delete') {
    const saved = selectedWithReceipt.value
    const question = saved
      ? `Видалити ${ids.length} сканувань? Серед них ${saved} зі збереженим чеком — ` +
        'видалити їх разом із чеками?'
      : `Видалити ${ids.length} сканувань?`
    if (!confirm(question)) return
    // Only after an explicit yes does a delete take saved receipts with it.
    withReceipt = saved > 0
  }
  bulkBusy.value = action
  bulkNote.value = ''
  try {
    const out = await api.inbox.bulkScans(ids, action, withReceipt)
    const failed = out.results.filter((r) => !r.ok)
    bulkNote.value = failed.length
      ? `${label}: ${out.done} з ${ids.length}. Не вдалося: ` +
        failed.map((r) => r.detail).join('; ')
      : `${label}: ${out.done} з ${ids.length}.`
    selected.value = new Set(failed.map((r) => r.id))
    ;[scans.value, stats.value] = await Promise.all([
      api.inbox.scans(showArchived.value),
      api.inbox.scanStats(),
    ])
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    bulkBusy.value = ''
  }
}

function resultText(r: UploadResult): string {
  if (r.kind === 'document') return `Особистий документ (співвідношення ${r.ratio}) — у черзі інбоксу.`
  if (r.confidence === 'high') return `Чек розпізнано (${r.method}) і збережено.`
  if (r.needs_review) return `Чек розпізнано частково (${r.method}) — потрібна ручна перевірка.`
  return `Не вдалося розпізнати чек (метод: ${r.method}).`
}

// --- Scan journal: status, filters, next step --------------------------------

function statusInfo(j: ScanJob): { cls: string; text: string } {
  if (j.receipt_id) return { cls: 'ok', text: 'Збережено ✓' }
  if (j.status === 'needs_review') return { cls: 'warn', text: 'Потребує перевірки' }
  if (j.status === 'failed') return { cls: 'bad', text: 'Не розпізнано' }
  if (j.status === 'pending') return { cls: 'neutral', text: 'В обробці…' }
  return { cls: 'neutral', text: j.status }
}

/** A scan still waiting for a decision — nothing saved and not put aside. */
function needsAttention(j: ScanJob): boolean {
  return !j.receipt_id && !j.archived_at && !j.duplicate_of
    && (j.status === 'needs_review' || j.status === 'failed')
}

/** One sentence telling the reader what this row is waiting for. */
function nextStep(j: ScanJob): string {
  if (j.receipt_id || j.archived_at) return ''
  if (j.duplicate_of) return ''
  if (draftOf(j)) return 'Перевірте чернетку → «Прийняти чернетку».'
  if (fiscalUrl(j)) return 'Є фіскальний QR — спробуйте «Запросити з API ДПС».'
  if (j.status === 'failed') return 'Перезніміть фото рівніше або спробуйте «Через AI».'
  return ''
}

const scanFilter = ref<'all' | 'attention' | 'saved' | 'failed'>('all')
const scanQuery = ref('')

const attentionCount = computed(() => scans.value.filter(needsAttention).length)
const savedCount = computed(() => scans.value.filter((j) => !!j.receipt_id).length)
const pendingDocs = computed(() => docs.value.filter((d) => d.status !== 'done').length)
const pendingVoices = computed(() => voices.value.filter((v) => !v.applied).length)

const visibleScans = computed(() => {
  const q = scanQuery.value.trim().toLowerCase()
  return scans.value.filter((j) => {
    if (scanFilter.value === 'attention' && !needsAttention(j)) return false
    if (scanFilter.value === 'saved' && !j.receipt_id) return false
    if (scanFilter.value === 'failed' && !(j.status === 'failed' && !j.receipt_id)) return false
    if (!q) return true
    return [j.shop_name, j.source_filename, j.method, j.total_amount?.toFixed(2)]
      .some((v) => (v || '').toString().toLowerCase().includes(q))
  })
})

function scrollTo(id: string) {
  document.getElementById(id)?.scrollIntoView({ block: 'start' })
}

function showAttention() {
  scanFilter.value = 'attention'
  scanQuery.value = ''
  scrollTo('scan-journal')
}
</script>

<template>
  <ToastMessage ref="toast" :error="error" />
  <h2>Інбокс</h2>
  <p class="muted">
    Завантажте фото — вузьке піде в конвеєр розпізнавання чека, широке потрапить у чергу
    особистих документів (вона ніколи не йде в зовнішню модель).
  </p>

  <!-- What is waiting for a decision, before anything else on the page. -->
  <div v-if="!loading" class="attention-bar">
    <button
      v-if="attentionCount"
      class="chip chip-warn"
      title="Сканування без збереженого чека"
      @click="showAttention"
    >
      🔍 Потребують перевірки: {{ attentionCount }}
    </button>
    <button
      v-if="pendingDocs"
      class="chip"
      title="Документи в черзі — дайте імʼя й оберіть папку"
      @click="scrollTo('doc-queue')"
    >
      📄 Документів у черзі: {{ pendingDocs }}
    </button>
    <button
      v-if="pendingVoices"
      class="chip"
      title="Голосові повідомлення без застосованого результату"
      @click="scrollTo('voice-queue')"
    >
      🎙 Голосових: {{ pendingVoices }}
    </button>
    <span v-if="!attentionCount && !pendingDocs && !pendingVoices" class="chip chip-ok">
      ✅ Черга порожня — усе розібрано
    </span>
    <span class="attention-drive muted">
      Google Drive:
      <template v-if="drive?.connected">під'єднано ✅ — документи йдуть у «Необроблені».</template>
      <template v-else>не під'єднано, документи лежать локально
        (<RouterLink to="/settings">під'єднати</RouterLink>).</template>
    </span>
  </div>

  <!-- Upload ---------------------------------------------------------------->
  <div
    class="card dropzone"
    :class="{ over: dragOver, busy: uploading }"
    @dragover.prevent="dragOver = true"
    @dragenter.prevent="dragOver = true"
    @dragleave="dragOver = false"
    @drop.prevent="onDrop"
  >
    <p class="dz-title">📥 Перетягніть фото сюди</p>
    <div class="dz-buttons">
      <label class="btn file-button" :class="{ disabled: uploading }">
        <span>Обрати файли</span>
        <input type="file" accept="image/*" multiple :disabled="uploading" @change="onFile" />
      </label>
      <label class="btn secondary file-button" :class="{ disabled: uploading }">
        <span>📷 Зняти камерою</span>
        <input
          type="file"
          accept="image/*"
          capture="environment"
          :disabled="uploading"
          @change="onFile"
        />
      </label>
    </div>
    <label class="dz-hint" style="display: flex; gap: 8px; align-items: center; justify-content: center">
      <input v-model="seriesMode" type="checkbox" style="width: auto" :disabled="uploading" />
      Кілька фото — це один довгий чек (склеїти за спільними рядками)
    </label>
    <p class="muted dz-hint">
      Можна кинути одразу кілька фото або вставити зображення з буфера — <kbd>Ctrl</kbd>+<kbd>V</kbd>.
      Для довгого чека знімайте кадри з перекриттям: кілька нижніх рядків одного кадру
      мають повторюватися вгорі наступного.
    </p>

    <ul v-if="uploads.length" class="upload-list">
      <li v-for="u in uploads" :key="u.id" :class="`up-${u.state}`">
        <span class="up-icon">{{
          u.state === 'busy' ? '⏳' : u.state === 'ok' ? '✅' : u.state === 'warn' ? '🔍' : '⚠️'
        }}</span>
        <span class="up-name">{{ u.name }}</span>
        <span class="up-text muted">{{ u.text }}</span>
        <RouterLink v-if="u.receiptId" :to="`/receipts/${u.receiptId}`">Відкрити чек →</RouterLink>
      </li>
    </ul>
  </div>

  <!-- Voice ----------------------------------------------------------------->
  <div v-if="voices.length" id="voice-queue" class="card section">
    <h3 class="section-title">🎙 Голосові повідомлення ({{ voices.length }})</h3>
    <p class="muted">
      Запис зберігається завжди — навіть коли розпізнавання вимкнене. Його можна
      прослухати й розпізнати пізніше
      (<RouterLink to="/settings">модель STT у Налаштуваннях</RouterLink>).
    </p>
    <div v-for="v in voices" :key="v.id" class="card" style="margin-bottom: 12px">
      <div class="toolbar">
        <span class="muted" style="flex: 1">
          {{ fmtDateTime(v.created_at) }} · {{ v.source }}
          <template v-if="voiceLength(v)"> · {{ voiceLength(v) }}</template>
        </span>
        <button
          v-if="!v.transcript || v.status === 'failed'"
          :disabled="voiceBusy === v.id || !v.stt_ready"
          @click="recognizeVoice(v)"
        >
          {{ voiceBusy === v.id ? 'Розпізнавання…' : 'Розпізнати' }}
        </button>
        <template v-else>
          <button class="secondary" :disabled="voiceBusy === v.id" @click="applyVoice(v)">
            Розібрати ще раз
          </button>
          <button class="secondary" :disabled="voiceBusy === v.id || !v.stt_ready" @click="recognizeVoice(v)">
            Розпізнати заново
          </button>
        </template>
        <button class="secondary" :disabled="voiceBusy === v.id" @click="removeVoice(v)">Видалити</button>
      </div>
      <audio :src="api.voice.audioUrl(v.id)" controls preload="none" style="width: 100%; margin-top: 8px" />
      <p v-if="v.transcript" style="margin: 8px 0 0">
        🗣 {{ v.transcript }}
        <span v-if="v.language" class="muted">({{ languageLabel(v) }})</span>
      </p>
      <!-- A recogniser locked to one language turns a sentence said in the other
           into nonsense; the fix is to run the same recording again, not to
           re-dictate it. -->
      <div v-if="v.stt_ready && otherLanguages(v).length" class="toolbar" style="margin-top: 6px">
        <span class="muted" style="font-size: 12px">Розпізнати іншою мовою:</span>
        <button
          v-for="lang in otherLanguages(v)"
          :key="lang.code"
          class="secondary"
          :disabled="voiceBusy === v.id"
          @click="recognizeVoice(v, lang.code)"
        >
          🌐 {{ lang.label }}
        </button>
      </div>
      <p v-if="v.applied" class="muted" style="margin: 4px 0 0">→ {{ v.applied }}</p>
      <p v-if="v.error" class="error" style="margin: 4px 0 0">{{ v.error }}</p>
      <p v-else-if="!v.stt_ready && !v.transcript" class="muted" style="margin: 4px 0 0">
        Розпізнавання не налаштоване — запис збережено, спробуйте після налаштування моделі STT.
      </p>
    </div>
  </div>

  <!-- Documents ------------------------------------------------------------->
  <div id="doc-queue" class="card section">
    <div class="section-head">
      <h3 class="section-title">
        📄 {{ showDoneDocs ? 'Документи' : 'Документи в черзі' }} ({{ docs.length }})
      </h3>
      <label class="switch">
        <input v-model="showDoneDocs" type="checkbox" />
        Показати оброблені
      </label>
    </div>
    <p v-if="!loading && !docs.length" class="empty">
      Черга порожня. Широкі фото — паспорти, договори, гарантійні талони — потраплять сюди.
    </p>
    <div v-for="doc in docs" :key="doc.id" class="card doc-card">
      <div class="doc-head">
        <a v-if="doc.file_url" :href="doc.file_url" target="_blank">{{ doc.original_filename || doc.id }}</a>
        <span v-else>{{ doc.original_filename || doc.id }}</span>
        <span class="muted doc-meta">
          {{ doc.width }}×{{ doc.height }} · {{ doc.storage }}
          <template v-if="doc.status === 'done'">
            · оброблено {{ doc.processed_at ? fmtDateTime(doc.processed_at) : '' }}
          </template>
        </span>
        <button class="danger" @click="removeDoc(doc)">Видалити</button>
      </div>
      <p v-if="doc.status === 'done'" class="muted" style="margin: 0">
        Збережено як <strong>{{ doc.final_name || doc.original_filename }}</strong>.
      </p>
      <template v-else>
        <div class="form-row">
          <label>Імʼя файлу</label>
          <input
            v-model="names[doc.id]"
            placeholder="Договір оренди 2026"
            @keyup.enter="processDoc(doc)"
          />
        </div>

        <div v-if="doc.storage === 'drive'" class="form-row">
          <label>Папка Drive</label>
          <span>
            <span
              v-if="folders[doc.id]"
              class="folder-dot"
              :style="{ background: folders[doc.id]?.color || '#c2c2c2' }"
            />
            {{ folders[doc.id]?.name || 'Необроблені (без переміщення)' }}
            <button class="secondary" style="margin-left: 8px" @click="openPicker(doc.id)">
              {{ pickerFor === doc.id ? 'Сховати' : 'Обрати папку' }}
            </button>
          </span>
        </div>
        <p v-else class="muted" style="margin: 0 0 8px">
          Зберігається локально в <code>data/inbox/</code> — Drive не під'єднано.
          Після обробки файл отримає задане ім'я в тій самій папці.
        </p>

        <div v-if="pickerFor === doc.id" class="card" style="margin-bottom: 8px">
          <div class="muted" style="margin-bottom: 6px">
            <span v-for="(f, i) in pickerStack" :key="i">
              <a href="#" @click.prevent="browse(f, i)">{{ f.name }}</a>
              <span v-if="i < pickerStack.length - 1"> / </span>
            </span>
          </div>
          <ul style="list-style: none; padding: 0; margin: 0">
            <li v-for="f in pickerFolders" :key="f.id" style="display: flex; gap: 8px; align-items: center; padding: 3px 0">
              <span class="folder-dot" :style="{ background: f.color || '#c2c2c2' }" />
              <a href="#" @click.prevent="browse({ id: f.id, name: f.name }, pickerStack.length - 1)">{{ f.name }}</a>
              <button class="secondary" style="margin-left: auto" @click="chooseFolder(f)">Сюди</button>
            </li>
            <li v-if="!pickerFolders.length" class="muted">Порожньо</li>
          </ul>
        </div>

        <button @click="processDoc(doc)">Обробити</button>
      </template>
    </div>
  </div>

  <!-- Scan journal ---------------------------------------------------------->
  <div id="scan-journal" class="card section">
    <div class="section-head">
      <h3 class="section-title">🧾 Журнал сканувань</h3>
      <label class="switch">
        <input v-model="showArchived" type="checkbox" />
        Показати архівовані
      </label>
    </div>

    <div class="filters">
      <div class="tabs">
        <button
          v-for="t in ([
            { key: 'all', label: `Усі (${scans.length})` },
            { key: 'attention', label: `Потребують перевірки (${attentionCount})` },
            { key: 'saved', label: `Збережені (${savedCount})` },
            { key: 'failed', label: 'Не розпізнані' },
          ] as const)"
          :key="t.key"
          class="tab"
          :class="{ active: scanFilter === t.key }"
          @click="scanFilter = t.key"
        >
          {{ t.label }}
        </button>
      </div>
      <input v-model="scanQuery" class="search" type="search" placeholder="Пошук: магазин, файл, сума…" />
    </div>

    <details class="hint">
      <summary>Що означає «Потребує перевірки» і що з цим робити</summary>
      <p class="muted" style="margin: 8px 0 0">
        Розпізнане не збіглося саме з собою, тому чек не збережено. Розгорніть чернетку
        в колонці «Результат»: якщо позиції та сума правильні —
        <strong>Прийняти чернетку</strong>, і чек можна доредагувати на його сторінці.
        Якщо ні — <strong>Пересканувати</strong>, <strong>Через AI</strong> або перезніміть фото.
      </p>
      <p v-if="stats" class="muted" style="margin: 8px 0 0">
        Усього сканувань: {{ stats.total }} · через AI: {{ (stats.ai_share * 100).toFixed(0) }}% ·
        за методом: <span v-for="(n, m) in stats.by_method" :key="m">{{ m }}={{ n }} </span>
      </p>
    </details>

    <p v-if="loading" class="empty">Завантаження…</p>
    <p v-else-if="!scans.length" class="empty">
      {{ showArchived ? 'Журнал порожній.' : 'Активних сканувань немає — усі в архіві.' }}
    </p>
    <p v-else-if="!visibleScans.length" class="empty">
      Нічого не знайдено за цим фільтром.
      <button class="secondary" @click="scanFilter = 'all'; scanQuery = ''">Скинути фільтри</button>
    </p>

    <div v-if="visibleScans.length && selected.size" class="bulk-bar">
      <strong>Вибрано: {{ selected.size }}</strong>
      <button class="secondary" :disabled="!!bulkBusy" @click="runBulk('rescan_ai', 'Розпізнано через AI')">
        {{ bulkBusy === 'rescan_ai' ? 'Розпізнавання…' : '🤖 Розпізнати через AI' }}
      </button>
      <button class="secondary" :disabled="!!bulkBusy" @click="runBulk('rescan', 'Пересканованo')">
        {{ bulkBusy === 'rescan' ? 'Сканування…' : '🔄 Пересканувати' }}
      </button>
      <button class="secondary" :disabled="!!bulkBusy" @click="runBulk('archive', 'В архіві')">
        📥 В архів
      </button>
      <button class="secondary" :disabled="!!bulkBusy" @click="runBulk('unarchive', 'Повернуто з архіву')">
        📤 З архіву
      </button>
      <button class="danger" :disabled="!!bulkBusy" @click="runBulk('delete', 'Видалено')">
        🗑 Видалити
      </button>
      <button class="secondary" :disabled="!!bulkBusy" @click="clearSelection">Зняти вибір</button>
      <span v-if="bulkNote" class="muted bulk-note">{{ bulkNote }}</span>
    </div>

    <table v-if="visibleScans.length" class="scan-table">
      <thead>
        <tr>
          <th class="pick">
            <input
              type="checkbox"
              :checked="allVisibleSelected"
              :disabled="!!bulkBusy"
              title="Вибрати всі показані"
              @change="toggleAllVisible"
            />
          </th>
          <th>Завантажено</th><th>Вхідні дані</th><th>Магазин</th>
          <th>Дата чека</th><th class="num">Сума</th>
          <th>Результат</th><th>Метод</th><th>Статус</th><th>Дії</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="j in visibleScans"
          :id="`scan-${j.id}`"
          :key="j.id"
          :class="{
            archived: j.archived_at,
            duplicate: j.duplicate_of,
            highlighted: highlightedScan === j.id,
            attention: needsAttention(j),
          }"
        >
          <td class="pick" data-label="">
            <input
              type="checkbox"
              :checked="isSelected(j.id)"
              :disabled="!!bulkBusy"
              @change="toggleSelected(j.id)"
            />
          </td>
          <td data-label="Завантажено">{{ fmtDateTime(j.created_at) }}</td>
          <td data-label="Вхідні дані">
            <div v-if="(j.part_urls?.length || 0) > 1" class="scan-frames">
              <ZoomableImage
                v-for="(url, idx) in j.part_urls"
                :key="url"
                :src="BASE + url"
                :alt="`Кадр ${idx + 1}`"
                :preview-size="280"
                class="scan-thumb"
              />
              <span class="muted">{{ j.part_urls?.length }} фото однієї серії</span>
            </div>
            <div v-else-if="j.input_url && !brokenThumbs[j.id]" class="scan-photo">
              <ZoomableImage
                :src="BASE + j.input_url"
                :alt="j.source_filename || 'Фото чека'"
                :preview-size="280"
                class="scan-thumb"
                @error="brokenThumbs[j.id] = true"
              />
              <span>{{ j.source_filename || 'Фото чека' }}</span>
            </div>
            <span v-else>{{ j.source_filename || '—' }}</span>
            <br /><small class="muted">{{ qrText(j) }}</small>
            <template v-if="fiscalUrl(j)">
              <br /><small><a :href="fiscalUrl(j)" target="_blank">Посилання ДПС</a></small>
              <div class="dps-actions">
                <button class="secondary" :disabled="busyScan === j.id"
                        title="Через відкритий API ДПС за токеном — без браузера й CAPTCHA"
                        @click="requestFromDps(j)">
                  Запросити з API ДПС
                </button>
                <label class="btn secondary file-button" :class="{ disabled: busyScan === j.id }"
                       title="XML з кабінету ДПС: файл чека (CHECK), фіскальний документ (RQ) або відповідь API (Chk)">
                  <span>📄 Завантажити XML</span>
                  <input type="file" accept=".xml,.txt" :disabled="busyScan === j.id"
                         @change="uploadDpsXml(j, $event)" />
                </label>
              </div>
            </template>
          </td>
          <td data-label="Магазин">{{ j.shop_name || '—' }}</td>
          <td data-label="Дата чека">{{ j.purchased_at ? fmtDateTime(j.purchased_at) : '—' }}</td>
          <td data-label="Сума" class="num">{{ j.total_amount == null ? '—' : `${j.total_amount.toFixed(2)} ${j.currency || 'UAH'}` }}</td>
          <td data-label="Результат">
            <RouterLink v-if="j.receipt_id" :to="`/receipts/${j.receipt_id}`">Відкрити чек →</RouterLink>
            <details v-else-if="draftOf(j)" class="draft">
              <summary>Переглянути чернетку ({{ draftOf(j)?.items?.length || 0 }} поз.)</summary>
              <table class="draft-items">
                <tr v-for="(it, idx) in draftOf(j)?.items || []" :key="idx">
                  <td>{{ it.name || '—' }}</td>
                  <td class="num">{{ it.quantity ?? 1 }}{{ it.unit_price ? ` × ${it.unit_price}` : '' }}</td>
                  <td class="num">{{ (it.total_price ?? 0).toFixed(2) }}</td>
                </tr>
                <tr v-if="!draftOf(j)?.items?.length">
                  <td colspan="3" class="muted">Позицій не розпізнано.</td>
                </tr>
              </table>
              <p v-if="draftBalance(draftOf(j)!)?.delta" class="draft-warn">
                Позиції дають {{ draftBalance(draftOf(j)!)?.sum.toFixed(2) }},
                а разом — {{ draftOf(j)?.total_amount }}.
                Розбіжність {{ draftBalance(draftOf(j)!)?.delta.toFixed(2) }}.
              </p>
              <p v-else class="muted" style="margin: 4px 0 0">Позиції сходяться із сумою.</p>
            </details>
            <span v-else>—</span>
          </td>
          <td data-label="Метод">{{ j.method || '—' }}</td>
          <td data-label="Статус">
            <RouterLink v-if="j.receipt_id" :to="`/receipts/${j.receipt_id}`" class="status-link">
              <span class="badge" :class="`badge-scan-${statusInfo(j).cls}`">{{ statusInfo(j).text }}</span>
            </RouterLink>
            <span v-else class="badge" :class="`badge-scan-${statusInfo(j).cls}`">{{ statusInfo(j).text }}</span>
            <small v-if="j.duplicate_of" class="dup-badge">
              Дубль. Дані обʼєднано в чек, який завела
              <!-- Labelled by upload time, not by row number: the position
                   shifts as soon as the list is filtered or refreshed. -->
              <a v-if="originalScan(j)" href="#" @click.prevent="goToScan(j.duplicate_of!)">
                проба від {{ fmtDateTime(originalScan(j)!.created_at) }}
              </a>
              <span v-else>інша проба (не в цьому списку)</span>.
            </small>
            <br v-if="j.receipt_deleted || j.archived_at" />
            <small v-if="j.receipt_deleted" class="muted">чек видалено вручну</small>
            <small v-else-if="j.archived_at" class="muted">в архіві</small>
          </td>
          <td data-label="Дії" class="scan-actions">
            <template v-if="j.needs_choice">
              <button :disabled="busyScan === j.id" title="Склеїти кадри в один довгий чек"
                      @click="resolveSeries(j, 'one')">
                🧩 Один чек
              </button>
              <button class="secondary" :disabled="busyScan === j.id"
                      title="Обробити кожне фото як окремий чек"
                      @click="resolveSeries(j, 'separate')">
                🧾 Різні чеки
              </button>
            </template>
            <button
              v-if="!j.receipt_id && draftOf(j) && !j.needs_choice"
              :disabled="busyScan === j.id"
              title="Створити чек із чернетки — далі його можна відредагувати на сторінці чека"
              @click="saveDraft(j)"
            >
              Прийняти чернетку
            </button>
            <label
              v-if="!j.receipt_id && draftOf(j)"
              class="btn secondary file-button"
              :class="{ disabled: busyScan === j.id || uploading }"
              title="Чек довший за кадр? Додайте наступне фото — воно склеїться з цією чернеткою"
            >
              <span>➕ Ще фото чека</span>
              <input type="file" accept="image/*" multiple :disabled="busyScan === j.id || uploading"
                     @change="addSeriesPhoto(j, $event)" />
            </label>
            <button
              class="secondary"
              :disabled="busyScan === j.id"
              :aria-expanded="openMenu === j.id"
              @click="openMenu = openMenu === j.id ? null : j.id"
            >
              {{ openMenu === j.id ? 'Менше ▴' : 'Ще ▾' }}
            </button>
            <div v-if="openMenu === j.id" class="row-menu">
              <button class="secondary" :disabled="busyScan === j.id" @click="rescan(j, false)">
                Пересканувати
              </button>
              <button class="secondary" :disabled="busyScan === j.id" @click="rescan(j, true)">
                Через AI
              </button>
              <button class="secondary" :disabled="busyScan === j.id" @click="archiveScan(j)">
                {{ j.archived_at ? 'З архіву' : 'В архів' }}
              </button>
              <button class="danger" :disabled="busyScan === j.id" @click="removeScan(j)">
                {{ j.receipt_id ? 'Видалити чек' : 'Видалити' }}
              </button>
            </div>
            <small v-if="busyScan === j.id" class="row-note muted">⏳ {{ busyLabel }}</small>
            <small
              v-else-if="rowNote[j.id]"
              class="row-note"
              :class="{ failed: rowNote[j.id]?.failed, muted: !rowNote[j.id]?.failed }"
            >{{ rowNote[j.id]?.text }}</small>
            <small v-else-if="nextStep(j)" class="row-note muted">💡 {{ nextStep(j) }}</small>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
/* A batch of photos is one row: its frames sit side by side inside it. */
.scan-frames {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  max-width: 240px;
}
/* `:deep` for the same reason as .scan-photo below — the <img> lives in
   ZoomableImage, which has several root nodes, so a plain scoped class never
   reaches it and every frame renders at its natural size. */
.scan-frames :deep(.scan-thumb) {
  width: 44px;
  height: 44px;
  object-fit: cover;
  border-radius: 6px;
  border: 1px solid var(--border);
  flex: none;
}
.scan-frames .muted {
  font-size: 11px;
  flex-basis: 100%;
}
/* Group actions over the journal */
.bulk-bar {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  position: sticky;
  top: 0;
  z-index: 2;
  margin-bottom: 10px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--card, var(--bg));
}
.bulk-note {
  font-size: 12px;
  flex-basis: 100%;
}
.scan-table th.pick,
.scan-table td.pick {
  width: 28px;
  text-align: center;
}
.scan-table td.pick input,
.scan-table th.pick input {
  width: auto;
}
.section {
  margin-top: 24px;
}
.section-title {
  margin: 0;
}
.section-head {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.section-head .switch {
  margin-left: auto;
}
.switch {
  display: inline-flex;
  gap: 6px;
  align-items: center;
  font-size: 13px;
  color: var(--text-dim);
  cursor: pointer;
}
.switch input {
  width: auto;
  margin: 0;
}
.empty {
  color: var(--text-dim);
  margin: 8px 0 0;
}

/* Attention bar: the queue's outstanding work, as clickable jumps. */
.attention-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 18px;
}
.chip {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: 999px;
  padding: 6px 14px;
  font-size: 13px;
}
.chip-warn {
  border-color: var(--warning);
  color: var(--warning);
}
.chip-ok {
  border-color: rgba(74, 222, 128, 0.4);
  color: var(--success);
  cursor: default;
}
.attention-drive {
  font-size: 13px;
  margin-left: auto;
}

/* Upload ------------------------------------------------------------------ */
.dropzone {
  border-style: dashed;
  text-align: center;
  transition: border-color 0.15s, background 0.15s;
}
.dropzone.over {
  border-color: var(--accent);
  background: var(--bg-hover);
}
.dropzone.busy {
  opacity: 0.75;
}
.dz-title {
  margin: 0 0 10px;
  font-size: 16px;
  font-weight: 600;
}
.dz-buttons {
  display: flex;
  gap: 8px;
  justify-content: center;
  flex-wrap: wrap;
}
.dz-hint {
  margin: 10px 0 0;
  font-size: 12px;
}
.dz-hint kbd {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 1px 5px;
  font-size: 11px;
}
.upload-list {
  list-style: none;
  padding: 0;
  margin: 14px 0 0;
  text-align: left;
}
.upload-list li {
  display: flex;
  gap: 8px;
  align-items: baseline;
  flex-wrap: wrap;
  padding: 4px 0;
  border-top: 1px solid var(--border);
  font-size: 13px;
}
.up-name {
  font-weight: 500;
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.up-error .up-text {
  color: var(--danger);
}

/* Documents --------------------------------------------------------------- */
.doc-card {
  margin-bottom: 12px;
}
.doc-head {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.doc-meta {
  flex: 1;
  font-size: 12px;
}

/* Journal filters --------------------------------------------------------- */
.filters {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.tabs {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.tab {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--text-dim);
  padding: 6px 12px;
}
.tab.active {
  background: var(--accent-dim);
  border-color: var(--accent-dim);
  color: white;
}
.search {
  margin-left: auto;
  max-width: 260px;
}
.hint summary {
  cursor: pointer;
  color: var(--text-dim);
  font-size: 13px;
}
.hint {
  margin-bottom: 12px;
}

/* Journal rows ------------------------------------------------------------ */
.badge-scan-ok {
  background: rgba(74, 222, 128, 0.15);
  color: var(--success);
}
.badge-scan-warn {
  background: rgba(251, 191, 36, 0.15);
  color: var(--warning);
}
.badge-scan-bad {
  background: rgba(248, 113, 113, 0.15);
  color: var(--danger);
}
.badge-scan-neutral {
  background: rgba(148, 163, 184, 0.15);
  color: var(--text-dim);
}
.status-link {
  text-decoration: none;
}
.scan-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.scan-actions button {
  white-space: nowrap;
}
.row-menu {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  width: 100%;
}
.num {
  text-align: right;
  white-space: nowrap;
}
.archived {
  opacity: 0.55;
}
/* Rows still waiting for a decision are the reason to open this page. */
.attention > td:first-child {
  border-left: 3px solid var(--warning);
}
/* A copy of a purchase already filed — marked, but still fully actionable. */
.duplicate > td:first-child {
  border-left: 3px solid var(--accent-dim, #3b5bdb);
}
/* Following a duplicate link often lands on a row that was already on screen,
   so the target says so itself instead of relying on the scroll being noticed. */
.highlighted > td {
  animation: flash 2.2s ease-out;
}
@keyframes flash {
  0%, 55% { background: var(--accent-dim, #3b5bdb); }
  100% { background: transparent; }
}
.dup-badge {
  display: block;
  margin-top: 4px;
  max-width: 220px;
  white-space: normal;
  color: var(--muted, #94a3b8);
}
.scan-photo {
  display: flex;
  align-items: center;
  gap: 8px;
}
/* `:deep` because the <img> belongs to ZoomableImage's template, and that
   component has several root nodes — so a scoped class alone never reaches it,
   leaving the photo at its natural size. */
.scan-photo :deep(.scan-thumb) {
  width: 44px;
  height: 44px;
  object-fit: cover;
  border-radius: 6px;
  border: 1px solid var(--border);
  flex: none;
}
.dps-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  margin-top: 6px;
}
/* One line each, so the two controls end up the same height side by side. */
.dps-actions button {
  white-space: nowrap;
}
/* A native file input can't be styled, so the real control is hidden and the
   wrapping label wears the shared `.btn secondary` look instead. */
.file-button {
  display: inline-flex;
  align-items: center;
  white-space: nowrap;
}
.file-button:hover {
  filter: brightness(1.15);
}
.file-button.disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.file-button input {
  position: absolute;
  opacity: 0;
  pointer-events: none;
  width: 0;
  padding: 0;
  border: 0;
}
.draft summary {
  cursor: pointer;
}
.draft-items {
  margin: 6px 0 0;
  font-size: 12px;
}
.draft-items td {
  padding: 2px 8px 2px 0;
  border: 0;
}
.draft-warn {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--danger);
}
.row-note {
  display: block;
  margin-top: 6px;
  max-width: 260px;
  white-space: normal;
}
.row-note.failed {
  color: var(--danger, #e05260);
}
.folder-dot {
  display: inline-block;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  border: 1px solid rgba(0, 0, 0, 0.25);
  vertical-align: middle;
}

/* Narrow screens: nine columns never fit, so each scan becomes a card whose
   cells carry their own header from `data-label`. */
@media (max-width: 900px) {
  .scan-table > thead {
    display: none;
  }
  /* Direct children only — the draft table nested inside a cell stays a table. */
  .scan-table,
  .scan-table > tbody,
  .scan-table > tbody > tr,
  .scan-table > tbody > tr > td {
    display: block;
    width: 100%;
  }
  .scan-table > tbody > tr {
    border: 1px solid var(--border);
    border-radius: var(--radius);
    margin-bottom: 12px;
    padding: 6px 10px;
  }
  .scan-table > tbody > tr > td {
    border-bottom: none;
    padding: 4px 0;
    text-align: left;
  }
  .scan-table > tbody > tr > td[data-label]::before {
    content: attr(data-label);
    display: block;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    color: var(--text-dim);
  }
  /* The card's own border replaces the left marker used in the table. */
  .attention > td:first-child,
  .duplicate > td:first-child {
    border-left: none;
  }
  .attention {
    border-color: var(--warning);
  }
  .duplicate {
    border-color: var(--accent-dim);
  }
  .search {
    margin-left: 0;
    max-width: none;
  }
  .attention-drive {
    margin-left: 0;
  }
}
</style>
