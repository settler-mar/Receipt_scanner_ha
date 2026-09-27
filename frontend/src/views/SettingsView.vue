<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { api, BASE } from '@/lib/api'
import PasswordInput from '@/components/PasswordInput.vue'
import { fmtDateTime } from '@/lib/datetime'
import type {
  AiBackendCheck,
  AiCheckResult,
  AiConfig,
  AiLogEntry,
  AiModelChoices,
  AiProviderModels,
  AiProviderMeta,
  AiPrompt,
  AiPromptRevision,
  AiRouteTarget,
  AiSettings,
  AiTask,
  DriveFolder,
  DriveStatus,
  InboxConfig,
  McpConfig,
  McpToolInfo,
  SpeechLanguage,
  TaxCabinetSettings,
  TelegramSettings,
  TelegramMessageLog,
} from '@/types/api'

const loading = ref(true)
const saving = ref(false)
const error = ref('')
const savedAt = ref('')
const toast = reactive({ visible: false, message: '', kind: 'error' as 'error' | 'success' })
let toastTimer: number | null = null

function showToast(message: string, kind: 'error' | 'success' = 'error') {
  if (!message) return
  toast.message = message
  toast.kind = kind
  toast.visible = true
  if (toastTimer !== null) window.clearTimeout(toastTimer)
  toastTimer = window.setTimeout(() => { toast.visible = false }, kind === 'error' ? 8000 : 3500)
}

// The sidebar picks which settings page this is; only that group's sections
// render (see SECTIONS below).
const props = defineProps<{ group?: string }>()
const group = computed(() => props.group || 'ai-providers')

const config = reactive<AiConfig>({
  local: { enabled: false, url: 'http://localhost:11434', model: 'qwen2.5:3b-instruct', num_ctx: 8192 },
  providers: [],
  speech: {
    enabled: false,
    provider: '',
    model: 'gpt-4o-transcribe',
    language: 'uk-UA',
    alternative_languages: ['ru-RU'],
  },
  routing: { classify: [], normalize: [], ocr: [], intent: [] },
  monthly_token_budget: 2_000_000,
})
const usage = ref<AiSettings['usage'] | null>(null)
const providerMeta = ref<AiProviderMeta[]>([])
const sttProviderList = ref<AiSettings['stt_providers']>([])
const speechLanguageList = ref<SpeechLanguage[]>([])
const keyHints = ref<Record<string, string | null>>({})
const models = ref<AiModelChoices>({ local: [], providers: {}, live: [], stt: {} })
const prompts = ref<AiPrompt[]>([])
const selectedPromptKey = ref('')
const promptDraft = ref('')
const promptNote = ref('')
const promptHistory = ref<AiPromptRevision[]>([])
const promptBusy = ref(false)
// One key field / test result per provider: several services can be active at
// once, and each keeps its own key.
const keyInputs = reactive<Record<string, string>>({})
const keyTests = reactive<Record<string, AiBackendCheck | null>>({})
const keyTesting = ref('')
const newModelInputs = reactive<Record<string, string>>({})

const sttKeyInput = ref('')
const sttKeyTest = ref<AiBackendCheck | null>(null)
const sttKeyTesting = ref(false)

const check = ref<AiCheckResult | null>(null)
const checking = ref(false)

const TASKS: { key: AiTask; label: string; hint: string; vision?: boolean }[] = [
  {
    key: 'classify',
    label: 'Категорії товарів',
    hint: 'Позиції чека → дерево категорій. Багато коротких запитів — локальна модель тут дешевша.',
  },
  {
    key: 'normalize',
    label: 'Запити списку покупок',
    hint: 'Фраза «молоко» → пошуковий запит під конкретну мережу. Результат кешується назавжди.',
  },
  {
    key: 'ocr',
    label: 'OCR чеків (фото)',
    hint: 'Потрібна модель із підтримкою зображень. Локальний бекенд сюди не годиться — його кроки пропускаються.',
    vision: true,
  },
  {
    key: 'intent',
    label: 'Розбір вільного тексту й голосових (бот)',
    hint: '«додай чек …», «заправився на 1000грн». Змішані російсько-українські фрази — маленька локальна модель на них ламається.',
  },
]

const AI_LOCAL = 'local'

// --- Block 1: services ----------------------------------------------------
function providerLabel(id: string) {
  if (id === AI_LOCAL) return 'Локальна модель (Ollama)'
  return config.providers.find((p) => p.id === id)?.label
    || providerMeta.value.find((p) => p.id === id)?.label
    || id
}

/** What the model checklist offers: the provider's live catalogue when we have
 * one, otherwise the static fallback list (no key yet, or endpoint down). */
function providerCatalogue(id: string): string[] {
  const fetched = (models.value.providers?.[id] ?? []).map((m) => m.id)
  const fallback = providerMeta.value.find((p) => p.id === id)?.fallback_models ?? []
  const conf = config.providers.find((p) => p.id === id)
  return [...new Set([...(fetched.length ? fetched : fallback), ...(conf?.models ?? [])])]
}

/** Whether that checklist is the provider's own answer or the static set —
 * worth saying, since a fallback list can be out of date by a whole model
 * generation and hides anything the account has special access to. */
function catalogueIsLive(id: string): boolean {
  return (models.value.live ?? []).includes(id)
}

function modelDisplay(providerId: string, modelId: string) {
  const entry = (models.value.providers?.[providerId] ?? []).find((m) => m.id === modelId)
  return entry && entry.display_name !== entry.id ? `${entry.display_name} · ${entry.id}` : modelId
}

function toggleModel(providerId: string, modelId: string, on: boolean) {
  const conf = config.providers.find((p) => p.id === providerId)
  if (!conf) return
  conf.models = on
    ? [...conf.models, modelId].filter((m, i, all) => all.indexOf(m) === i)
    : conf.models.filter((m) => m !== modelId)
}

/** A catalogue is best-effort: an endpoint may serve a model it does not list. */
function addManualModel(providerId: string) {
  const value = (newModelInputs[providerId] || '').trim()
  if (!value) return
  toggleModel(providerId, value, true)
  newModelInputs[providerId] = ''
}

const enabledProviders = computed(() => config.providers.filter((p) => p.enabled))

async function saveProviderKey(providerId: string, clear = false) {
  saving.value = true
  error.value = ''
  try {
    applyKeys(await api.ai.setCloudKey(providerId, clear ? null : (keyInputs[providerId] || '').trim()))
    keyInputs[providerId] = ''
    keyTests[providerId] = null
    await loadModels() // the catalogue is only reachable with a key
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    saving.value = false
  }
}

// Tests the key being typed if the field has one, otherwise the stored key —
// so a key can be validated before it's committed.
async function testProviderKey(providerId: string) {
  keyTesting.value = providerId
  keyTests[providerId] = null
  try {
    const conf = config.providers.find((p) => p.id === providerId)
    keyTests[providerId] = await api.ai.testKey(
      providerId,
      (keyInputs[providerId] || '').trim() || null,
      conf?.models[0] || null,
      conf?.workspace_id || null,
      conf?.base_url || null,
    )
  } catch (e) {
    keyTests[providerId] = { ok: false, detail: (e as Error).message }
  } finally {
    keyTesting.value = ''
  }
}

function canTestKey(providerId: string) {
  const conf = config.providers.find((p) => p.id === providerId)
  if (conf?.custom) return Boolean(conf.base_url.trim())
  return Boolean((keyInputs[providerId] || '').trim() || keyHints.value[providerId])
}

function addCustomProvider() {
  config.providers.push({
    id: `custom_openai_${Date.now().toString(36)}`,
    label: 'Custom OpenAI-compatible',
    enabled: true,
    base_url: '',
    usage_url: '',
    models: [],
    workspace_id: '',
    custom: true,
  })
}

/** Removing a service also drops every route step that pointed at it —
 * otherwise saving fails validation on a route to a service that is gone. */
function removeProvider(id: string) {
  config.providers = config.providers.filter((p) => p.id !== id)
  for (const task of Object.keys(config.routing) as AiTask[]) {
    config.routing[task] = config.routing[task].filter((t) => t.provider !== id)
  }
  if (config.speech.provider === id) config.speech.provider = ''
}

// --- Block 2: routing -----------------------------------------------------
function routeTargets(task: AiTask): AiRouteTarget[] {
  if (!config.routing[task]) config.routing[task] = []
  return config.routing[task]
}

/** Services a route step can point at: the local backend plus every service
 * switched on in block 1. */
const routeProviderOptions = computed(() => [
  { id: AI_LOCAL, label: providerLabel(AI_LOCAL) },
  ...enabledProviders.value.map((p) => ({ id: p.id, label: p.label || p.id })),
])

function targetModelOptions(providerId: string): string[] {
  if (providerId === AI_LOCAL) {
    return [...new Set([...models.value.local, config.local.model].filter(Boolean))]
  }
  return config.providers.find((p) => p.id === providerId)?.models ?? []
}

function addTarget(task: AiTask) {
  const first = routeProviderOptions.value[0]
  if (!first) return
  const targets = routeTargets(task)
  targets.push({ provider: first.id, model: targetModelOptions(first.id)[0] ?? '' })
}

function removeTarget(task: AiTask, index: number) {
  routeTargets(task).splice(index, 1)
}

function moveTarget(task: AiTask, index: number, delta: number) {
  const targets = routeTargets(task)
  const to = index + delta
  if (to < 0 || to >= targets.length) return
  const [moved] = targets.splice(index, 1)
  targets.splice(to, 0, moved!)
}

// Switching the service of a step leaves the previous service's model behind,
// and it would be rejected — offer that service's first active model instead.
function setTargetProvider(task: AiTask, index: number, providerId: string) {
  const target = routeTargets(task)[index]
  if (!target) return
  target.provider = providerId
  target.model = targetModelOptions(providerId)[0] ?? ''
}

/** Sensible starting point for an empty routing table: everything through the
 * first enabled service, with the local backend leading the cheap tasks. */
function autofillRouting() {
  const cloud = enabledProviders.value[0]
  for (const task of TASKS) {
    const targets: AiRouteTarget[] = []
    if (!task.vision && config.local.enabled) {
      targets.push({ provider: AI_LOCAL, model: config.local.model })
    }
    if (cloud && cloud.models.length) targets.push({ provider: cloud.id, model: cloud.models[0]! })
    if (task.key === 'intent') targets.reverse()   // mixed-language phrases need the cloud first
    config.routing[task.key] = targets
  }
}

// --- Speech (its own API, its own key) ------------------------------------
const sttProviders = computed(() => sttProviderList.value)
const isGoogleStt = computed(() => config.speech.provider === 'google_speech')
const currentSttProvider = computed(
  () => sttProviderList.value.find((p) => p.id === config.speech.provider) ?? null,
)
// A provider with a fixed catalogue gets a dropdown; a self-hosted endpoint can
// run any model name, so it keeps a free-text field. The curated ids come
// first, then whatever transcription models the provider's API reports.
const sttModelOptions = computed(() => {
  const id = config.speech.provider
  const known = [
    ...(currentSttProvider.value?.models ?? []),
    ...(models.value.stt?.[id] ?? []),
  ].filter(Boolean)
  // An empty list is the signal for the free-text field, so nothing is added to
  // it; the configured model only joins a list that already exists, so a
  // hand-typed id does not vanish from the menu.
  if (!known.length) return []
  return [...new Set([...known, config.speech.model].filter(Boolean))]
})
const sttKeyHint = computed(() => keyHints.value[config.speech.provider] ?? null)
const sttUsageUrl = computed(() => currentSttProvider.value?.usage_url || '')
// The menu always contains whatever is configured, even a code the backend does
// not offer (an older config, or a hand-edited one) — otherwise picking the
// provider would silently change the language.
const speechLanguages = computed(() => {
  const known = speechLanguageList.value
  const extra = [config.speech.language, ...(config.speech.alternative_languages ?? [])]
    .filter((code) => code && !known.some((l) => l.code === code))
    .map((code) => ({ code, label: code }))
  return [...known, ...extra]
})

/** Everything except the primary language — each can be switched on as a retry. */
const secondaryLanguages = computed(() =>
  speechLanguages.value.filter((l) => l.code !== config.speech.language),
)

function isAlternative(code: string) {
  return (config.speech.alternative_languages ?? []).includes(code)
}

function toggleAlternative(code: string, on: boolean) {
  const rest = (config.speech.alternative_languages ?? []).filter((c) => c !== code)
  config.speech.alternative_languages = on
    ? [...rest, code].sort(
        (a, b) =>
          speechLanguages.value.findIndex((l) => l.code === a) -
          speechLanguages.value.findIndex((l) => l.code === b),
      )
    : rest
}

// The primary language must never also sit in the list of retries — that would
// offer a "try it again in…" button that changes nothing.
watch(
  () => config.speech.language,
  (code) => {
    config.speech.alternative_languages = (config.speech.alternative_languages ?? []).filter(
      (c) => c !== code,
    )
  },
)
// Switching speech provider leaves the old provider's model id behind, and the
// new one rejects it — offer its own default straight away (the backend snaps it
// too, on save).
watch(
  () => config.speech.provider,
  async () => {
    if (loading.value) return
    sttKeyInput.value = ''
    sttKeyTest.value = null
    const options = sttModelOptions.value
    if (options.length && !options.includes(config.speech.model)) {
      config.speech.model = options[0]!
    }
    // Persist the choice immediately: otherwise a key saved right after picking
    // a provider belongs to a provider the server does not know about yet.
    await save()
  },
)

async function saveSttKey(clear = false) {
  saving.value = true
  error.value = ''
  try {
    applyKeys(await api.ai.setCloudKey(config.speech.provider, clear ? null : sttKeyInput.value.trim()))
    sttKeyInput.value = ''
    sttKeyTest.value = null
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    saving.value = false
  }
}

async function testSttKey() {
  sttKeyTesting.value = true
  sttKeyTest.value = null
  try {
    sttKeyTest.value = await api.ai.testKey(
      config.speech.provider,
      sttKeyInput.value.trim() || null,
      null,
      null,
      null,
      'stt',
    )
  } catch (e) {
    sttKeyTest.value = { ok: false, detail: (e as Error).message }
  } finally {
    sttKeyTesting.value = false
  }
}

function apply(data: AiSettings) {
  Object.assign(config, data.config)
  applyKeys(data)
}

/** Everything except `config` — for responses that must not overwrite the form.
 *
 * Saving a key is its own action: the rest of the page may hold edits the user
 * has not pressed "Зберегти" for yet, and replacing `config` with the stored
 * copy would throw them away and make the just-saved key look like it did not
 * stick. */
function applyKeys(data: AiSettings) {
  usage.value = data.usage
  providerMeta.value = data.providers
  sttProviderList.value = data.stt_providers ?? []
  speechLanguageList.value = data.speech_languages ?? []
  keyHints.value = data.cloud_key_hints
}

const modelsLoading = ref(false)
// Which single service is refreshing right now ('' = none). Per-service, so one
// slow endpoint does not grey out the other services' buttons.
const providerModelsLoading = ref('')
// Per-service outcome of the last refresh, shown next to its button.
const providerModelsNote = reactive<Record<string, string>>({})

/** Refresh the catalogue of one service only. Cheaper and clearer than the
 * page-wide refresh: right after saving a key you want *this* list. */
async function loadProviderModels(id: string) {
  providerModelsLoading.value = id
  delete providerModelsNote[id]
  try {
    const data: AiProviderModels = await api.ai.providerModels(id)
    models.value = {
      ...models.value,
      providers: { ...models.value.providers, [id]: data.models },
      live: data.live
        ? [...new Set([...(models.value.live ?? []), id])]
        : (models.value.live ?? []).filter((p) => p !== id),
      stt: { ...(models.value.stt ?? {}), [id]: data.stt },
    }
    // Same rule as the page-wide refresh: take only the healed model selection,
    // never the whole stored config — the form may hold unsaved edits.
    const healed = data.config?.providers.find((p) => p.id === id)
    const conf = config.providers.find((p) => p.id === id)
    if (healed && conf) conf.models = healed.models
    providerModelsNote[id] = data.live
      ? `з API: ${data.models.length}`
      : data.models.length
        ? 'сервіс не відповів — типовий набір'
        : 'фіксований набір (сервіс не має списку моделей)'
  } catch (e) {
    providerModelsNote[id] = e instanceof Error ? e.message : 'не вдалося оновити'
  } finally {
    providerModelsLoading.value = ''
  }
}

async function loadModels() {
  modelsLoading.value = true
  try {
    const data = await api.ai.models()
    models.value = {
      local: data.local,
      providers: data.providers ?? {},
      live: data.live ?? [],
      stt: data.stt ?? {},
    }
    // The endpoint may have dropped models the provider has retired. Only the
    // model selection is taken from its response: the rest of the form may hold
    // unsaved edits that the stored config would silently revert.
    if (data.config) {
      for (const conf of config.providers) {
        const healed = data.config.providers.find((p) => p.id === conf.id)
        if (healed) conf.models = healed.models
      }
    }
  } catch {
    /* checklists fall back to the static catalogue */
  } finally {
    modelsLoading.value = false
  }
}

const log = ref<AiLogEntry[]>([])
const logLoading = ref(false)
const expanded = ref<Set<number>>(new Set())

const LOG_PAGE_SIZE = 15
const logPage = ref(0)
const logPageCount = computed(() => Math.max(1, Math.ceil(log.value.length / LOG_PAGE_SIZE)))
// [originalIndex, entry] pairs for the current page — index stays stable so
// expand/toggle keeps working across page changes.
const pagedLog = computed<[number, AiLogEntry][]>(() => {
  const start = logPage.value * LOG_PAGE_SIZE
  return log.value
    .map((e, i) => [i, e] as [number, AiLogEntry])
    .slice(start, start + LOG_PAGE_SIZE)
})
function setLogPage(p: number) {
  logPage.value = Math.min(Math.max(0, p), logPageCount.value - 1)
}
function toggleLog(i: number) {
  const s = new Set(expanded.value)
  s.has(i) ? s.delete(i) : s.add(i)
  expanded.value = s
}

async function loadLog() {
  logLoading.value = true
  try {
    log.value = (await api.ai.log()).entries
    expanded.value = new Set()
    logPage.value = 0
    if (usage.value) usage.value = (await api.ai.get()).usage
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    logLoading.value = false
  }
}

function fmtLogTime(iso: string) {
  return new Date(iso).toLocaleString('uk-UA', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
}

// --- MCP server (ROADMAP Крок 4) -----------------------------------------
const mcpCfg = reactive<McpConfig>({ enabled: true, tools: {} })
const mcpTools = ref<McpToolInfo[]>([])
const mcpMount = ref('/mcp')
const mcpSavedAt = ref('')
const mcpUrl = computed(() => `${BASE}${mcpMount.value}`)

function applyMcp(data: { config: McpConfig; tools: McpToolInfo[]; mount_path: string }) {
  Object.assign(mcpCfg, data.config)
  mcpCfg.tools = { ...data.config.tools }
  mcpTools.value = data.tools
  mcpMount.value = data.mount_path
}

async function saveMcp() {
  saving.value = true
  error.value = ''
  try {
    applyMcp(await api.mcp.save(JSON.parse(JSON.stringify(mcpCfg))))
    mcpSavedAt.value = new Date().toLocaleTimeString('uk-UA')
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    saving.value = false
  }
}

const inboxCfg = reactive<InboxConfig>({
  receipt_ratio: 2.0,
  color_max_saturation: 0.12,
  color_max_nongray_fraction: 0.1,
  max_file_mb: 25,
})
const inboxSavedAt = ref('')

async function saveInboxCfg() {
  try {
    Object.assign(inboxCfg, await api.inbox.saveConfig(JSON.parse(JSON.stringify(inboxCfg))))
    inboxSavedAt.value = new Date().toLocaleTimeString('uk-UA')
  } catch (e) {
    error.value = (e as Error).message
  }
}

// --- ДПС fiscal-check API --------------------------------------------------
const taxCabinet = ref<TaxCabinetSettings | null>(null)
const taxTokenInput = ref('')
const taxBusy = ref(false)
const taxSavedAt = ref('')
const fiscalJobs = ref<any[]>([])
const fiscalUrl = ref('')
const fiscalBrowserJob = ref<any>(null)
const fiscalShot = ref('')
const fiscalBrowserState = ref<any>(null)
const fiscalBrowserBusy = ref(false)
const fiscalWebResponse = ref('')
const fiscalResponseFile = ref<File | null>(null)
let fiscalPollTimer: number | null = null
let fiscalPollRunning = false

function stopFiscalPolling(){
  if(fiscalPollTimer!==null)window.clearInterval(fiscalPollTimer)
  fiscalPollTimer=null
}
function startFiscalPolling(){
  stopFiscalPolling()
  fiscalPollTimer=window.setInterval(pollFiscalBrowser,2500)
}
async function pollFiscalBrowser(){
  const j=fiscalBrowserJob.value
  if(!j||fiscalBrowserBusy.value||fiscalPollRunning)return
  fiscalPollRunning=true
  try {
    fiscalBrowserState.value=await api.fiscalQueue.state(j.id,j.session_id)
    fiscalShot.value=api.fiscalQueue.screenshot(j.id,j.session_id)+'&t='+Date.now()
    if(fiscalBrowserState.value?.result_ready)await checkFiscalBrowser()
  } catch(e) {
    detachFiscalBrowser((e as Error).message)
  } finally { fiscalPollRunning=false }
}
/** Drop the CAPTCHA panel: the session behind it is gone or was never started. */
function detachFiscalBrowser(message = '') {
  stopFiscalPolling()
  fiscalBrowserJob.value = null
  fiscalBrowserState.value = null
  fiscalShot.value = ''
  if (message) error.value = message
}

async function loadFiscalJobs() {
  fiscalJobs.value = await api.fiscalQueue.list()
  // Attach to a CAPTCHA session only when one is already running. Starting
  // Chrome here would make opening Settings wait on a browser launch, and a
  // session left over from a previous run is already dead — the backend reports
  // session_id only while the session really exists.
  const active = fiscalJobs.value.find((j) => j.status === 'captcha' && j.session_id)
  if (active) { fiscalBrowserJob.value = active; await refreshFiscalShot(); startFiscalPolling() }
  else if (fiscalBrowserJob.value) detachFiscalBrowser()
}

/** The "CAPTCHA" button: attach to the running session, or start one. */
async function openFiscalCaptcha(job: any) {
  if (!job.session_id) { fiscalBrowserJob.value = job; await restartFiscalBrowser(); return }
  fiscalBrowserJob.value = job
  await refreshFiscalShot()
  startFiscalPolling()
}
async function addFiscalJob() { await api.fiscalQueue.add(fiscalUrl.value); fiscalUrl.value=''; await loadFiscalJobs() }
async function runFiscalJob(job:any) {
  const next=await api.fiscalQueue.run(job.id); await loadFiscalJobs()
}
async function runFiscalPublicApi(job:any) {
  fiscalBrowserBusy.value=true
  try { await api.fiscalQueue.runPublicApi(job.id); await loadFiscalJobs() }
  catch(e){ error.value=(e as Error).message }
  finally { fiscalBrowserBusy.value=false }
}
async function importFiscalWebResponse(){
  if(!fiscalWebResponse.value.trim())return
  fiscalBrowserBusy.value=true
  try {
    const next=await api.fiscalQueue.importResponse(fiscalWebResponse.value)
    fiscalWebResponse.value=''; stopFiscalPolling(); fiscalBrowserJob.value=null
    await loadFiscalJobs()
    if(next.receipt_id)window.location.hash=`#/receipts/${next.receipt_id}`
  } catch(e){ error.value=(e as Error).message }
  finally { fiscalBrowserBusy.value=false }
}
async function importFiscalResponseFile(){
  if(!fiscalResponseFile.value)return
  fiscalBrowserBusy.value=true
  try {
    const next=await api.fiscalQueue.importResponseFile(fiscalResponseFile.value)
    fiscalResponseFile.value=null; stopFiscalPolling(); fiscalBrowserJob.value=null
    await loadFiscalJobs()
    if(next.receipt_id)window.location.hash=`#/receipts/${next.receipt_id}`
  } catch(e){ error.value=(e as Error).message }
  finally { fiscalBrowserBusy.value=false }
}
function selectFiscalResponseFile(event:Event){
  fiscalResponseFile.value=(event.target as HTMLInputElement).files?.[0]||null
}
async function refreshFiscalShot(){
  const j=fiscalBrowserJob.value
  if(!j)return
  fiscalShot.value=api.fiscalQueue.screenshot(j.id,j.session_id)+'&t='+Date.now()
  try { fiscalBrowserState.value=await api.fiscalQueue.state(j.id,j.session_id) }
  catch (e) { detachFiscalBrowser((e as Error).message) }
}
async function fiscalClick(e:MouseEvent){
  const j=fiscalBrowserJob.value, img=e.currentTarget as HTMLImageElement
  if(!j||!img.naturalWidth)return
  const r=img.getBoundingClientRect(); await api.fiscalQueue.click(j.id,j.session_id,(e.clientX-r.left)*img.naturalWidth/r.width,(e.clientY-r.top)*img.naturalHeight/r.height)
  setTimeout(refreshFiscalShot,700)
}
async function checkFiscalBrowser(){
  const j=fiscalBrowserJob.value; if(!j)return
  const next=await api.fiscalQueue.check(j.id,j.session_id)
  if(next.status==='imported'){
    stopFiscalPolling(); fiscalBrowserJob.value=null; fiscalBrowserState.value=null
    await loadFiscalJobs()
  } else await refreshFiscalShot()
}
async function reloadFiscalBrowser(){
  const j=fiscalBrowserJob.value; if(!j)return
  fiscalBrowserBusy.value=true
  try { fiscalBrowserState.value=await api.fiscalQueue.reload(j.id,j.session_id); await refreshFiscalShot() }
  catch(e){ error.value=(e as Error).message }
  finally { fiscalBrowserBusy.value=false }
}
async function restartFiscalBrowser(){
  const j=fiscalBrowserJob.value; if(!j)return
  fiscalBrowserBusy.value=true
  try {
    const next=await api.fiscalQueue.startBrowser(j.id)
    fiscalBrowserJob.value=next
    fiscalJobs.value=await api.fiscalQueue.list()
    await refreshFiscalShot()
    startFiscalPolling()
  } catch(e){ detachFiscalBrowser((e as Error).message) }
  finally { fiscalBrowserBusy.value=false }
}

// --- Telegram -------------------------------------------------------------
const telegram = ref<TelegramSettings | null>(null)
const telegramForm = reactive({ enabled: false, bot_token: '', public_url: '', capture_enabled: true })
const telegramBusy = ref(false)
const telegramSavedAt = ref('')
const telegramMessages = ref<TelegramMessageLog[]>([])
const telegramMessageTotal = ref(0)
const telegramLogLoading = ref(false)
const telegramConversation = ref('')
const telegramLogPageSize = 20
const telegramLogPage = ref(0)

const telegramConversations = computed(() => {
  const rows = new Map<string, string>()
  for (const message of telegramMessages.value) {
    const branch = message.message_thread_id
      ? `Тема ${message.message_thread_id}`
      : `Гілка ${message.root_message_id}`
    rows.set(message.conversation_key, `${message.chat_title || message.chat_id} · ${branch}`)
  }
  return [...rows.entries()]
})

const filteredTelegramMessages = computed(() => telegramConversation.value
  ? telegramMessages.value.filter((message) => message.conversation_key === telegramConversation.value)
  : telegramMessages.value)

const visibleTelegramMessages = computed(() => {
  const start = telegramLogPage.value * telegramLogPageSize
  return filteredTelegramMessages.value.slice(start, start + telegramLogPageSize)
})

watch(telegramConversation, () => { telegramLogPage.value = 0 })

async function refreshTelegramMessages() {
  telegramLogLoading.value = true
  try {
    const result = await api.telegram.messages()
    telegramMessages.value = result.items
    telegramMessageTotal.value = result.total
    telegramLogPage.value = 0
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    telegramLogLoading.value = false
  }
}

async function saveTelegram() {
  telegramBusy.value = true
  error.value = ''
  try {
    telegram.value = await api.telegram.save({
      enabled: telegramForm.enabled,
      bot_token: telegramForm.bot_token.trim() || null,
      public_url: telegramForm.public_url.trim(),
      capture_enabled: telegramForm.capture_enabled,
    })
    telegramForm.enabled = telegram.value.config.enabled
    telegramForm.public_url = telegram.value.config.public_url
    telegramForm.capture_enabled = telegram.value.config.capture_enabled
    telegramForm.bot_token = ''
    telegramSavedAt.value = new Date().toLocaleTimeString('uk-UA')
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    telegramBusy.value = false
  }
}

async function revokeTelegramChat(chatId: number) {
  telegram.value = await api.telegram.revokeChat(chatId)
}

async function approveTelegramChat(chatId: number) {
  telegram.value = await api.telegram.approveChat(chatId)
}

async function rejectTelegramChat(chatId: number) {
  telegram.value = await api.telegram.rejectChat(chatId)
}

async function refreshTelegram() {
  telegram.value = await api.telegram.get()
  telegramForm.enabled = telegram.value.config.enabled
  telegramForm.public_url = telegram.value.config.public_url || ''
  telegramForm.capture_enabled = telegram.value.config.capture_enabled
}

async function saveTaxToken(clear = false) {
  taxBusy.value = true
  error.value = ''
  try {
    taxCabinet.value = await api.taxCabinet.save(
      clear ? null : taxTokenInput.value.trim() || null,
      clear,
    )
    taxTokenInput.value = ''
    taxSavedAt.value = new Date().toLocaleTimeString('uk-UA')
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    taxBusy.value = false
  }
}

// --- Google Drive ---------------------------------------------------------
const drive = ref<DriveStatus | null>(null)
const driveBusy = ref('')
const folderStack = ref<{ id: string | null; name: string }[]>([{ id: null, name: 'Мій диск' }])
const folders = ref<DriveFolder[]>([])
const newFolderName = ref('')
const newFolderColor = ref<string | null>(null)
const clientForm = reactive({ client_id: '', client_secret: '' })
const palette = ref<string[]>([])
const colorPickerFor = ref<string | null>(null) // folder id whose swatch popover is open

async function loadDrive() {
  drive.value = await api.inbox.driveStatus()
  clientForm.client_id = drive.value?.client_id ?? ''
  clientForm.client_secret = ''
  if (drive.value?.connected && !palette.value.length) {
    try {
      palette.value = (await api.inbox.driveColorPalette()).colors
    } catch {
      /* palette stays empty → picker just hidden */
    }
  }
}

async function setFolderColor(folderId: string, color: string | null) {
  colorPickerFor.value = null
  try {
    drive.value = await api.inbox.driveSetFolderColor(folderId, color)
    // reflect the new colour in the open browser list
    const row = folders.value.find((f) => f.id === folderId)
    if (row) row.color = color
    await openFolder(folderStack.value[folderStack.value.length - 1]!, folderStack.value.length - 1)
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function saveDriveClient() {
  driveBusy.value = 'client'
  try {
    drive.value = await api.inbox.driveSaveClient(
      clientForm.client_id.trim(),
      clientForm.client_secret.trim() || null,
    )
    clientForm.client_secret = ''
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    driveBusy.value = ''
  }
}

async function connectDrive() {
  driveBusy.value = 'connect'
  try {
    const { authorize_url } = await api.inbox.driveAuthorize()
    window.open(authorize_url, '_blank', 'noopener,width=520,height=680')
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    driveBusy.value = ''
  }
}

async function disconnectDrive() {
  if (!confirm('Відключити Google Drive? Нові документи знову зберігатимуться локально.')) return
  driveBusy.value = 'disconnect'
  try {
    drive.value = await api.inbox.driveDisconnect()
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    driveBusy.value = ''
  }
}

async function openFolder(f: { id: string | null; name: string }, depth: number) {
  folderStack.value = folderStack.value.slice(0, depth + 1)
  if (folderStack.value[depth]?.id !== f.id) folderStack.value.push(f)
  try {
    folders.value = await api.inbox.driveFolders(f.id ?? undefined)
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function makeFolder() {
  const name = newFolderName.value.trim()
  if (!name) return
  const parent = folderStack.value[folderStack.value.length - 1]
  if (!parent) return
  try {
    await api.inbox.driveCreateFolder(name, parent.id, newFolderColor.value)
    newFolderName.value = ''
    newFolderColor.value = null
    await openFolder(parent, folderStack.value.length - 1)
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function pickFolder(kind: 'root' | 'inbox', f: DriveFolder) {
  try {
    drive.value =
      kind === 'root'
        ? await api.inbox.driveSetFolders({ root_folder_id: f.id, root_folder_name: f.name, root_folder_color: f.color })
        : await api.inbox.driveSetFolders({ inbox_folder_id: f.id, inbox_folder_name: f.name, inbox_folder_color: f.color })
    clientForm.client_id = drive.value.client_id
    inboxSavedAt.value = `${kind === 'root' ? 'коренева' : '«Необроблені»'}: «${f.name}» — ${new Date().toLocaleTimeString('uk-UA')}`
    // Re-anchor the browser at the (possibly new) base folder.
    await browseFromRoot()
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function clearFolder(kind: 'root' | 'inbox') {
  try {
    drive.value = await api.inbox.driveSetFolders(
      kind === 'root' ? { clear_root: true } : { clear_inbox: true },
    )
    await browseFromRoot()
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function browseFromRoot() {
  const base = drive.value?.root_folder_id
    ? { id: drive.value.root_folder_id, name: drive.value.root_folder_name || 'Коренева папка' }
    : { id: null, name: 'Мій диск' }
  folderStack.value = [base]
  await openFolder(base, 0)
}

// Список покупок: автоклассификация позиций без категории.
const shopping = reactive({ auto_categorize: true, auto_categorize_delay_minutes: 10 })
const shoppingBusy = ref(false)
const shoppingSaved = ref('')

async function loadShopping() {
  Object.assign(shopping, await api.shoppingSettings.get())
}

async function selectPrompt(key: string) {
  const prompt = prompts.value.find((item) => item.key === key)
  if (!prompt) return
  selectedPromptKey.value = key
  promptDraft.value = prompt.content
  promptNote.value = ''
  try {
    promptHistory.value = await api.ai.promptHistory(key)
  } catch (e) {
    error.value = (e as Error).message
  }
}

const selectedPrompt = computed(() => prompts.value.find((item) => item.key === selectedPromptKey.value))

async function loadPrompts() {
  prompts.value = await api.ai.prompts()
  if (!selectedPromptKey.value && prompts.value[0]) await selectPrompt(prompts.value[0].key)
}

async function savePrompt() {
  if (!selectedPromptKey.value || !promptDraft.value.trim()) return
  promptBusy.value = true
  error.value = ''
  try {
    const saved = await api.ai.savePrompt(selectedPromptKey.value, promptDraft.value, promptNote.value)
    const index = prompts.value.findIndex((item) => item.key === saved.key)
    if (index >= 0) prompts.value[index] = saved
    promptDraft.value = saved.content
    promptNote.value = ''
    promptHistory.value = await api.ai.promptHistory(saved.key)
    showToast(`Версія ${saved.version} збережена`, 'success')
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    promptBusy.value = false
  }
}

function usePromptRevision(revision: AiPromptRevision) {
  promptDraft.value = revision.content
  promptNote.value = `Відновлено з версії ${revision.version}`
}
async function saveShopping() {
  shoppingBusy.value = true
  shoppingSaved.value = ''
  try {
    Object.assign(shopping, await api.shoppingSettings.save({
      auto_categorize: shopping.auto_categorize,
      auto_categorize_delay_minutes: Number(shopping.auto_categorize_delay_minutes) || 10,
    }))
    shoppingSaved.value = 'Збережено'
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    shoppingBusy.value = false
  }
}

onMounted(async () => {
  try {
    apply(await api.ai.get())
    await loadModels()
    await loadLog()
    applyMcp(await api.mcp.get())
    Object.assign(inboxCfg, await api.inbox.config())
    taxCabinet.value = await api.taxCabinet.get()
    await loadFiscalJobs()
    await refreshTelegram()
    await refreshTelegramMessages()
    await loadDrive()
    await loadShopping()
    await loadPrompts()
    if (drive.value?.connected) await browseFromRoot()
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
    await nextTick()
    observeSections()
  }
})


watch(error, (message) => showToast(message, 'error'))
watch(
  [savedAt, mcpSavedAt, inboxSavedAt, taxSavedAt, telegramSavedAt],
  (values, previous) => {
    if (values.some((value, index) => value && value !== previous[index])) {
      showToast('Налаштування збережено', 'success')
    }
  },
)

async function save() {
  saving.value = true
  error.value = ''
  try {
    apply(await api.ai.save(JSON.parse(JSON.stringify(config))))
    savedAt.value = new Date().toLocaleTimeString('uk-UA')
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    saving.value = false
  }
}

async function runCheck() {
  checking.value = true
  check.value = null
  try {
    check.value = await api.ai.check()
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    checking.value = false
  }
}

// --- Section navigation -------------------------------------------------
// Hash routing owns `location.hash`, so plain `<a href="#ai">` anchors would
// hijack the router. Scroll by ref instead, and track the visible section.
// The sidebar already chose the group; this tree is the level below it.
const SECTIONS: Record<string, { title: string; items: { id: string; label: string }[] }> = {
  'ai-providers': {
    title: 'AI · сервіси та ключі',
    items: [
      { id: 'ai-local', label: 'Локальна модель' },
      { id: 'ai-cloud', label: 'Хмарні сервіси' },
      { id: 'ai-speech', label: 'Голос (STT)' },
    ],
  },
  'ai-routing': {
    title: 'AI · маршрути задач',
    items: [
      { id: 'ai-routes', label: 'Пріоритети виклику' },
      { id: 'ai-budget', label: 'Обмеження витрат' },
      { id: 'ai-usage', label: 'Використання та журнал' },
    ],
  },
  'ai-prompts': { title: 'AI · промти', items: [] },
  integrations: {
    title: 'Інтеграції та доступ',
    items: [
      { id: 'telegram', label: 'Telegram-бот' },
      { id: 'mcp', label: 'MCP-сервер' },
    ],
  },
  scanner: {
    title: 'Сканування та розпізнавання',
    items: [
      { id: 'inbox-routing', label: 'Роутинг інбокса' },
      { id: 'tax', label: 'ДПС · фіскальні чеки' },
    ],
  },
  storage: {
    title: 'Зберігання документів',
    items: [{ id: 'drive', label: 'Google Drive' }],
  },
  shopping: {
    title: 'Список покупок',
    items: [{ id: 'shopping-auto', label: 'Автоматична класифікація' }],
  },
}

const currentSections = computed(() => {
  if (group.value === 'ai-prompts') {
    return {
      title: 'AI · промти',
      items: prompts.value.map((prompt) => ({ id: prompt.key, label: prompt.title })),
    }
  }
  return SECTIONS[group.value] ?? SECTIONS['ai-providers']!
})
const activeSection = ref<string>('')

function scrollToSection(id: string) {
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

function selectSection(id: string) {
  if (group.value === 'ai-prompts') {
    void selectPrompt(id)
    return
  }
  scrollToSection(id)
}

let observer: IntersectionObserver | null = null
function observeSections() {
  observer?.disconnect()
  observer = new IntersectionObserver(
    (entries) => {
      for (const e of entries) {
        if (e.isIntersecting) activeSection.value = e.target.id
      }
    },
    { rootMargin: '-16px 0px -65% 0px', threshold: 0 },
  )
  for (const item of currentSections.value.items) {
    const el = document.getElementById(item.id)
    if (el) observer.observe(el)
  }
}

// Switching sidebar page swaps the rendered sections, so the observer has to be
// pointed at the new ones.
watch(group, async () => {
  activeSection.value = ''
  await nextTick()
  observeSections()
})
onBeforeUnmount(() => {
  observer?.disconnect()
  stopFiscalPolling()
  if (toastTimer !== null) window.clearTimeout(toastTimer)
})
</script>

<template>
  <Transition name="settings-toast">
    <div v-if="toast.visible" class="settings-toast" :class="`is-${toast.kind}`" role="status" aria-live="polite">
      <span>{{ toast.message }}</span>
      <button type="button" aria-label="Закрити повідомлення" @click="toast.visible = false">×</button>
    </div>
  </Transition>
  <div class="settings-head">
    <div style="flex: 1">
      <h2 style="margin-bottom: 4px">Налаштування · {{ currentSections.title }}</h2>
      <div class="muted">Конфігурація зберігається в базі (app_settings), не в змінних оточення.</div>
    </div>
    <span v-if="savedAt" class="muted">Збережено о {{ savedAt }}</span>
  </div>

  <div v-if="loading" class="muted">Завантаження…</div>

  <div v-if="!loading" class="settings-layout">
    <aside class="settings-side">
      <nav class="settings-tree">
        <div class="settings-tree-group">
          <span class="settings-tree-link is-group">{{ currentSections.title }}</span>
          <button
            v-for="it in currentSections.items"
            :key="it.id"
            type="button"
            class="settings-tree-link is-child"
            :class="{ 'is-active': group === 'ai-prompts' ? selectedPromptKey === it.id : activeSection === it.id }"
            @click="selectSection(it.id)"
          >
            {{ it.label }}
          </button>
        </div>
      </nav>
    </aside>

    <div class="settings-main">
    <template v-if="group === 'ai-providers'">
    <h3 class="settings-group">AI · сервіси та ключі</h3>
    <p class="muted" style="margin-top: 0">
      Тут вирішується, <strong>що взагалі доступно</strong>: які сервіси увімкнені, їхні
      ключі й моделі, які можна викликати. Куди саме йде кожна задача й у якому
      порядку — на сторінці «AI · маршрути».
    </p>

    <section id="ai-local" class="card" style="margin-bottom: 16px">
      <h3 style="margin-top: 0">Локальна модель (Ollama)</h3>
      <p class="muted" style="font-size: 12px; margin-top: 0">
        Працює на вашому обладнанні: безкоштовно й нічого не виходить за межі машини,
        але слабше на складних текстах. Зображення не читає — OCR туди не маршрутизується.
      </p>
      <label style="display: flex; gap: 8px; align-items: center; margin-bottom: 10px">
        <input type="checkbox" v-model="config.local.enabled" style="width: auto" />
        <strong>Увімкнути локальний бекенд</strong>
      </label>
      <div v-if="config.local.enabled" style="max-width: 620px">
        <div class="form-row">
          <label>URL сервера Ollama</label>
          <input v-model="config.local.url" placeholder="http://localhost:11434" />
        </div>
        <div class="form-row">
          <label>Модель за замовчуванням</label>
          <select v-model="config.local.model">
            <option v-for="m in targetModelOptions('local')" :key="m" :value="m">{{ m }}</option>
            <option v-if="!targetModelOptions('local').length" :value="config.local.model">
              {{ config.local.model || '— немає доступних —' }}
            </option>
          </select>
          <div class="muted" style="font-size: 12px">
            Використовується, коли крок маршруту не називає іншу модель.
          </div>
        </div>
        <div class="form-row">
          <label>Контекст (num_ctx)</label>
          <input type="number" min="2048" step="1024" v-model.number="config.local.num_ctx" />
          <div class="muted" style="font-size: 12px">
            Скільки тексту модель бачить за раз. Замало — довгий промпт мовчки обріжеться.
          </div>
        </div>
      </div>
    </section>

    <section id="ai-cloud" class="card" style="margin-bottom: 16px">
      <h3 style="margin-top: 0">Хмарні сервіси</h3>
      <p class="muted" style="font-size: 12px; margin-top: 0">
        Увімкніть кожен сервіс, яким збираєтеся користуватися, збережіть його ключ і
        позначте активні моделі. Ключі зберігаються окремо для кожного сервісу
        (шифрування Fernet, як токени магазинів), тому кілька сервісів можуть бути
        активні одночасно.
      </p>

      <div v-for="p in config.providers" :key="p.id" class="ai-provider">
        <div class="ai-provider-head">
          <label style="display: flex; gap: 8px; align-items: center; flex: 1">
            <input type="checkbox" v-model="p.enabled" style="width: auto" />
            <input v-if="p.custom" v-model="p.label" placeholder="Назва сервісу" style="max-width: 220px" />
            <strong v-else>{{ p.label }}</strong>
          </label>
          <span class="badge" :class="keyHints[p.id] ? 'badge-active' : ''">
            {{ keyHints[p.id] ? `ключ ${keyHints[p.id]}` : 'без ключа' }}
          </span>
          <span class="badge" :class="p.models.length ? 'badge-active' : ''">
            моделей: {{ p.models.length }}<template v-if="providerCatalogue(p.id).length">
              з {{ providerCatalogue(p.id).length }}</template>
          </span>
          <a v-if="p.usage_url" :href="p.usage_url" target="_blank" rel="noopener" style="font-size: 12px">📊 витрати ↗</a>
          <button v-if="p.custom" class="danger" @click="removeProvider(p.id)">Видалити</button>
        </div>

        <!-- Which models are ticked, without opening the service: the checklist
             is long and scrollable, so the answer to "what is this service
             allowed to run" must be readable from the collapsed card. -->
        <div v-if="p.models.length" class="ai-provider-models">
          <span v-for="m in p.models" :key="m" class="ai-model-chip">{{ m }}</span>
        </div>
        <div v-else-if="p.enabled" class="ai-provider-models muted" style="font-size: 12px">
          жодної моделі не позначено — маршрути не зможуть викликати цей сервіс
        </div>

        <div v-if="p.enabled" class="ai-provider-body">
          <div v-if="p.custom" class="form-row">
            <label>Base URL (без /chat/completions)</label>
            <input v-model="p.base_url" placeholder="https://example.com/v1 або http://localhost:8000/v1" autocomplete="off" />
            <div class="muted" style="font-size: 12px">
              Для безкоштовних маршрутів перевірте політику зберігання prompt/response,
              перш ніж слати туди фото чеків.
            </div>
          </div>
          <div v-if="p.custom" class="form-row">
            <label>Посилання на статистику (необовʼязково)</label>
            <input v-model="p.usage_url" placeholder="https://example.com/usage" autocomplete="off" />
          </div>
          <div v-if="p.id === 'anthropic'" class="form-row">
            <label>Workspace ID (необовʼязково)</label>
            <input v-model="p.workspace_id" placeholder="wrkspc_…" autocomplete="off" />
            <div class="muted" style="font-size: 12px">
              Потрібен лише identity-linked / admin ключам. Зберігається кнопкою «Зберегти» внизу.
            </div>
          </div>

          <div class="toolbar" style="margin-bottom: 4px">
            <PasswordInput
              :model-value="keyInputs[p.id] ?? ''"
              placeholder="ключ API…"
              @update:model-value="keyInputs[p.id] = $event"
              autocomplete="off"
              style="min-width: 240px"
            />
            <button :disabled="saving || !(keyInputs[p.id] || '').trim()" @click="saveProviderKey(p.id, false)">
              Зберегти ключ
            </button>
            <button class="secondary" :disabled="keyTesting === p.id || !canTestKey(p.id)" @click="testProviderKey(p.id)">
              {{ keyTesting === p.id ? 'Тест…' : 'Тест' }}
            </button>
            <button class="danger" :disabled="saving || !keyHints[p.id]" @click="saveProviderKey(p.id, true)">
              Видалити ключ
            </button>
          </div>
          <p class="muted" style="font-size: 12px; margin: 0">
            Кнопка «Зберегти ключ» діє одразу й окремо від «Зберегти» внизу сторінки.
          </p>
          <p v-if="keyTests[p.id]" style="margin: 6px 0 0">
            <span class="badge" :class="keyTests[p.id]!.ok ? 'badge-active' : 'badge-error'">
              {{ keyTests[p.id]!.ok ? 'ключ робочий' : 'помилка' }}
            </span>
            <span class="muted" style="margin-left: 6px">{{ keyTests[p.id]!.detail }}</span>
          </p>

          <div style="margin-top: 12px">
            <strong style="font-size: 13px">Активні моделі</strong>
            <div class="muted" style="font-size: 12px; margin-bottom: 6px">
              Лише позначені моделі можна обрати в маршрутах.
              <template v-if="catalogueIsLive(p.id)">Список отримано з API сервісу.</template>
              <template v-else>
                Показано типовий набір — збережіть ключ і натисніть «Оновити списки
                моделей», щоб підтягнути справжній список.
              </template>
            </div>
            <div class="ai-model-list">
              <label v-for="m in providerCatalogue(p.id)" :key="m" class="ai-model">
                <input
                  type="checkbox"
                  style="width: auto"
                  :checked="p.models.includes(m)"
                  @change="toggleModel(p.id, m, ($event.target as HTMLInputElement).checked)"
                />
                <span>{{ modelDisplay(p.id, m) }}</span>
              </label>
              <div v-if="!providerCatalogue(p.id).length" class="muted" style="font-size: 12px">
                Список порожній — збережіть ключ і оновіть моделі.
              </div>
            </div>
            <div class="toolbar" style="margin-top: 6px">
              <button
                class="secondary"
                :disabled="providerModelsLoading === p.id"
                @click="loadProviderModels(p.id)"
              >
                {{ providerModelsLoading === p.id ? 'Оновлення…' : '↻ Оновити моделі' }}
              </button>
              <span v-if="providerModelsNote[p.id]" class="muted" style="font-size: 12px">
                {{ providerModelsNote[p.id] }}
              </span>
            </div>
            <div class="toolbar" style="margin-top: 6px">
              <input
                v-model="newModelInputs[p.id]"
                placeholder="додати модель вручну (id)"
                style="min-width: 240px"
                @keyup.enter="addManualModel(p.id)"
              />
              <button class="secondary" :disabled="!(newModelInputs[p.id] || '').trim()" @click="addManualModel(p.id)">
                Додати
              </button>
            </div>
          </div>

          <p v-if="check?.providers?.[p.id]" style="margin: 8px 0 0">
            <span class="badge" :class="check.providers[p.id]!.ok ? 'badge-active' : 'badge-error'">
              {{ check.providers[p.id]!.ok ? 'зʼєднання ок' : 'помилка' }}
            </span>
            <span class="muted" style="margin-left: 6px">{{ check.providers[p.id]!.detail }}</span>
          </p>
        </div>
      </div>

      <div class="toolbar" style="margin-top: 12px">
        <button :disabled="saving" @click="save">Зберегти</button>
        <button class="secondary" @click="addCustomProvider">Додати OpenAI-сумісний сервіс</button>
        <button class="secondary" :disabled="modelsLoading" @click="loadModels">
          {{ modelsLoading ? 'Оновлення…' : 'Оновити моделі всіх сервісів' }}
        </button>
        <button class="secondary" :disabled="checking" @click="runCheck">
          {{ checking ? 'Перевірка…' : 'Перевірити зʼєднання' }}
        </button>
      </div>
      <p v-if="check" style="margin: 8px 0 0">
        <span class="badge" :class="check.local.ok ? 'badge-active' : 'badge-error'">локальна</span>
        <span class="muted" style="margin-left: 6px">{{ check.local.detail }}</span>
      </p>
    </section>

    <section id="ai-speech" class="card" style="margin-bottom: 16px">
      <h3 style="margin-top: 0">Голос (STT)</h3>
      <p class="muted" style="font-size: 12px; margin-top: 0">
        Транскрипція — окремий API від чату, тому сервіс і ключ тут свої й у маршрути
        вона не входить. Google Cloud Speech-to-Text працює за ключем API; решта — через
        /audio/transcriptions (в Anthropic такого немає, тому його тут немає).
      </p>
      <label style="display: flex; gap: 8px; align-items: center; margin-bottom: 10px">
        <input type="checkbox" v-model="config.speech.enabled" style="width: auto" />
        <strong>Розпізнавати голосові повідомлення</strong>
      </label>
      <div class="form-row" style="max-width: 620px">
        <label>Сервіс розпізнавання</label>
        <select v-model="config.speech.provider">
          <option value="">Не обрано — голосові зберігаються, але не розпізнаються</option>
          <option v-for="p in sttProviders" :key="p.id" :value="p.id">{{ p.label }}</option>
        </select>
      </div>

      <div v-if="config.speech.provider" style="border: 1px solid var(--border); border-radius: 6px; padding: 12px; max-width: 620px; margin-bottom: 12px">
        <div class="form-row">
          <label>Мова (основна)</label>
          <select v-model="config.speech.language">
            <option v-for="l in speechLanguages" :key="l.code" :value="l.code">
              {{ l.label }} ({{ l.code }})
            </option>
          </select>
          <div class="muted" style="font-size: 12px">
            Нею розпізнається кожне голосове насамперед.
          </div>
        </div>
        <div class="form-row">
          <label>Інші мови</label>
          <label
            v-for="l in secondaryLanguages"
            :key="l.code"
            style="display: flex; gap: 8px; align-items: center"
          >
            <input
              type="checkbox"
              style="width: auto"
              :checked="isAlternative(l.code)"
              @change="toggleAlternative(l.code, ($event.target as HTMLInputElement).checked)"
            />
            {{ l.label }} ({{ l.code }})
          </label>
          <div class="muted" style="font-size: 12px">
            У боті й в Інбоксі під кожним голосовим з’явиться кнопка «🌐 Розпізнати:
            <em>мова</em>» — той самий запис прослухається ще раз уже нею.
            <template v-if="isGoogleStt">
              Google до того ж сам обирає з них мову вже в першій спробі (максимум 3).
            </template>
          </div>
        </div>
        <div class="form-row">
          <label>Модель розпізнавання</label>
          <select v-if="sttModelOptions.length" v-model="config.speech.model">
            <option v-for="m in sttModelOptions" :key="m" :value="m">{{ m }}</option>
          </select>
          <input v-else v-model="config.speech.model" placeholder="Systran/faster-whisper-small" />
          <div v-if="isGoogleStt" class="muted" style="font-size: 12px">
            Для голосових повідомлень підходить <code>latest_long</code>;
            <code>latest_short</code> — для коротких команд.
          </div>
          <div class="toolbar" style="margin-top: 6px">
            <button
              class="secondary"
              :disabled="providerModelsLoading === config.speech.provider"
              @click="loadProviderModels(config.speech.provider)"
            >
              {{ providerModelsLoading === config.speech.provider ? 'Оновлення…' : '↻ Оновити моделі' }}
            </button>
            <span v-if="providerModelsNote[config.speech.provider]" class="muted" style="font-size: 12px">
              {{ providerModelsNote[config.speech.provider] }}
            </span>
          </div>
          <div v-if="sttModelOptions.length" class="muted" style="font-size: 12px">
            Курований набір плюс моделі транскрипції з API цього сервісу.
          </div>
          <div v-else-if="config.speech.provider === 'groq'" class="muted" style="font-size: 12px">
            Whisper large-v3 на безкоштовному тарифі Groq. <code>turbo</code> швидший,
            звичайний <code>whisper-large-v3</code> трохи точніший на числах.
          </div>
          <a v-if="sttUsageUrl" :href="sttUsageUrl" target="_blank" rel="noopener" style="font-size: 12px">
            📊 Витрати й ліміти в
            {{ sttProviders.find((p) => p.id === config.speech.provider)?.label ?? config.speech.provider }} ↗
          </a>
        </div>

        <div style="margin-bottom: 6px">
          <strong>Ключ сервісу голосу</strong>
          <span class="muted">
            для {{ sttProviders.find((p) => p.id === config.speech.provider)?.label ?? config.speech.provider }} ·
            зараз: <strong>{{ sttKeyHint ?? 'не задано' }}</strong>
          </span>
        </div>
        <div class="toolbar" style="margin-bottom: 4px">
          <PasswordInput
            v-model="sttKeyInput"
            placeholder="ключ API…"
            autocomplete="off"
            style="min-width: 240px"
          />
          <button :disabled="saving || !sttKeyInput.trim()" @click="saveSttKey(false)">Зберегти ключ</button>
          <button
            class="secondary"
            :disabled="sttKeyTesting || !(sttKeyInput.trim() || sttKeyHint)"
            @click="testSttKey"
          >
            {{ sttKeyTesting ? 'Тест…' : 'Тест' }}
          </button>
          <button class="danger" :disabled="saving || !sttKeyHint" @click="saveSttKey(true)">Видалити</button>
        </div>
        <p v-if="sttKeyTest" style="margin: 4px 0 0">
          <span class="badge" :class="sttKeyTest.ok ? 'badge-active' : 'badge-error'">
            {{ sttKeyTest.ok ? 'ключ робочий' : 'помилка' }}
          </span>
          <span class="muted" style="margin-left: 6px">{{ sttKeyTest.detail }}</span>
        </p>
        <p v-if="isGoogleStt" class="muted" style="font-size: 12px; margin: 6px 0 0">
          Потрібен ключ API проєкту Google Cloud з увімкненим Cloud Speech-to-Text API
          і активним білінгом. Синхронне розпізнавання — до 60 секунд аудіо.
        </p>
        <p class="muted" style="font-size: 12px; margin: 6px 0 0">
          Ключ зберігається окремою кнопкою й одразу. Мова та модель — кнопкою «Зберегти».
        </p>
      </div>
      <button :disabled="saving" @click="save">Зберегти</button>
    </section>
    </template>

    <template v-if="group === 'ai-prompts'">
    <h3 class="settings-group">AI · каталог промтов</h3>
    <p class="muted" style="margin-top: 0">
      Все рабочие инструкции AI собраны здесь. Изменения применяются ко всем новым
      вызовам и сохраняются отдельной версией с историей.
    </p>
    <section id="ai-prompts-editor" class="card" style="margin-bottom: 16px">
      <div class="toolbar" style="align-items: flex-start; margin-bottom: 12px">
        <div>
          <h3 style="margin: 0">{{ selectedPrompt?.title || 'Промты' }}</h3>
          <div v-if="selectedPrompt" class="muted" style="font-size: 12px; margin-top: 4px">
            версия {{ selectedPrompt.version }} · {{ selectedPrompt.task }}
          </div>
        </div>
      </div>
      <p v-if="selectedPrompt" class="muted" style="font-size: 12px">
        <strong>Для чего нужен:</strong> {{ selectedPrompt.description }}
      </p>
      <textarea v-model="promptDraft" rows="12" style="width: 100%; resize: vertical" spellcheck="false" />
      <div class="toolbar" style="margin-top: 10px">
        <input v-model="promptNote" placeholder="Комментарий к изменению (необязательно)" style="min-width: 260px; flex: 1" />
        <button :disabled="promptBusy || !promptDraft.trim()" @click="savePrompt">Сохранить новую версию</button>
      </div>
    </section>

    <section class="card">
      <h3 style="margin-top: 0">История правок</h3>
      <div v-if="!promptHistory.length" class="muted">История пока пуста.</div>
      <div v-for="revision in promptHistory" :key="revision.id" class="settings-history-row">
        <div>
          <strong>Версия {{ revision.version }}</strong>
          <span class="muted" style="margin-left: 8px">{{ fmtDateTime(revision.created_at) }}</span>
          <span v-if="revision.note" class="muted" style="margin-left: 8px">{{ revision.note }}</span>
        </div>
        <button class="secondary" @click="usePromptRevision(revision)">Загрузить в редактор</button>
      </div>
    </section>
    </template>

    <template v-if="group === 'ai-routing'">
    <h3 class="settings-group">AI · маршрути задач</h3>
    <p class="muted" style="margin-top: 0">
      Тут вирішується, <strong>куди й у якому порядку</strong> йде кожна задача. Крок —
      це пара «сервіс + модель»; перший крок, який увімкнений, налаштований і відповів,
      і використовується, решта лишаються запасними. Сервіси та їхні активні моделі
      налаштовуються на сторінці «AI · сервіси».
    </p>

    <section id="ai-routes" class="card" style="margin-bottom: 16px">
      <h3 style="margin-top: 0">Пріоритети виклику</h3>
      <div v-if="!routeProviderOptions.length" class="muted">
        Спершу увімкніть хоча б один сервіс на сторінці «AI · сервіси».
      </div>
      <div v-for="task in TASKS" :key="task.key" class="ai-route">
        <div class="ai-route-head">
          <strong>{{ task.label }}</strong>
          <span v-if="task.vision" class="badge">потрібне бачення (vision)</span>
        </div>
        <div class="muted" style="font-size: 12px; margin-bottom: 6px">{{ task.hint }}</div>
        <div v-if="!routeTargets(task.key).length" class="muted" style="font-size: 12px">
          Маршрут порожній — задача не працюватиме.
        </div>
        <div v-for="(target, i) in routeTargets(task.key)" :key="i" class="ai-route-step">
          <span class="ai-route-order">{{ i + 1 }}</span>
          <select
            :value="target.provider"
            @change="setTargetProvider(task.key, i, ($event.target as HTMLSelectElement).value)"
          >
            <option v-for="o in routeProviderOptions" :key="o.id" :value="o.id">{{ o.label }}</option>
            <option v-if="!routeProviderOptions.some((o) => o.id === target.provider)" :value="target.provider">
              {{ target.provider }} (вимкнено)
            </option>
          </select>
          <select v-model="target.model">
            <option value="">— модель за замовчуванням —</option>
            <option v-for="m in targetModelOptions(target.provider)" :key="m" :value="m">{{ m }}</option>
            <option
              v-if="target.model && !targetModelOptions(target.provider).includes(target.model)"
              :value="target.model"
            >
              {{ target.model }} (не активна)
            </option>
          </select>
          <button class="secondary" :disabled="i === 0" title="Вище" @click="moveTarget(task.key, i, -1)">↑</button>
          <button
            class="secondary"
            :disabled="i === routeTargets(task.key).length - 1"
            title="Нижче"
            @click="moveTarget(task.key, i, 1)"
          >↓</button>
          <button class="danger" title="Прибрати крок" @click="removeTarget(task.key, i)">✕</button>
          <span v-if="task.vision && target.provider === 'local'" class="muted" style="font-size: 12px">
            локальний бекенд не читає зображення — крок пропускається
          </span>
        </div>
        <button class="secondary" :disabled="!routeProviderOptions.length" @click="addTarget(task.key)">
          Додати крок
        </button>
      </div>

      <div class="toolbar" style="margin-top: 12px">
        <button :disabled="saving" @click="save">Зберегти</button>
        <button class="secondary" :disabled="!routeProviderOptions.length" @click="autofillRouting">
          Заповнити типово
        </button>
        <button class="secondary" :disabled="checking" @click="runCheck">
          {{ checking ? 'Перевірка…' : 'Перевірити зʼєднання' }}
        </button>
      </div>
      <div v-if="check" style="display: flex; gap: 20px; flex-wrap: wrap; margin-top: 8px">
        <div>
          <span class="badge" :class="check.local.ok ? 'badge-active' : 'badge-error'">локальна</span>
          <span class="muted" style="margin-left: 6px">{{ check.local.detail }}</span>
        </div>
        <div v-for="(result, pid) in check.providers" :key="pid">
          <span class="badge" :class="result.ok ? 'badge-active' : 'badge-error'">{{ providerLabel(pid) }}</span>
          <span class="muted" style="margin-left: 6px">{{ result.detail }}</span>
        </div>
      </div>
    </section>

    <section id="ai-budget" class="card" style="margin-bottom: 16px">
      <h3 style="margin-top: 0">Обмеження витрат</h3>
      <div class="form-row" style="max-width: 620px">
        <label>Місячний бюджет токенів хмари (0 — без ліміту)</label>
        <input type="number" min="0" step="100000" v-model.number="config.monthly_token_budget" />
        <div class="muted" style="font-size: 12px">
          Коли ліміт вичерпано, всі платні кроки пропускаються до кінця місяця — лишається
          тільки локальний бекенд.
        </div>
      </div>
      <button :disabled="saving" @click="save">Зберегти</button>
    </section>

    <section v-if="usage" id="ai-usage" class="card" style="margin-bottom: 16px">
      <div class="toolbar">
        <h3 style="margin: 0; flex: 1">Використання AI · {{ usage.month }}</h3>
        <button class="secondary" :disabled="logLoading" @click="loadLog">
          {{ logLoading ? 'Оновлення…' : 'Оновити журнал' }}
        </button>
      </div>
      <div class="card-grid" style="margin-bottom: 0">
        <div>
          <div class="stat">{{ (usage.cloud_tokens_in + usage.cloud_tokens_out).toLocaleString('uk-UA') }}</div>
          <div class="stat-label">токенів хмари · {{ usage.cloud_calls }} запитів<span v-if="usage.cloud_errors" style="color: var(--danger)"> · {{ usage.cloud_errors }} помилок</span></div>
        </div>
        <div>
          <div class="stat">{{ (usage.local_tokens_in + usage.local_tokens_out).toLocaleString('uk-UA') }}</div>
          <div class="stat-label">токенів локально · {{ usage.local_calls }} запитів<span v-if="usage.local_errors" style="color: var(--danger)"> · {{ usage.local_errors }} помилок</span></div>
        </div>
        <div>
          <div class="stat">{{ usage.cloud_cached_tokens.toLocaleString('uk-UA') }}</div>
          <div class="stat-label">вхідних токенів отримано з кешу</div>
        </div>
        <div>
          <div class="stat">
            {{ config.monthly_token_budget ? Math.max(0, config.monthly_token_budget - usage.cloud_tokens_in - usage.cloud_tokens_out).toLocaleString('uk-UA') : '∞' }}
          </div>
          <div class="stat-label">залишок бюджету хмари</div>
        </div>
      </div>
      <p class="muted" style="font-size: 12px; margin: 8px 0 0">
        Тут рахується лише те, що витратив цей застосунок. Реальний рахунок і
        безкоштовні ліміти — у консолях сервісів (посилання «витрати» на сторінці
        «AI · сервіси»). Кешовані токени входять у загальну кількість, але сервіс
        зазвичай тарифікує їх значно дешевше.
      </p>

      <h4 style="margin: 18px 0 6px">Журнал AI</h4>
      <div class="muted" style="font-size: 12px; margin-bottom: 6px">
        Натисніть рядок, щоб побачити що надіслали й що відповіла модель.
      </div>
      <div v-if="!log.length" class="muted">Записів ще немає.</div>
      <div v-else style="overflow-x: auto">
        <table>
          <thead>
            <tr><th>Час</th><th>Задача</th><th>Бекенд · модель</th><th>Токени</th><th>Час</th><th>Результат</th></tr>
          </thead>
          <tbody>
            <template v-for="[i, e] in pagedLog" :key="i">
              <tr style="cursor: pointer" @click="toggleLog(i)">
                <td class="muted" style="white-space: nowrap">{{ expanded.has(i) ? '▾ ' : '▸ ' }}{{ fmtLogTime(e.ts) }}</td>
                <td>{{ e.task }}</td>
                <td>{{ e.backend || '—' }}<span v-if="e.model" class="muted"> · {{ e.model }}</span></td>
                <td style="white-space: nowrap">
                  {{ e.tokens_in || e.tokens_out ? `${e.tokens_in}/${e.tokens_out}` : '—' }}
                  <span v-if="e.cached_tokens" class="muted"> · кеш {{ e.cached_tokens }}</span>
                </td>
                <td style="white-space: nowrap">{{ e.duration_ms ? (e.duration_ms / 1000).toFixed(1) + ' с' : '—' }}</td>
                <td :style="e.ok ? {} : { color: 'var(--danger)' }">{{ e.error || e.note || (e.ok ? 'ok' : 'помилка') }}</td>
              </tr>
              <tr v-if="expanded.has(i)">
                <td colspan="6" style="background: var(--bg)">
                  <div class="muted" style="font-size: 12px; margin-bottom: 2px">Що обробляли</div>
                  <pre style="white-space: pre-wrap; word-break: break-word; margin: 0 0 10px; max-height: 180px; overflow: auto">{{ e.prompt || '—' }}</pre>
                  <div class="muted" style="font-size: 12px; margin-bottom: 2px">Відповідь моделі</div>
                  <pre style="white-space: pre-wrap; word-break: break-word; margin: 0; max-height: 260px; overflow: auto">{{ e.response || e.error || '—' }}</pre>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
        <div v-if="logPageCount > 1" style="display: flex; gap: 8px; align-items: center; margin-top: 8px">
          <button class="secondary" :disabled="logPage === 0" @click="setLogPage(logPage - 1)">← Новіші</button>
          <span class="muted" style="font-size: 12px">Сторінка {{ logPage + 1 }} з {{ logPageCount }}</span>
          <button class="secondary" :disabled="logPage >= logPageCount - 1" @click="setLogPage(logPage + 1)">Старіші →</button>
        </div>
      </div>
    </section>
    </template>

    <template v-if="group === 'integrations'">
    <h3 class="settings-group">Інтеграції та доступ</h3>
    <section id="telegram" class="card" style="margin-bottom: 16px">
      <h3 style="margin-top: 0">Telegram-бот</h3>
      <p class="muted">Приймає фото/QR і публічні посилання на чеки, перевіряє дублікати та додає покупки. Команди <code>/find</code>, <code>/spending</code>, <code>/receipts</code> і <code>/add</code> використовують ті самі функції, що й MCP.</p>
      <label style="display:flex; gap:8px; align-items:center; margin-bottom:12px">
        <input v-model="telegramForm.enabled" type="checkbox" style="width:auto" />
        <strong>Увімкнути бота</strong>
      </label>
      <label style="display:flex; gap:8px; align-items:center; margin-bottom:12px">
        <input v-model="telegramForm.capture_enabled" type="checkbox" style="width:auto" />
        <span>Розбирати вільний текст і голосові через AI («додай чек …», «додай в список покупок …»)</span>
      </label>
      <div class="form-row"><label>Токен від @BotFather</label><PasswordInput v-model="telegramForm.bot_token" :placeholder="telegram?.token_hint || '123456:ABC…'" autocomplete="off" /></div>
      <p class="muted">Після збереження користувач надсилає боту <code>/start</code>. Його заявка зʼявиться нижче — доступ надається лише після ручного підтвердження. Токен зберігається зашифрованим.</p>
      <div class="form-row">
        <label>Адреса застосунку для посилань</label>
        <input v-model="telegramForm.public_url" placeholder="http://192.168.1.50:8001" />
      </div>
      <p class="muted">
        Звідси беруться посилання на чеки в повідомленнях бота. Телефон відкриває їх сам,
        тому <code>localhost</code> не підходить — вкажіть IP у мережі або зовнішній домен.
        Порожньо — використати адресу бекенду; якщо й вона локальна, посилання не додаються.
      </p>
      <div style="margin-bottom:16px">
        <div class="toolbar"><strong style="flex:1">Очікують підтвердження</strong><button class="secondary" @click="refreshTelegram">Оновити заявки</button></div>
        <p v-if="!telegram?.config.pending_chats.length" class="muted">Нових заявок немає.</p>
        <div v-for="request in (telegram?.config.pending_chats || [])" :key="request.chat_id" class="toolbar" style="margin-top:8px">
          <span style="flex:1">
            <strong>{{ request.display_name || request.username || request.chat_id }}</strong>
            <span v-if="request.username" class="muted"> · @{{ request.username }}</span>
            <small class="muted" style="display:block">Chat ID: {{ request.chat_id }}</small>
          </span>
          <button @click="approveTelegramChat(request.chat_id)">Дозволити</button>
          <button class="secondary" @click="rejectTelegramChat(request.chat_id)">Відхилити</button>
        </div>
      </div>
      <div v-if="telegram?.config.authorized_chats.length" style="margin-bottom:12px">
        <strong>Авторизовані чати</strong>
        <div v-for="chat in telegram.config.authorized_chats" :key="chat.chat_id" class="toolbar" style="margin-top:8px">
          <span style="flex:1">
            <strong>{{ chat.display_name || chat.username || `Чат ${chat.chat_id}` }}</strong>
            <span v-if="chat.username" class="muted"> · @{{ chat.username }}</span>
            <small class="muted" style="display:block">
              Chat ID: {{ chat.chat_id }}<template v-if="chat.approved_at"> · дозволено {{ new Date(chat.approved_at).toLocaleString('uk-UA') }}</template>
            </small>
          </span>
          <button class="secondary" @click="revokeTelegramChat(chat.chat_id)">Відкликати</button>
        </div>
      </div>
      <div class="toolbar"><button :disabled="telegramBusy" @click="saveTelegram">{{ telegramBusy ? 'Перевірка…' : 'Зберегти й перевірити' }}</button><span v-if="telegramSavedAt" class="muted">збережено {{ telegramSavedAt }}</span></div>
    </section>

    <section id="telegram-log" class="card" style="margin-bottom: 16px">
      <div class="toolbar">
        <div style="flex: 1">
          <h3 style="margin: 0">Журнал Telegram</h3>
          <div class="muted" style="font-size: 12px">Вхідні повідомлення, відправники та гілки розмови · {{ telegramMessageTotal }}</div>
        </div>
        <select v-model="telegramConversation">
          <option value="">Усі гілки</option>
          <option v-for="[key, label] in telegramConversations" :key="key" :value="key">{{ label }}</option>
        </select>
        <button class="secondary" :disabled="telegramLogLoading" @click="refreshTelegramMessages">
          {{ telegramLogLoading ? 'Оновлення…' : 'Оновити' }}
        </button>
      </div>
      <p v-if="!visibleTelegramMessages.length" class="muted">Повідомлень у цій гілці ще немає. Нові повідомлення зʼявляться тут після отримання ботом.</p>
      <div v-else style="overflow-x: auto">
        <table>
          <thead>
            <tr><th>Час</th><th>Від кого</th><th>Чат · гілка</th><th>Тип</th><th>Повідомлення</th><th>Обробка</th></tr>
          </thead>
          <tbody>
            <tr v-for="message in visibleTelegramMessages" :key="message.id">
              <td class="muted" style="white-space: nowrap">{{ fmtDateTime(message.received_at) }}</td>
              <td>
                <strong>{{ message.sender_display_name || message.sender_username || message.sender_id || 'невідомо' }}</strong>
                <div v-if="message.sender_username" class="muted" style="font-size: 12px">@{{ message.sender_username }}</div>
              </td>
              <td style="white-space: nowrap">
                {{ message.chat_title || message.chat_id }}
                <div class="muted" style="font-size: 12px">
                  {{ message.message_thread_id ? `тема ${message.message_thread_id}` : `гілка ${message.root_message_id}` }}
                  <template v-if="message.reply_to_message_id"> · відповідь на {{ message.reply_to_message_id }}</template>
                </div>
              </td>
              <td>{{ message.content_type }}</td>
              <td style="min-width: 220px; max-width: 440px; white-space: normal; word-break: break-word">
                {{ message.text || '—' }}
              </td>
              <td>
                <span class="badge" :class="['error', 'rejected'].includes(message.status) ? 'badge-error' : 'badge-active'">{{ message.status }}</span>
                <div v-if="!message.authorized" class="muted" style="font-size: 12px">без доступу</div>
                <details v-if="message.outcome || message.error" style="margin-top: 5px; max-width: 360px">
                  <summary class="muted" style="cursor: pointer">результат</summary>
                  <div style="white-space: pre-wrap; word-break: break-word; margin-top: 4px">{{ message.error || message.outcome }}</div>
                </details>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="filteredTelegramMessages.length > telegramLogPageSize" class="toolbar" style="margin-top: 12px">
        <button class="secondary" :disabled="telegramLogPage === 0" @click="telegramLogPage--">← Назад</button>
        <span class="muted">
          {{ telegramLogPage * telegramLogPageSize + 1 }}–{{ Math.min((telegramLogPage + 1) * telegramLogPageSize, filteredTelegramMessages.length) }}
          з {{ filteredTelegramMessages.length }}
        </span>
        <button class="secondary" :disabled="(telegramLogPage + 1) * telegramLogPageSize >= filteredTelegramMessages.length" @click="telegramLogPage++">Далі →</button>
      </div>
    </section>

    <section id="mcp" class="card" style="margin-bottom: 16px">
      <h3 style="margin-top: 0">MCP-сервер</h3>
      <p class="muted" style="margin-top: -4px">
        Віддає дані застосунку зовнішньому AI (Claude Desktop тощо) по Model Context
        Protocol. Тільки LAN, без окремої авторизації. Адреса:
        <code>{{ mcpUrl }}</code> (Streamable HTTP).
      </p>

      <label style="display: flex; gap: 8px; align-items: center; margin-bottom: 10px">
        <input type="checkbox" v-model="mcpCfg.enabled" style="width: auto" />
        <strong>Увімкнути MCP-сервер</strong>
      </label>
      <p class="muted" style="margin-top: -6px">
        Позначте інструменти, доступні зовнішньому клієнту. Інструменти, що
        змінюють дані, позначені окремо — вмикайте їх свідомо.
      </p>
      <h4 style="margin-bottom: 8px">Інструменти</h4>
      <ul style="list-style: none; padding: 0; margin: 0">
        <li v-for="t in mcpTools" :key="t.name" style="padding: 4px 0">
          <label style="display: flex; gap: 8px; align-items: baseline">
            <input
              type="checkbox"
              :checked="mcpCfg.tools[t.name] && mcpCfg.tools[t.name] !== 'off'"
              :disabled="!mcpCfg.enabled"
              style="width: auto"
              @change="mcpCfg.tools[t.name] = ($event.target as HTMLInputElement).checked ? (t.kind === 'write' ? 'write' : 'read') : 'off'"
            />
            <span>
              <code>{{ t.name }}</code>
              <span
                v-if="t.kind === 'write'"
                class="badge badge-error"
                style="margin-left: 6px"
              >змінює дані</span>
              <div class="muted" style="font-size: 12px">{{ t.label }}</div>
            </span>
          </label>
        </li>
      </ul>

      <div class="toolbar" style="margin-top: 10px">
        <button :disabled="saving" @click="saveMcp">Зберегти</button>
        <span v-if="mcpSavedAt" class="muted">збережено {{ mcpSavedAt }}</span>
      </div>
    </section>
    </template>

    <template v-if="group === 'scanner'">
    <h3 class="settings-group">Сканування та розпізнавання</h3>
    <section id="inbox-routing" class="card" style="margin-bottom: 16px">
      <h3 style="margin-top: 0">Інбокс · роутинг завантажень</h3>
      <p class="muted" style="margin-top: -4px">
        Коли ви завантажуєте фото в розділі <RouterLink to="/inbox">Інбокс</RouterLink>,
        застосунок вирішує: це <strong>чек</strong> (іде в конвеєр розпізнавання —
        QR&nbsp;→&nbsp;шаблон&nbsp;→&nbsp;OCR&nbsp;→&nbsp;AI) чи <strong>особистий
        документ</strong> (лягає в чергу на Google Drive і <em>ніколи</em> не
        потрапляє в зовнішню модель). Рішення: спершу колір, потім форма.
      </p>

      <div class="form-row">
        <label>Поріг форми, h/w</label>
        <input type="number" step="0.1" min="1" v-model.number="inboxCfg.receipt_ratio" />
      </div>
      <p class="muted" style="margin-top: -6px">
        Зображення, у якого висота ≥ цього значення від ширини (за замовч. 2.0 —
        вдвічі довше за ширину), вважається чеком. Нижче — документ. Паперовий чек
        — вузька довга стрічка; договір/паспорт ближчі до формату А4.
      </p>

      <div class="form-row">
        <label>Макс. насиченість «монохромного»</label>
        <input type="number" step="0.01" min="0" max="1" v-model.number="inboxCfg.color_max_saturation" />
      </div>
      <div class="form-row">
        <label>Макс. частка кольорових пікселів</label>
        <input type="number" step="0.01" min="0" max="1" v-model.number="inboxCfg.color_max_nongray_fraction" />
      </div>
      <p class="muted" style="margin-top: -6px">
        Якщо середня насиченість <em>і</em> частка помітно кольорових пікселів обидві
        нижче цих порогів — зображення вважається монохромним і йде в чеки
        <strong>незалежно від форми</strong> (сірий термодрук так виглядає завжди).
        Це відсіює хибне спрацювання на картках-документах (права, ID) — вони
        вузькі як чек, але кольорові. Більші значення = більше зображень
        трактується як чек.
      </p>

      <div class="form-row">
        <label>Ліміт розміру файлу, МБ</label>
        <input type="number" step="1" min="1" v-model.number="inboxCfg.max_file_mb" />
      </div>
      <p class="muted" style="margin-top: -6px">
        Завантаження більші за це відхиляються з помилкою (жорсткий стоп — 40&nbsp;МБ).
      </p>

      <button :disabled="saving" @click="saveInboxCfg">Зберегти</button>
      <span v-if="inboxSavedAt" class="muted" style="margin-left: 8px">збережено {{ inboxSavedAt }}</span>
    </section>

    <section id="tax" class="card" style="margin-bottom: 16px">
      <h3 style="margin-top: 0">ДПС · фіскальні чеки</h3>
      <p class="muted" style="margin-top: -4px">
        Токен API реєстрів відкритої частини Електронного кабінету. Він потрібен,
        щоб за реквізитами QR завантажувати оригінальний XML чека з товарними
        позиціями без CAPTCHA. Створюється в
        <a href="https://cabinet.tax.gov.ua/user/settings" target="_blank" rel="noopener">
          Налаштування → Токени відкритої частини
        </a>.
      </p>

      <div class="form-row">
        <label>Токен відкритої частини</label>
        <PasswordInput
          v-model="taxTokenInput"
          autocomplete="off"
          :placeholder="taxCabinet?.token_hint || 'Вставте токен ДПС'"
        />
      </div>
      <p class="muted" style="margin-top: -6px">
        Зберігається в базі у зашифрованому вигляді. Порожнє поле не змінює вже
        збережений токен. ДПС анулює токен після більш ніж 1000 запитів за добу.
      </p>
      <p v-if="taxCabinet?.api_guard" class="muted">
        Запитів API сьогодні: {{ taxCabinet.api_guard.count || 0 }} / 900 (100 залишаємо в резерві).
        <template v-if="taxCabinet.api_guard.blocked_until"><br><strong>Запобіжник активний до {{ fmtDateTime(taxCabinet.api_guard.blocked_until) }}.</strong> Нові API-запити не надсилаються.</template>
      </p>

      <div class="toolbar">
        <button :disabled="taxBusy || !taxTokenInput.trim()" @click="saveTaxToken(false)">
          {{ taxBusy ? 'Збереження…' : 'Зберегти токен' }}
        </button>
        <button
          v-if="taxCabinet?.configured"
          class="secondary"
          :disabled="taxBusy"
          @click="saveTaxToken(true)"
        >Видалити</button>
        <span v-if="taxCabinet?.configured" class="muted">
          налаштовано {{ taxCabinet.token_hint }}
        </span>
        <span v-if="taxSavedAt" class="muted">збережено {{ taxSavedAt }}</span>
      </div>
      <h4>Черга та історія чеків</h4>
      <div class="toolbar">
        <input v-model="fiscalUrl" style="min-width: min(620px, 100%)" placeholder="Посилання cabinet.tax.gov.ua/cashregs/check…" />
        <button :disabled="!fiscalUrl.trim()" @click="addFiscalJob">Додати в чергу</button>
      </div>
      <div v-if="fiscalJobs.length" style="overflow-x:auto;margin-top:12px">
        <table><thead><tr><th>Створено</th><th>Джерело</th><th>Статус</th><th>Спроби</th><th></th></tr></thead>
        <tbody><tr v-for="j in fiscalJobs" :key="j.id">
          <td>{{ fmtDateTime(j.created_at) }}</td><td><a :href="j.url" target="_blank">{{ j.source }}</a></td><td>{{ j.status }}<br><small v-if="j.error">{{ j.error }}</small></td><td>{{ j.attempts }}</td>
          <td>
            <RouterLink v-if="j.receipt_id" :to="`/receipts/${j.receipt_id}`">Відкрити чек →</RouterLink>
            <template v-else>
              <button :disabled="fiscalBrowserBusy" @click="runFiscalPublicApi(j)">API ДПС</button>
              <button v-if="j.status==='captcha'" class="secondary" :disabled="fiscalBrowserBusy" @click="openFiscalCaptcha(j)">CAPTCHA</button>
            </template>
          </td>
        </tr></tbody></table>
      </div>
      <div v-if="fiscalBrowserJob" class="card" style="margin-top:12px">
        <p>ДПС потребує CAPTCHA. Натисніть потрібні елементи безпосередньо на зображенні браузера.</p>
        <p class="muted">Екран оновлюється автоматично кожні 2,5 секунди. Після успішної відповіді чек імпортується без додаткового натискання.</p>
        <p v-if="fiscalBrowserState?.error_code" class="error">
          Cloudflare відхилив перевірку (код {{ fiscalBrowserState.error_code }}). Перезавантажте перевірку; якщо помилка повторюється — спробуйте пізніше або з іншої мережі.
        </p>
        <img :src="fiscalShot" @click="fiscalClick" style="display:block;max-width:100%;cursor:crosshair;border:1px solid #bbb" />
        <div class="toolbar" style="margin-top:8px">
          <button @click="checkFiscalBrowser">Перевірити та імпортувати</button>
          <button class="secondary" @click="refreshFiscalShot">Оновити екран</button>
          <button class="secondary" :disabled="fiscalBrowserBusy" @click="reloadFiscalBrowser">Перезавантажити CAPTCHA</button>
          <button class="secondary" :disabled="fiscalBrowserBusy" @click="restartFiscalBrowser">Новий сеанс Chrome</button>
        </div>
      </div>
      <div class="card" style="margin-top:12px">
        <h4 style="margin-top:0">Звичайний браузер</h4>
        <p class="muted">Відкрийте чек у своєму Chrome, пройдіть перевірку та збережіть XML зі сторінки. Його можна завантажити файлом або вставити текстом.</p>
        <div class="toolbar" style="margin-bottom:12px">
          <input type="file" accept=".xml,.txt,text/xml,application/xml,text/plain" @change="selectFiscalResponseFile" />
          <button :disabled="fiscalBrowserBusy||!fiscalResponseFile" @click="importFiscalResponseFile">Завантажити XML-файл</button>
        </div>
        <details>
          <summary style="cursor:pointer">Або вставити XML текстом</summary>
          <textarea v-model="fiscalWebResponse" rows="7" style="width:100%;margin-top:8px" placeholder="<Chk>...</Chk>"></textarea>
          <button :disabled="fiscalBrowserBusy||!fiscalWebResponse.trim()" @click="importFiscalWebResponse">Імпортувати XML-текст</button>
        </details>
      </div>
    </section>
    </template>

    <template v-if="group === 'shopping'">
    <h3 class="settings-group">Список покупок</h3>
    <section id="shopping-auto" class="card">
      <h3 style="margin-top: 0">Автоматична класифікація</h3>
      <p class="muted" style="margin-top: -4px">
        Позиції без категорії розкладаються самі: спочатку за словниками й історією
        чеків, потім — через AI. Пауза рахується від останнього доданого товару,
        тож серія додавань обробляється один раз. Кожен запуск потрапляє в
        <RouterLink to="/logs">журнал</RouterLink>.
      </p>
      <div class="form-row">
        <label>Вмикати автоматично</label>
        <input v-model="shopping.auto_categorize" type="checkbox" />
      </div>
      <div class="form-row">
        <label>Пауза після останнього додавання, хв</label>
        <input
          v-model.number="shopping.auto_categorize_delay_minutes"
          type="number"
          min="1"
          max="1440"
          style="width: 110px"
          :disabled="!shopping.auto_categorize"
        />
      </div>
      <button :disabled="shoppingBusy" @click="saveShopping">Зберегти</button>
      <span v-if="shoppingSaved" class="muted" style="margin-left: 8px">{{ shoppingSaved }}</span>
    </section>
    </template>

    <template v-if="group === 'storage'">
    <h3 class="settings-group">Зберігання документів</h3>
    <section id="drive" class="card">
      <h3 style="margin-top: 0">Google Drive · інбокс документів</h3>

      <p class="muted" style="margin-top: -4px">
        Особисті документи зберігаються тут. Доки Drive не під'єднано — лягають
        локально в <code>data/inbox/</code>; після під'єднання завантажуються одразу
        в обрану папку «Необроблені» і локально не тримаються.
      </p>

      <template v-if="drive">
        <!-- OAuth client credentials -->
        <div class="form-row">
          <label>Google Client ID</label>
          <input
            v-model="clientForm.client_id"
            placeholder="…apps.googleusercontent.com"
            autocomplete="off"
          />
        </div>
        <div class="form-row">
          <label>Google Client secret</label>
          <PasswordInput
            v-model="clientForm.client_secret"
            autocomplete="off"
            :placeholder="drive.client_secret_masked || 'GOCSPX-…'"
          />
        </div>
        <p class="muted" style="margin-top: -6px">
          Джерело:
          <strong v-if="drive.client_source === 'settings'">задано тут</strong>
          <strong v-else-if="drive.client_source === 'env'">зі змінних оточення / опцій add-on</strong>
          <strong v-else>не задано</strong>.
          Redirect URI для Google Cloud Console: <code>{{ drive.redirect_uri }}</code>
        </p>
        <button :disabled="driveBusy === 'client'" @click="saveDriveClient">Зберегти креденшели</button>

        <details style="margin: 12px 0">
          <summary style="cursor: pointer">Як отримати Client ID / secret →</summary>
          <ol class="muted" style="margin: 8px 0 0; padding-left: 20px; line-height: 1.5">
            <li><a href="https://console.cloud.google.com/" target="_blank">console.cloud.google.com</a> → створіть проєкт.</li>
            <li><strong>APIs &amp; Services → Library</strong> → увімкніть <strong>Google Drive API</strong>.</li>
            <li>
              <strong>OAuth consent screen</strong>: тип <em>External</em>, заповніть назву й email;
              у <strong>Test users</strong> додайте свій Google-акаунт (публікувати застосунок не треба).
            </li>
            <li>
              <strong>Credentials → Create credentials → OAuth client ID</strong> → тип
              <em>Web application</em>. У <strong>Authorized redirect URIs</strong> вставте точно:
              <code>{{ drive.redirect_uri }}</code>
            </li>
            <li>Скопіюйте <strong>Client ID</strong> і <strong>Client secret</strong> у поля вище й збережіть.</li>
            <li>Далі — «Підключити Google Drive» і згода в вікні Google.</li>
          </ol>
          <p class="muted" style="margin-top: 6px">
            Альтернатива полям вище — змінні <code>RS_GOOGLE_CLIENT_ID</code> /
            <code>RS_GOOGLE_CLIENT_SECRET</code> у <code>backend/.env</code> або опції add-on.
            Повна інструкція — DOCS.md.
          </p>
        </details>

        <hr style="border: none; border-top: 1px solid var(--border, #333); margin: 12px 0" />

        <p v-if="!drive.configured" class="muted">
          Спочатку вкажіть Client ID та secret вище — тоді з'явиться кнопка підключення.
        </p>
        <div v-if="drive.configured && !drive.connected">
          <button :disabled="driveBusy === 'connect'" @click="connectDrive">
            Підключити Google Drive
          </button>
          <button class="secondary" style="margin-left: 8px" @click="loadDrive">Я підключив — оновити</button>
          <p class="muted">
            Відкриється вікно згоди Google. Після підтвердження поверніться сюди й
            натисніть «оновити».
          </p>
        </div>

        <div v-else-if="drive.connected">
          <p class="stat" style="margin-bottom: 6px">
            Підключено ✅
            <button class="secondary" style="margin-left: 8px" :disabled="driveBusy === 'disconnect'" @click="disconnectDrive">
              Відключити
            </button>
          </p>

          <table style="margin-bottom: 8px">
            <tbody>
              <tr>
                <td>Коренева папка</td>
                <td>
                  <span
                    v-if="drive.root_folder_id"
                    class="folder-dot"
                    :style="{ background: drive.root_folder_color || '#c2c2c2' }"
                    title="Змінити колір"
                    @click="colorPickerFor = colorPickerFor === drive.root_folder_id ? null : drive.root_folder_id"
                  />
                  <strong>{{ drive.root_folder_name || drive.root_folder_id || 'уся моя Google Drive' }}</strong>
                  <button v-if="drive.root_folder_id" class="secondary" style="margin-left: 8px" @click="clearFolder('root')">✕</button>
                  <span v-if="drive.root_folder_id && colorPickerFor === drive.root_folder_id" class="palette">
                    <span
                      v-for="c in palette" :key="c" class="folder-dot"
                      :style="{ background: c }" @click="setFolderColor(drive.root_folder_id, c)"
                    />
                  </span>
                </td>
              </tr>
              <tr>
                <td>Папка «Необроблені»</td>
                <td>
                  <span
                    v-if="drive.inbox_folder_id"
                    class="folder-dot"
                    :style="{ background: drive.inbox_folder_color || '#c2c2c2' }"
                    title="Змінити колір"
                    @click="colorPickerFor = colorPickerFor === drive.inbox_folder_id ? null : drive.inbox_folder_id"
                  />
                  <strong>{{ drive.inbox_folder_name || drive.inbox_folder_id || 'не обрана — беремо кореневу' }}</strong>
                  <button v-if="drive.inbox_folder_id" class="secondary" style="margin-left: 8px" @click="clearFolder('inbox')">✕</button>
                  <span v-if="drive.inbox_folder_id && colorPickerFor === drive.inbox_folder_id" class="palette">
                    <span
                      v-for="c in palette" :key="c" class="folder-dot"
                      :style="{ background: c }" @click="setFolderColor(drive.inbox_folder_id, c)"
                    />
                  </span>
                </td>
              </tr>
            </tbody>
          </table>
          <p class="muted" style="margin-top: -4px">
            <strong>Коренева</strong> — база для всіх документів (звідси починається
            огляд і тут створюються нові папки). <strong>«Необроблені»</strong> —
            куди падають свіжі завантаження до обробки; якщо не задана, беремо кореневу.
            Колір біля назви — той самий, що й у самій Google Drive.
          </p>

          <div class="card" style="margin-top: 10px">
            <div class="muted" style="margin-bottom: 6px">
              <span v-for="(f, i) in folderStack" :key="i">
                <a href="#" @click.prevent="openFolder(f, i)">{{ f.name }}</a>
                <span v-if="i < folderStack.length - 1"> / </span>
              </span>
            </div>
            <ul style="list-style: none; padding: 0; margin: 0">
              <li v-for="f in folders" :key="f.id" style="padding: 3px 0">
                <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap">
                  <span class="folder-dot" :style="{ background: f.color || '#c2c2c2' }" />
                  <a href="#" @click.prevent="openFolder({ id: f.id, name: f.name }, folderStack.length - 1)">{{ f.name }}</a>
                  <span style="margin-left: auto; display: flex; gap: 6px">
                    <button class="secondary" @click="colorPickerFor = colorPickerFor === f.id ? null : f.id">Колір</button>
                    <button class="secondary" @click="pickFolder('root', f)">Коренева</button>
                    <button class="secondary" @click="pickFolder('inbox', f)">«Необроблені»</button>
                  </span>
                </div>
                <span v-if="colorPickerFor === f.id" class="palette">
                  <span
                    v-for="c in palette" :key="c" class="folder-dot"
                    :style="{ background: c }" @click="setFolderColor(f.id, c)"
                  />
                </span>
              </li>
              <li v-if="!folders.length" class="muted">Порожньо</li>
            </ul>
            <div class="form-row" style="margin-top: 8px; align-items: center">
              <input v-model="newFolderName" placeholder="Нова папка тут…" style="flex: 1" />
              <span class="palette" style="margin: 0 6px">
                <span
                  v-for="c in palette" :key="c" class="folder-dot"
                  :style="{ background: c, outline: newFolderColor === c ? '2px solid var(--accent, #6366f1)' : 'none' }"
                  @click="newFolderColor = newFolderColor === c ? null : c"
                />
              </span>
              <button class="secondary" @click="makeFolder">Створити</button>
            </div>
          </div>
        </div>
      </template>
    </section>
    </template>
    </div>
  </div>
</template>

<style scoped>
.settings-toast {
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
  box-shadow: 0 12px 36px rgba(0, 0, 0, .35);
}
.settings-toast.is-success { border-left-color: #3fb950; }
.settings-toast span { flex: 1; line-height: 1.4; overflow-wrap: anywhere; }
.settings-toast button {
  padding: 0;
  border: 0;
  background: transparent;
  color: inherit;
  font-size: 22px;
  line-height: 1;
}
.settings-toast-enter-active, .settings-toast-leave-active { transition: opacity .18s ease, transform .18s ease; }
.settings-toast-enter-from, .settings-toast-leave-to { opacity: 0; transform: translateY(-8px); }
.settings-head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 12px;
}

/* Two-column layout: a sticky navigation tree on the left, settings cards on
   the right. Collapses to a stacked layout on narrow viewports. */
.settings-layout {
  display: flex;
  align-items: flex-start;
  gap: 28px;
}
.settings-main {
  flex: 1;
  min-width: 0;
}
.settings-history-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 0;
  border-top: 1px solid var(--border);
}

.settings-side {
  flex-shrink: 0;
  width: 210px;
  position: sticky;
  top: 16px;
  align-self: flex-start;
  max-height: calc(100vh - 32px);
  overflow-y: auto;
}
.settings-tree {
  display: flex;
  flex-direction: column;
  gap: 2px;
  border-left: 1px solid var(--border);
}
.settings-tree-group {
  display: flex;
  flex-direction: column;
  gap: 1px;
  padding-bottom: 8px;
}
.settings-tree-link {
  background: none;
  border: none;
  border-left: 2px solid transparent;
  border-radius: 0;
  text-align: left;
  color: var(--text-dim);
  font-size: 13px;
  padding: 6px 10px;
  margin-left: -1px;
  cursor: pointer;
}
.settings-tree-link:hover {
  color: var(--text);
  background: var(--bg-hover);
  filter: none;
}
.settings-tree-link.is-group {
  color: var(--text);
  font-weight: 600;
  text-transform: uppercase;
  font-size: 11px;
  letter-spacing: 0.05em;
}
.settings-tree-link.is-child {
  padding-left: 22px;
}
.settings-tree-link.is-active {
  color: var(--accent);
  border-left-color: var(--accent);
  background: var(--bg-hover);
}

@media (max-width: 820px) {
  .settings-layout {
    flex-direction: column;
  }
  .settings-side {
    position: static;
    width: 100%;
    max-height: none;
  }
  .settings-tree {
    flex-direction: row;
    flex-wrap: wrap;
    border-left: none;
  }
  .settings-tree-group {
    padding-bottom: 0;
  }
  .settings-tree-link.is-child {
    padding-left: 10px;
  }
}

section[id] {
  scroll-margin-top: 20px;
}

.settings-group {
  scroll-margin-top: 20px;
  margin: 28px 0 12px;
  padding-bottom: 6px;
  font-size: 13px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-dim);
  border-bottom: 1px solid var(--border);
}
.settings-group:first-of-type {
  margin-top: 8px;
}

/* One card per configured AI service (Settings block 1). */
.ai-provider {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px 12px;
  margin-bottom: 10px;
}
.ai-provider-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.ai-provider-body {
  border-top: 1px solid var(--border);
  margin-top: 10px;
  padding-top: 10px;
}
.ai-model-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 2px 12px;
  max-height: 220px;
  overflow: auto;
}
.ai-model {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}
.ai-provider-models {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 6px;
  margin-top: 8px;
}
.ai-model-chip {
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 1px 8px;
  font-size: 12px;
  color: var(--muted);
  white-space: nowrap;
}

/* One task's priority list (Settings block 2). */
.ai-route {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px 12px;
  margin-bottom: 10px;
}
.ai-route-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.ai-route-step {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}
.ai-route-step select {
  min-width: 180px;
  width: auto;
}
.ai-route-order {
  width: 20px;
  color: var(--text-dim);
  font-size: 12px;
}

.folder-dot {
  display: inline-block;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  border: 1px solid rgba(0, 0, 0, 0.25);
  vertical-align: middle;
  margin-right: 6px;
  cursor: pointer;
}
.palette {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 4px;
  padding: 4px 6px;
  margin-left: 6px;
  border: 1px solid var(--border, #333);
  border-radius: 6px;
}
.palette .folder-dot {
  margin: 0;
}
</style>
