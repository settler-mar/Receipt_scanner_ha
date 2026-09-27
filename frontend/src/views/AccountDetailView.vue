<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/lib/api'
import LoyaltyCodeDisplay from '@/components/LoyaltyCodeDisplay.vue'
import PasswordInput from '@/components/PasswordInput.vue'
import { storeFallbackIcon, storeLogoUrl } from '@/lib/storeIcons'
import type { Account, AliLoginStep, CollectionRun, FuelPrice, LoyaltyCode, Receipt } from '@/types/api'
import { fmtDateTime } from '@/lib/datetime'

const props = defineProps<{ id: string }>()
const router = useRouter()

const account = ref<Account | null>(null)
const runs = ref<CollectionRun[]>([])
const receipts = ref<Receipt[]>([])
const loading = ref(true)
const collecting = ref(false)
const collectingAll = ref(false)

function addReceiptHere() {
  router.push({ name: 'receipt-new', query: { account_id: props.id } })
}

const tokenForm = ref({ access_token: '', extra_json: '' })
const scheduleForm = ref({ enabled: true, cron_expression: '0 12,19 * * *' })

const importJson = ref('')
const importing = ref(false)
const importError = ref('')
// Which store the interactive browser-login panel drives. Both AliExpress and
// Prom.ua use the same headed-Chromium flow (screenshot streaming + manual
// takeover); Prom skips the password step (phone + SMS only).
const browserLoginSeg = computed<'aliexpress' | 'prom' | 'rozetka' | 'olx'>(() =>
  account.value?.store.code === 'prom' ? 'prom' : account.value?.store.code === 'rozetka' ? 'rozetka' : account.value?.store.code === 'olx' ? 'olx' : 'aliexpress',
)
const usesBrowserLogin = computed(() =>
  ['aliexpress', 'prom', 'rozetka', 'olx'].includes(account.value?.store.code ?? ''),
)
// Що саме лежить у `phone_number` для цього магазину — каже сам адаптер
// (store.login_field), а не список кодів магазинів у фронтенді.
const LOGIN_FIELD_LABELS: Record<string, string> = {
  email: 'Email',
  phone_or_email: 'Телефон або email',
  phone: 'Телефон',
}
const loginFieldLabel = computed(
  () => LOGIN_FIELD_LABELS[account.value?.store.login_field ?? 'phone'] ?? 'Телефон',
)
const aliLoginStep = ref<AliLoginStep | null>(null)
const aliPassword = ref('')
const aliOtp = ref('')
const aliLoginBusy = ref(false)
const aliLoginError = ref('')
const aliBrowserImageUrl = ref('')
const aliManualBusy = ref(false)
let aliProgressTimer: ReturnType<typeof setInterval> | null = null
let aliPointerActive = false
let aliPointerQueue: Promise<void> = Promise.resolve()
let aliLastPointerMoveAt = 0

function stopAliProgressPolling() {
  if (aliProgressTimer) clearInterval(aliProgressTimer)
  aliProgressTimer = null
}

function startAliProgressPolling(sessionId: string) {
  stopAliProgressPolling()
  aliProgressTimer = setInterval(async () => {
    try {
      const progress = await api.accounts.browserLoginStatus(browserLoginSeg.value, props.id, sessionId)
      aliLoginStep.value = progress
      if (['saving_session', 'importing_orders', 'done'].includes(progress.step)) aliPassword.value = ''
      aliBrowserImageUrl.value = `${api.accounts.browserLoginScreenshotUrl(browserLoginSeg.value, props.id, sessionId)}?t=${Date.now()}`
      if (['done', 'error'].includes(progress.step)) stopAliProgressPolling()
    } catch {
      // The start request may not have registered its progress entry yet.
    }
  }, 800)
}

async function startAliLogin() {
  aliLoginBusy.value = true
  aliLoginError.value = ''
  const sessionId = crypto.randomUUID()
  aliLoginStep.value = {
    session_id: sessionId, step: 'starting', message: 'Запускаємо авторизацію…', run: null,
  }
  startAliProgressPolling(sessionId)
  try {
    aliLoginStep.value = await api.accounts.browserLoginStart(browserLoginSeg.value, props.id, { password: aliPassword.value, sessionId })
    if (aliLoginStep.value.step !== 'manual') aliPassword.value = ''
    if (aliLoginStep.value.step === 'done') await load()
  } catch (e) {
    aliLoginError.value = (e as Error).message
  } finally {
    aliLoginBusy.value = false
    if (!['manual', 'otp'].includes(aliLoginStep.value?.step ?? '')) stopAliProgressPolling()
  }
}

async function takeOverAliBrowser() {
  const sessionId = aliLoginStep.value?.session_id
  if (!sessionId) return
  aliLoginError.value = ''
  try {
    aliLoginStep.value = await api.accounts.browserLoginTakeover(browserLoginSeg.value, props.id, sessionId)
  } catch (e) {
    aliLoginError.value = (e as Error).message
  }
}

function aliLoginValue(): string {
  const value = (account.value?.phone_number ?? '').replace(/[\s()\-]/g, '')
  if (['prom', 'rozetka', 'olx'].includes(browserLoginSeg.value)) {
    const digits = value.replace(/\D/g, '')
    if (digits.length === 10 && digits.startsWith('0')) return '+38' + digits
    if (digits.startsWith('380')) return '+' + digits
    return value
  }
  if (value.includes('@')) return value
  if (value.startsWith('+380')) return value.slice(4).replace(/^0+/, '')
  if (value.startsWith('380') && value.length >= 12) return value.slice(3).replace(/^0+/, '')
  return value
}

async function typeAliText(value: string) {
  const sessionId = aliLoginStep.value?.session_id
  if (!sessionId || !value) return
  aliManualBusy.value = true
  try {
    await api.accounts.browserLoginText(browserLoginSeg.value, props.id, sessionId, value)
  } catch (e) {
    aliLoginError.value = (e as Error).message
  } finally {
    aliManualBusy.value = false
  }
}

function aliBrowserPointerPosition(event: PointerEvent, clamp: boolean): { x: number; y: number } | null {
  const container = event.currentTarget as HTMLElement
  const image = container.querySelector('img')
  if (!image) return null
  const rect = image.getBoundingClientRect()
  const rawX = (event.clientX - rect.left) * image.naturalWidth / rect.width
  const rawY = (event.clientY - rect.top) * image.naturalHeight / rect.height
  if (!clamp && (rawX < 0 || rawY < 0 || rawX > image.naturalWidth || rawY > image.naturalHeight)) return null
  return {
    x: Math.max(0, Math.min(image.naturalWidth, rawX)),
    y: Math.max(0, Math.min(image.naturalHeight, rawY)),
  }
}

function queueAliBrowserPointer(action: 'down' | 'move' | 'up', x: number, y: number) {
  const sessionId = aliLoginStep.value?.session_id
  if (!sessionId || aliLoginStep.value?.step !== 'manual') return
  aliPointerQueue = aliPointerQueue
    .then(() => api.accounts.browserLoginPointer(browserLoginSeg.value, props.id, sessionId, action, x, y))
    .catch((e: Error) => { aliLoginError.value = e.message })
}

function beginAliBrowserPointer(event: PointerEvent) {
  if (event.button !== 0 || aliLoginStep.value?.step !== 'manual') return
  const position = aliBrowserPointerPosition(event, false)
  if (!position) return
  event.preventDefault()
  const container = event.currentTarget as HTMLElement
  container.focus()
  container.setPointerCapture(event.pointerId)
  aliPointerActive = true
  aliLastPointerMoveAt = 0
  queueAliBrowserPointer('down', position.x, position.y)
}

function moveAliBrowserPointer(event: PointerEvent) {
  if (!aliPointerActive) return
  event.preventDefault()
  const now = performance.now()
  if (now - aliLastPointerMoveAt < 50) return
  const position = aliBrowserPointerPosition(event, true)
  if (!position) return
  aliLastPointerMoveAt = now
  queueAliBrowserPointer('move', position.x, position.y)
}

function endAliBrowserPointer(event: PointerEvent) {
  if (!aliPointerActive) return
  event.preventDefault()
  const position = aliBrowserPointerPosition(event, true)
  aliPointerActive = false
  const container = event.currentTarget as HTMLElement
  if (container.hasPointerCapture(event.pointerId)) container.releasePointerCapture(event.pointerId)
  if (position) queueAliBrowserPointer('up', position.x, position.y)
}

async function sendAliBrowserKey(event: KeyboardEvent) {
  const sessionId = aliLoginStep.value?.session_id
  if (!sessionId || aliLoginStep.value?.step !== 'manual') return
  event.preventDefault()
  await api.accounts.browserLoginKey(browserLoginSeg.value, props.id, sessionId, {
    key: event.key, alt: event.altKey, ctrl: event.ctrlKey,
    meta: event.metaKey, shift: event.shiftKey,
  }).catch((e: Error) => { aliLoginError.value = e.message })
}

async function checkAliManualLogin() {
  const sessionId = aliLoginStep.value?.session_id
  if (!sessionId) return
  aliManualBusy.value = true
  aliLoginError.value = ''
  try {
    aliLoginStep.value = await api.accounts.browserLoginCheck(browserLoginSeg.value, props.id, sessionId)
    if (aliLoginStep.value.step === 'done') {
      aliPassword.value = ''
      stopAliProgressPolling()
      await load()
    }
  } catch (e) {
    aliLoginError.value = (e as Error).message
    if (aliLoginStep.value) aliLoginStep.value.step = 'error'
  } finally {
    aliManualBusy.value = false
  }
}

async function confirmAliOtp() {
  if (!aliLoginStep.value?.session_id) return
  aliLoginBusy.value = true
  aliLoginError.value = ''
  try {
    aliLoginStep.value = await api.accounts.browserLoginOtp(
      browserLoginSeg.value, props.id, aliLoginStep.value.session_id, aliOtp.value,
    )
    aliOtp.value = ''
    if (aliLoginStep.value.step === 'done') await load()
  } catch (e) {
    aliLoginError.value = (e as Error).message
  } finally {
    aliLoginBusy.value = false
  }
}

async function cancelAliLogin() {
  stopAliProgressPolling()
  if (aliLoginStep.value?.session_id) {
    await api.accounts.browserLoginClose(browserLoginSeg.value, props.id, aliLoginStep.value.session_id).catch(() => {})
  }
  aliLoginStep.value = null
  aliPassword.value = ''
  aliOtp.value = ''
  aliLoginError.value = ''
  aliBrowserImageUrl.value = ''
}
const otpSending = ref(false)
const otpPending = ref(false)
const otpCode = ref('')
const otpConfirming = ref(false)
const otpError = ref('')

const oauthStarting = ref(false)
const oauthError = ref('')
let oauthPollTimer: ReturnType<typeof setInterval> | null = null
let foxtrotApprovalTimer: ReturnType<typeof setInterval> | null = null

async function startOauth() {
  oauthError.value = ''
  oauthStarting.value = true
  try {
    const { authorize_url } = await api.accounts.oauthStart(props.id)
    window.open(authorize_url, '_blank')
    pollForActive()
  } catch (e) {
    oauthError.value = (e as Error).message
  } finally {
    oauthStarting.value = false
  }
}

function pollForActive() {
  if (oauthPollTimer) clearInterval(oauthPollTimer)
  let attempts = 0
  oauthPollTimer = setInterval(async () => {
    attempts++
    await load()
    if (account.value?.status === 'active' || attempts > 40) {
      if (oauthPollTimer) clearInterval(oauthPollTimer)
      oauthPollTimer = null
    }
  }, 3000)
}

onUnmounted(() => {
  stopAliProgressPolling()
  stopRunsPolling()
  if (oauthPollTimer) clearInterval(oauthPollTimer)
  if (foxtrotApprovalTimer) clearInterval(foxtrotApprovalTimer)
  if (aliLoginStep.value?.session_id && aliLoginStep.value.step !== 'done') {
    api.accounts.browserLoginClose(browserLoginSeg.value, props.id, aliLoginStep.value.session_id).catch(() => {})
  }
})

const loyaltyCode = ref<LoyaltyCode | null>(null)
const loyaltyLoading = ref(false)
const loyaltyError = ref('')

async function loadLoyaltyCode() {
  loyaltyError.value = ''
  loyaltyLoading.value = true
  try {
    loyaltyCode.value = await api.accounts.loyaltyCode(props.id)
  } catch (e) {
    loyaltyCode.value = null
    loyaltyError.value = (e as Error).message
  } finally {
    loyaltyLoading.value = false
  }
}

const fuelPrices = ref<FuelPrice[] | null>(null)
const fuelLoading = ref(false)
const fuelError = ref('')

async function loadFuelPrices() {
  fuelError.value = ''
  fuelLoading.value = true
  try {
    fuelPrices.value = await api.accounts.fuelPrices(props.id)
  } catch (e) {
    fuelPrices.value = null
    fuelError.value = (e as Error).message
  } finally {
    fuelLoading.value = false
  }
}

function discountedPrice(p: FuelPrice) {
  return p.discount ? p.price - p.discount : p.price
}

function fmtMoney(v: number) {
  return new Intl.NumberFormat('uk-UA', { style: 'currency', currency: 'UAH' }).format(v)
}

async function load() {
  loading.value = true
  const [accounts, runList, receiptList] = await Promise.all([
    api.accounts.list(),
    api.accounts.runs(props.id),
    api.receipts.list({ account_id: props.id, limit: 20 }),
  ])
  account.value = accounts.find((a) => a.id === props.id) ?? null
  runs.value = runList
  receipts.value = receiptList.items
  // A deep-history job started before a page reload is still running in the
  // background — resume watching it instead of leaving progress silent.
  if (runs.value[0]?.status === 'running' && !runsPollTimer) startRunsPolling()
  if (account.value?.schedule) {
    scheduleForm.value = {
      enabled: account.value.schedule.enabled,
      cron_expression: account.value.schedule.cron_expression,
    }
  }
  loading.value = false
}

onMounted(load)

async function waitForRun(runId: string): Promise<CollectionRun> {
  for (let attempt = 0; attempt < 600; attempt += 1) {
    const current = (await api.accounts.runs(props.id)).find((run) => run.id === runId)
    if (current && current.status !== 'running') return current
    await new Promise((resolve) => setTimeout(resolve, 500))
  }
  throw new Error('Збір ще виконується — перевірте історію запусків пізніше.')
}

const editingAccount = ref(false)
const accountForm = ref({ label: '', phone_number: '' })
const savingAccount = ref(false)

function startEditAccount() {
  if (!account.value) return
  accountForm.value = { label: account.value.label, phone_number: account.value.phone_number ?? '' }
  editingAccount.value = true
}

async function saveAccount() {
  savingAccount.value = true
  try {
    await api.accounts.update(props.id, {
      label: accountForm.value.label,
      phone_number: accountForm.value.phone_number || null,
    })
    editingAccount.value = false
    await load()
  } catch (e) {
    alert((e as Error).message)
  } finally {
    savingAccount.value = false
  }
}

async function saveToken() {
  let extra = {}
  if (tokenForm.value.extra_json.trim()) {
    try {
      extra = JSON.parse(tokenForm.value.extra_json)
    } catch {
      alert('extra має бути коректним JSON')
      return
    }
  }
  await api.accounts.setToken(props.id, { access_token: tokenForm.value.access_token, extra })
  tokenForm.value.access_token = ''
  await load()
}

const testTokenBusy = ref(false)
const testTokenResult = ref<{ ok: boolean; detail: string } | null>(null)

async function testToken() {
  testTokenBusy.value = true
  testTokenResult.value = null
  try {
    testTokenResult.value = await api.bankTransactions.testToken(props.id)
  } catch (e) {
    testTokenResult.value = { ok: false, detail: (e as Error).message }
  } finally {
    testTokenBusy.value = false
  }
}

const pollBusy = ref(false)
const pollMessage = ref('')

async function pollTransactions() {
  pollBusy.value = true
  pollMessage.value = ''
  try {
    const result = await api.bankTransactions.poll(props.id)
    pollMessage.value = 'fetched' in result
      ? `Отримано: ${result.fetched}, нових: ${result.created}`
      : 'Розпочато'
    await load()
  } catch (e) {
    pollMessage.value = (e as Error).message
  } finally {
    pollBusy.value = false
  }
}

// Monobank throttles the statement endpoint to ~1 request per 31 days of
// history per minute, so a wide range runs in the background — see
// services/bank_transactions.run_deep_sync_in_background. Progress shows up
// as new transactions and a Журнал entry, not a live progress bar.
const deepHistoryOpen = ref(false)
const deepHistoryDate = ref('')
const deepHistoryBusy = ref(false)
const deepHistoryMessage = ref('')

// While a deep-history job is running in the background, its CollectionRun
// row is the only place progress lives (services/bank_transactions.py commits
// progress_current/progress_total to it after every throttled chunk) — poll
// the run list until it leaves "running" instead of making the user refresh
// the page repeatedly to see whether it's still going.
let runsPollTimer: ReturnType<typeof setInterval> | null = null

function stopRunsPolling() {
  if (runsPollTimer) clearInterval(runsPollTimer)
  runsPollTimer = null
}

function startRunsPolling() {
  stopRunsPolling()
  runsPollTimer = setInterval(async () => {
    runs.value = await api.accounts.runs(props.id)
    if (runs.value[0]?.status !== 'running') stopRunsPolling()
  }, 3000)
}

async function loadDeepHistory() {
  if (!deepHistoryDate.value) return
  deepHistoryBusy.value = true
  deepHistoryMessage.value = ''
  try {
    await api.bankTransactions.poll(props.id, `${deepHistoryDate.value}T00:00:00Z`)
    deepHistoryMessage.value = 'Розпочато у фоні — прогрес видно нижче, в «Історії запусків».'
    await load()
    startRunsPolling()
  } catch (e) {
    deepHistoryMessage.value = (e as Error).message
  } finally {
    deepHistoryBusy.value = false
  }
}

const cardNumberInput = ref('')
const cardLoginSaving = ref(false)
const cardLoginError = ref('')

const metroCartBarcode = ref('')
const metroCardBarcode = ref('')
const deviceLoginSaving = ref(false)
const deviceLoginError = ref('')

const passwordInput = ref('')
const passwordLoginSaving = ref(false)
const passwordLoginError = ref('')

async function submitPasswordLogin() {
  passwordLoginError.value = ''
  passwordLoginSaving.value = true
  try {
    await api.accounts.loginPassword(props.id, passwordInput.value)
    passwordInput.value = ''
    await load()
  } catch (e) {
    passwordLoginError.value = (e as Error).message
  } finally {
    passwordLoginSaving.value = false
  }
}

async function submitCardLogin() {
  cardLoginError.value = ''
  cardLoginSaving.value = true
  try {
    await api.accounts.loginCard(props.id, cardNumberInput.value.trim())
    cardNumberInput.value = ''
    await load()
  } catch (e) {
    cardLoginError.value = (e as Error).message
  } finally {
    cardLoginSaving.value = false
  }
}

async function submitDeviceLogin() {
  deviceLoginError.value = ''
  deviceLoginSaving.value = true
  try {
    await api.accounts.loginDeviceRegistration(
      props.id,
      metroCartBarcode.value.trim(),
      metroCardBarcode.value.trim(),
    )
    metroCartBarcode.value = ''
    metroCardBarcode.value = ''
    await load()
  } catch (e) {
    deviceLoginError.value = (e as Error).message
  } finally {
    deviceLoginSaving.value = false
  }
}

async function sendOtp() {
  otpSending.value = true
  otpError.value = ''
  try {
    await api.accounts.loginStart(props.id)
    otpPending.value = true
    if (account.value?.store.code === 'foxtrot') pollFoxtrotApproval()
  } catch (e) {
    otpError.value = (e as Error).message
  } finally {
    otpSending.value = false
  }
}

function pollFoxtrotApproval() {
  if (foxtrotApprovalTimer) clearInterval(foxtrotApprovalTimer)
  let attempts = 0
  foxtrotApprovalTimer = setInterval(async () => {
    attempts++
    try {
      await api.accounts.loginConfirm(props.id, '')
      otpPending.value = false
      clearInterval(foxtrotApprovalTimer!)
      foxtrotApprovalTimer = null
      await load()
    } catch (e) {
      if (attempts >= 40) {
        clearInterval(foxtrotApprovalTimer!)
        foxtrotApprovalTimer = null
        otpError.value = (e as Error).message
      }
    }
  }, 3000)
}

async function confirmOtp() {
  if (!otpCode.value && account.value?.store.auth_type !== 'phone_call') return
  otpConfirming.value = true
  otpError.value = ''
  try {
    await api.accounts.loginConfirm(props.id, otpCode.value || 'call-confirmed')
    otpPending.value = false
    otpCode.value = ''
    await load()
  } catch (e) {
    otpError.value = (e as Error).message
  } finally {
    otpConfirming.value = false
  }
}

async function importReceipts() {
  importError.value = ''
  let payload: unknown
  try {
    payload = JSON.parse(importJson.value)
  } catch {
    importError.value = 'Некоректний JSON'
    return
  }
  importing.value = true
  try {
    await api.accounts.importReceipts(props.id, payload)
    importJson.value = ''
    await load()
  } catch (e) {
    importError.value = (e as Error).message
  } finally {
    importing.value = false
  }
}

async function saveSchedule() {
  await api.accounts.updateSchedule(props.id, scheduleForm.value)
  await load()
}

async function collectNow() {
  collecting.value = true
  try {
    const started = await api.accounts.collectNow(props.id)
    await waitForRun(started.id)
    await load()
  } catch (e) {
    alert((e as Error).message)
  } finally {
    collecting.value = false
  }
}

async function collectAllAli() {
  collectingAll.value = true
  try {
    const started = await api.accounts.collectAll(props.id)
    await waitForRun(started.id)
    await load()
  } catch (e) {
    alert((e as Error).message)
  } finally {
    collectingAll.value = false
  }
}

const loggingOut = ref(false)
const resettingError = ref(false)
async function logout() {
  if (!confirm('Вийти з акаунта? Зібрані чеки залишаться, знадобиться повторний вхід.')) return
  loggingOut.value = true
  try {
    await api.accounts.logout(props.id)
    await load()
  } finally {
    loggingOut.value = false
  }
}

async function resetError() {
  resettingError.value = true
  try {
    await api.accounts.resetError(props.id)
    await load()
  } catch (e) {
    alert((e as Error).message)
  } finally {
    resettingError.value = false
  }
}

async function removeAccount() {
  if (!confirm('Видалити акаунт разом з усіма зібраними чеками?')) return
  await api.accounts.remove(props.id)
  router.push('/accounts')
}

function fmtDate(v: string | null) {
  return fmtDateTime(v)
}
</script>

<template>
  <div v-if="loading" class="muted">Завантаження…</div>
  <template v-else-if="account">
    <div class="toolbar">
      <h2 style="margin: 0; flex: 1">{{ account.label }}</h2>
      <span class="badge" :class="`badge-${account.status}`">{{ account.status }}</span>
      <button
        v-if="account.store.capabilities.receipts && account.status === 'active'"
        :disabled="collecting || collectingAll"
        @click="collectNow"
      >
        {{ collecting ? 'Збір…' : 'Зібрати зараз' }}
      </button>
      <button
        v-if="account.store.capabilities.receipts && account.status === 'active'"
        class="secondary"
        :disabled="collecting || collectingAll"
        @click="collectAllAli"
      >
        {{ collectingAll ? 'Завантажуємо всю історію…' : 'Завантажити всю історію' }}
      </button>
      <button v-if="account.store.capabilities.manual_receipts" @click="addReceiptHere">+ Додати чек</button>
      <button class="secondary" @click="startEditAccount">Редагувати</button>
      <button class="danger" @click="removeAccount">Видалити</button>
    </div>

    <div v-if="editingAccount" class="card" style="margin-bottom: 24px">
      <h3 style="margin-top: 0">Редагування акаунта</h3>
      <div class="form-row">
        <label>Назва</label>
        <input v-model="accountForm.label" />
      </div>
      <div class="form-row">
        <label>{{ loginFieldLabel }}</label>
        <input
          v-model="accountForm.phone_number"
          :placeholder="account?.store.login_field === 'email' ? 'you@example.com' : '+380...'"
        />
      </div>
      <div class="toolbar" style="margin-bottom: 0">
        <button :disabled="savingAccount" @click="saveAccount">
          {{ savingAccount ? 'Зберігаємо…' : 'Зберегти' }}
        </button>
        <button class="secondary" @click="editingAccount = false">Скасувати</button>
      </div>
    </div>

    <div class="card-grid">
      <div class="card">
        <div class="stat-label">Магазин</div>
        <div>{{ account.store.name }}</div>
      </div>
      <div class="card">
        <div class="stat-label">{{ loginFieldLabel }}</div>
        <div>{{ account.phone_number ?? '—' }}</div>
      </div>
      <div class="card">
        <div class="stat-label">Зібрано чеків</div>
        <div>{{ account.receipt_count }}</div>
      </div>
    </div>

    <!-- A successful manual run clears `last_error`, so nightly failures used to
         leave the page looking perfectly healthy. This block is driven by the
         run history instead and stays until the failures stop. -->
    <div
      v-if="!account.last_error && account.failed_runs_24h"
      class="card"
      style="border-color: var(--warning); margin-bottom: 24px"
    >
      <h3 style="margin-top: 0">Збір даних збоїть</h3>
      <p style="margin: 0">
        За добу невдалих запусків: <strong>{{ account.failed_runs_24h }}</strong>.
        Останній збій {{ fmtDateTime(account.last_failed_at) }}:
        <span class="muted">{{ account.last_failed_error }}</span>
      </p>
      <p class="muted" style="margin: 6px 0 0">
        Ручний збір міг пройти вдало й «затерти» помилку — перевірте
        <RouterLink to="/logs">журнал</RouterLink> і за потреби увійдіть повторно.
      </p>
    </div>

    <p v-if="account.last_error" class="card" style="border-color: var(--danger); color: var(--danger)">
      {{ account.last_error }}
      <button
        v-if="account.last_error || account.status === 'needs_reauth' || account.status === 'error'"
        class="secondary"
        style="display: block; margin-top: 10px"
        :disabled="resettingError"
        @click="resetError"
      >
        {{ resettingError ? 'Скидаємо…' : 'Скинути помилку' }}
      </button>
    </p>

    <div
      v-else-if="account.status === 'needs_reauth' || account.status === 'error'"
      class="card"
      style="border-color: var(--danger); color: var(--danger); margin-bottom: 24px"
    >
      <button class="secondary" :disabled="resettingError" @click="resetError">
        {{ resettingError ? 'Скидаємо…' : 'Скинути помилку' }}
      </button>
    </div>

    <div
      v-if="account.status === 'active' && account.store.auth_type !== 'manual'"
      class="card"
      style="margin-bottom: 24px"
    >
      <h3 style="margin-top: 0">Вхід виконано</h3>
      <button class="secondary" :disabled="loggingOut" @click="logout">
        {{ loggingOut ? 'Виходимо…' : 'Вийти' }}
      </button>
    </div>

    <div
      v-if="account.status === 'active' && account.store.capabilities.loyalty_code"
      class="card"
      style="margin-bottom: 24px"
    >
      <h3 style="margin-top: 0">Картка магазину</h3>
      <p v-if="loyaltyError" style="color: var(--danger)">{{ loyaltyError }}</p>
      <button v-if="!loyaltyCode" :disabled="loyaltyLoading" @click="loadLoyaltyCode">
        {{ loyaltyLoading ? 'Завантажуємо…' : 'Показати код' }}
      </button>
      <div v-else>
        <LoyaltyCodeDisplay :code="loyaltyCode" />
        <div class="toolbar" style="justify-content: center; margin-bottom: 0">
          <button class="secondary" :disabled="loyaltyLoading" @click="loadLoyaltyCode">
            {{ loyaltyLoading ? 'Оновлюємо…' : 'Оновити' }}
          </button>
        </div>
      </div>
    </div>

    <div
      v-if="account.status === 'active' && account.store.capabilities.fuel_prices"
      class="card"
      style="margin-bottom: 24px"
    >
      <h3 style="margin-top: 0; display: flex; align-items: center; gap: 8px">
        <img
          v-if="storeLogoUrl(account.store.code)"
          :src="storeLogoUrl(account.store.code)!"
          alt=""
          width="20"
          height="20"
          style="border-radius: 4px"
        />
        <span v-else>{{ storeFallbackIcon(account.store.code) }}</span>
        Ціни на паливо
      </h3>
      <p v-if="fuelError" style="color: var(--danger)">{{ fuelError }}</p>
      <button v-if="!fuelPrices" :disabled="fuelLoading" @click="loadFuelPrices">
        {{ fuelLoading ? 'Завантажуємо…' : 'Показати ціни' }}
      </button>
      <div v-else>
        <table>
          <thead>
            <tr>
              <th>Паливо</th>
              <th>Ціна</th>
              <th>Особиста знижка</th>
              <th>З урахуванням знижки</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="p in fuelPrices" :key="p.code ?? p.name">
              <td>{{ p.name }}</td>
              <td>{{ fmtMoney(p.price) }}</td>
              <td>{{ p.discount ? '−' + fmtMoney(p.discount) + '/л' : '—' }}</td>
              <td><strong>{{ fmtMoney(discountedPrice(p)) }}</strong></td>
            </tr>
          </tbody>
        </table>
        <div class="toolbar" style="justify-content: center; margin-bottom: 0">
          <button class="secondary" :disabled="fuelLoading" @click="loadFuelPrices">
            {{ fuelLoading ? 'Оновлюємо…' : 'Оновити' }}
          </button>
        </div>
      </div>
    </div>

    <template v-if="account.status !== 'active'">
      <template v-if="account.store.auth_type === 'oauth_browser'">
        <div class="card" style="margin-bottom: 24px">
          <h3 style="margin-top: 0">Вхід через Сільпо</h3>
          <p class="muted" style="margin-top: -8px">
            Офіційний OAuth-вхід через MCP-сервер Сільпо (auth.silpo.ua) — той самий телефон +
            SMS-код, що й у застосунку. Відкриється нова вкладка; після входу ця сторінка сама
            оновиться за кілька секунд.
          </p>
          <p v-if="oauthError" style="color: var(--danger)">{{ oauthError }}</p>
          <button :disabled="oauthStarting" @click="startOauth">
            {{ oauthStarting ? 'Відкриваємо…' : 'Увійти через Сільпо' }}
          </button>
        </div>
      </template>

      <template v-else-if="account.store.auth_type === 'card_number'">
        <div class="card" style="margin-bottom: 24px">
          <h3 style="margin-top: 0">Номер картки</h3>
          <p class="muted" style="margin-top: -8px">
            Цей магазин не підтримує повний вхід в акаунт (див. docstring адаптера) — введіть
            номер фізичної картки лояльності, і застосунок зможе показувати QR/штрихкод для
            каси. Історія покупок і баланс тут недоступні.
          </p>
          <p v-if="cardLoginError" style="color: var(--danger)">{{ cardLoginError }}</p>
          <div class="form-row">
            <label>Номер картки</label>
            <input v-model="cardNumberInput" placeholder="1234567890" />
          </div>
          <button :disabled="cardLoginSaving || !cardNumberInput" @click="submitCardLogin">
            {{ cardLoginSaving ? 'Зберігаємо…' : 'Зберегти' }}
          </button>
        </div>
      </template>

      <template v-else-if="account.store.auth_type === 'device_uuid'">
        <div class="card" style="margin-bottom: 24px">
          <h3 style="margin-top: 0">Підключення METRO</h3>
          <p class="muted" style="margin-top: -8px">
            Введіть номер візка (наприклад, 125) або вставте його повний код, потім код картки клієнта.
            Це потрібно лише один раз: надалі чеки завантажуватимуться автоматично.
          </p>
          <p v-if="deviceLoginError" style="color: var(--danger)">{{ deviceLoginError }}</p>
          <div class="form-row">
            <label>Номер або повний код візка</label>
            <input v-model="metroCartBarcode" inputmode="numeric" placeholder="125" />
          </div>
          <div class="form-row">
            <label>Картка клієнта METRO</label>
            <input v-model="metroCardBarcode" inputmode="numeric" placeholder="Відскануйте або введіть номер картки" />
          </div>
          <button :disabled="deviceLoginSaving || !metroCartBarcode || !metroCardBarcode" @click="submitDeviceLogin">
            {{ deviceLoginSaving ? 'Підключаємо…' : 'Підключити' }}
          </button>
        </div>
      </template>

      <template v-else-if="account.store.auth_type === 'phone_password'">
        <div class="card" style="margin-bottom: 24px">
          <h3 style="margin-top: 0">
            Вхід у {{ account.store.code === 'aliexpress' ? 'AliExpress' : account.store.code === 'prom' ? 'Prom.ua' : account.store.name }}
          </h3>
          <p v-if="account.store.code === 'aliexpress'" class="muted" style="margin-top: -8px">
            Телефон і пароль передаються браузеру AliExpress лише для цього входу та не
            зберігаються. У базі залишиться тільки зашифрована браузерна сесія.
          </p>
          <p v-else-if="['prom', 'rozetka', 'olx'].includes(account.store.code)" class="muted" style="margin-top: -8px">
            Відкриється браузер {{ account.store.name }}. Введіть номер
            телефону, потім SMS-код. У базі залишиться лише зашифрована браузерна сесія.
            Якщо щось піде не так — перехопіть керування браузером.
          </p>
          <p v-else-if="account.store.login_field === 'email'" class="muted" style="margin-top: -8px">
            {{ account.store.name }} логінить лише за email — той самий, що й на сайті (вхід
            через Google чи Facebook не підходить). Застосунок збереже лише зашифровану сесію,
            пароль не зберігається.
          </p>
          <p v-else class="muted" style="margin-top: -8px">
            Введіть пароль від кабінету. Застосунок збереже лише зашифровану сесію — пароль
            не зберігається. Якщо {{ account.store.name }} запросить SMS, завершіть перевірку на сайті й повторіть вхід.
          </p>
          <p v-if="usesBrowserLogin ? aliLoginError : passwordLoginError" style="color: var(--danger)">
            {{ usesBrowserLogin ? aliLoginError : passwordLoginError }}
          </p>
          <!-- Only shown while the login is still missing: once it's set, the
               card grid above already displays it and this row is a duplicate. -->
          <div v-if="!account.phone_number" class="form-row">
            <label>{{ loginFieldLabel }}</label>
            <input value="" disabled placeholder="Спочатку вкажіть логін" />
          </div>
          <template v-if="usesBrowserLogin">
            <div v-if="aliLoginStep" style="margin: 12px 0 16px">
              <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 8px">
                <span class="badge" :class="aliLoginStep.step === 'error' ? 'badge-error' : aliLoginStep.step === 'done' ? 'badge-active' : ''">
                  {{ aliLoginStep.step }}
                </span>
                <span>{{ aliLoginStep.message }}</span>
              </div>
              <p v-if="aliLoginStep.step === 'manual'" class="muted">
                Клацайте та перетягуйте елементи на екрані браузера. Після кліку по полю можна вводити текст і натискати Enter, Tab, Escape або стрілки.
              </p>
              <div
                v-if="(aliLoginBusy || ['manual', 'otp'].includes(aliLoginStep.step)) && aliBrowserImageUrl"
                :tabindex="aliLoginStep.step === 'manual' ? 0 : -1"
                @pointerdown="beginAliBrowserPointer"
                @pointermove="moveAliBrowserPointer"
                @pointerup="endAliBrowserPointer"
                @pointercancel="endAliBrowserPointer"
                @keydown="sendAliBrowserKey"
                style="border: 1px solid var(--border); border-radius: 10px; overflow: hidden; background: #111; outline: none"
                :style="aliLoginStep.step === 'manual' ? 'cursor: crosshair; touch-action: none' : ''"
              >
                <img
                  :src="aliBrowserImageUrl"
                  alt="Поточний екран браузера входу"
                  draggable="false"
                  style="display: block; width: 100%; height: auto; user-select: none"
                />
              </div>
              <div v-if="!['manual', 'done', 'error'].includes(aliLoginStep.step)" class="toolbar" style="margin: 10px 0 0">
                <button
                  class="secondary"
                  :disabled="aliLoginStep.step === 'manual_requested'"
                  @click="takeOverAliBrowser"
                >
                  {{ aliLoginStep.step === 'manual_requested' ? 'Передаємо керування…' : 'Перехопити керування' }}
                </button>
              </div>
              <div v-if="aliLoginStep.step === 'manual'" class="toolbar" style="margin: 10px 0 0">
                <button class="secondary" :disabled="aliManualBusy || !account.phone_number" @click="typeAliText(aliLoginValue())">
                  {{ ['prom', 'rozetka', 'olx'].includes(browserLoginSeg) ? 'Ввести телефон' : 'Ввести логін' }}
                </button>
                <button
                  v-if="browserLoginSeg === 'aliexpress'"
                  class="secondary"
                  :disabled="aliManualBusy || !aliPassword"
                  @click="typeAliText(aliPassword)"
                >
                  Ввести пароль
                </button>
                <button :disabled="aliManualBusy" @click="checkAliManualLogin">
                  {{ aliManualBusy ? 'Перевіряємо…' : 'Перевірити вхід' }}
                </button>
                <button class="secondary" :disabled="aliManualBusy" @click="cancelAliLogin">Скасувати</button>
              </div>
            </div>
            <template v-if="aliLoginStep?.step !== 'otp'">
              <div v-if="browserLoginSeg === 'aliexpress'" class="form-row">
                <label>Пароль</label>
                <PasswordInput v-model="aliPassword" autocomplete="current-password" @keyup.enter="startAliLogin" />
              </div>
              <button
                :disabled="aliLoginBusy || !account.phone_number || (browserLoginSeg === 'aliexpress' && !aliPassword)"
                @click="startAliLogin"
              >
                {{ aliLoginBusy ? 'Входимо…' : 'Увійти' }}
              </button>
            </template>
            <template v-else>
              <p class="muted">{{ aliLoginStep.message }}</p>
              <div class="form-row">
                <label>Код підтвердження</label>
                <input v-model="aliOtp" autocomplete="one-time-code" inputmode="numeric" @keyup.enter="confirmAliOtp" />
              </div>
              <div class="toolbar" style="margin-bottom: 0">
                <button :disabled="aliLoginBusy || !aliOtp" @click="confirmAliOtp">
                  {{ aliLoginBusy ? 'Перевіряємо…' : 'Підтвердити' }}
                </button>
                <button class="secondary" :disabled="aliLoginBusy" @click="cancelAliLogin">Скасувати</button>
              </div>
            </template>
          </template>
          <template v-else>
            <div class="form-row">
              <label>Пароль</label>
              <PasswordInput v-model="passwordInput" autocomplete="current-password" @keyup.enter="submitPasswordLogin" />
            </div>
            <button :disabled="passwordLoginSaving || !account.phone_number || !passwordInput" @click="submitPasswordLogin">
              {{ passwordLoginSaving ? 'Входимо…' : 'Увійти' }}
            </button>
          </template>
          <p v-if="!account.phone_number" class="muted" style="margin-bottom: 0">
            Спочатку натисніть «Редагувати» й укажіть {{ loginFieldLabel.toLowerCase() }}.
          </p>
          <p
            v-if="usesBrowserLogin && aliLoginStep && ['manual', 'error'].includes(aliLoginStep.step)"
            class="muted"
            style="margin-bottom: 0"
          >
            <button class="secondary" :disabled="aliManualBusy" @click="cancelAliLogin">Скасувати сесію входу</button>
          </p>
        </div>
      </template>

      <template v-else-if="account.store.auth_type === 'manual' && account.store.code !== 'prom'">
        <div class="card" style="margin-bottom: 24px">
          <h3 style="margin-top: 0">
            {{ account.store.code === 'prom' ? 'Імпорт замовлень Prom.ua' : 'Ручний режим' }}
          </h3>
          <p v-if="account.store.code === 'prom'" class="muted" style="margin-top: -8px">
            Для покупця Prom.ua немає офіційного API-входу. Натисніть кнопку нижче:
            відкриється браузер Prom.ua, після входу замовлення імпортуються в цей акаунт.
          </p>
          <p v-else class="muted" style="margin-top: -8px">
            Для цього джерела не потрібен SMS-вхід.
          </p>
        </div>
      </template>

      <template v-else>
        <div class="card" style="margin-bottom: 24px">
          <h3 style="margin-top: 0">
            {{ account.store.code === 'foxtrot' ? 'Вхід через Viber' : account.store.auth_type === 'phone_call' ? 'Вхід через дзвінок' : 'Вхід по SMS' }}
          </h3>
          <p v-if="account.store.auth_type === 'phone_call'" class="muted" style="margin-top: -8px">
            БРСМ зателефонує на вказаний номер. Дайте відповідь і виконайте підказку дзвінка,
            потім натисніть «Перевірити дзвінок».
          </p>
          <p v-if="account.store.code === 'dnipro-m'" class="muted" style="margin-top: -8px">
            Під капотом відкривається браузер на сервері, сам вводить номер і код — вам треба
            лише ввести код із SMS сюди. Якщо «Перевищено ліміт запитів» — зачекайте годину.
          </p>
          <p v-if="account.store.code === 'fozzy'" class="muted" style="margin-top: -8px">
            Під капотом відкривається офіційна сторінка входу Fozzy у браузері, яка сама
            проходить Turnstile. Вам треба лише ввести код із SMS сюди.
          </p>
          <p v-if="account.store.code === 'prom'" class="muted" style="margin-top: -8px">
            Браузер на сервері вводить телефон і SMS-код, зберігає сесію та надалі
            автоматично отримує замовлення через кабінет Prom.ua.
          </p>
          <p v-if="otpError" style="color: var(--danger)">{{ otpError }}</p>
          <template v-if="!otpPending">
            <button :disabled="otpSending || !account.phone_number" @click="sendOtp">
              {{ otpSending ? 'Запускаємо…' : account.store.code === 'foxtrot' ? 'Надіслати підтвердження' : account.store.auth_type === 'phone_call' ? 'Замовити дзвінок' : 'Надіслати код' }}
            </button>
            <p v-if="!account.phone_number" class="muted" style="margin-bottom: 0">
              Спершу вкажіть номер телефону для акаунта.
            </p>
          </template>
          <template v-else>
            <p v-if="account.store.code === 'foxtrot'" class="muted">
              Підтвердіть вхід у Viber. Сторінка автоматично перевірить підтвердження.
            </p>
            <div v-else-if="account.store.auth_type !== 'phone_call'" class="form-row">
              <label>Код з SMS</label>
              <input v-model="otpCode" placeholder="123456" />
            </div>
            <button v-if="account.store.code !== 'foxtrot'" :disabled="otpConfirming" @click="confirmOtp">
              {{ otpConfirming ? 'Перевіряємо…' : account.store.auth_type === 'phone_call' ? 'Перевірити дзвінок' : 'Підтвердити' }}
            </button>
          </template>
        </div>

        <div class="card" style="margin-bottom: 24px">
          <h3 style="margin-top: 0">Токен доступу вручну</h3>
          <p v-if="account.store.code === 'dnipro-m'" class="muted" style="margin-top: -8px">
            Запасний шлях, якщо SMS не проходить: запустіть
            <code>backend/scripts/dnipro_m_login.py</code> на машині з дисплеєм. Поле нижче
            заповнювати вручну не треба.
          </p>
          <p v-else class="muted" style="margin-top: -8px">
            Якщо автоматичний вхід тимчасово недоступний або якщо
            токен потрібно замінити напряму: вставте значення, перехоплене через mitmproxy.
          </p>
          <div class="form-row">
            <label>access_token</label>
            <input v-model="tokenForm.access_token" placeholder="Bearer / Token значення" />
          </div>
          <div class="form-row">
            <label>extra (JSON, необов'язково — наприклад customerId для METRO)</label>
            <input v-model="tokenForm.extra_json" placeholder='{"customerId": "..."}' />
          </div>
          <button @click="saveToken">Зберегти токен</button>
        </div>
      </template>
    </template>

    <template v-if="account.store.auth_type === 'api_token'">
      <div class="card" style="margin-bottom: 24px">
        <h3 style="margin-top: 0">Токен</h3>
        <p class="muted" style="margin-top: -8px">
          Особистий API-токен цього підключення. Кожне підключення (наприклад,
          інша картка того самого банку) — окремий акаунт зі своїм токеном.
        </p>
        <p v-if="account.status === 'active'" class="muted" style="margin-top: -8px">
          Токен збережено. Вставте новий, щоб замінити його.
        </p>
        <div class="form-row">
          <label>Токен</label>
          <PasswordInput
            v-model="tokenForm.access_token"
            autocomplete="off"
            :placeholder="account.status === 'active' ? '••••••••' : 'Вставте токен'"
          />
        </div>
        <div class="toolbar">
          <button :disabled="!tokenForm.access_token.trim()" @click="saveToken">Зберегти токен</button>
          <button
            v-if="account.store.capabilities.token_test"
            class="secondary"
            :disabled="testTokenBusy || account.status !== 'active'"
            @click="testToken"
          >
            {{ testTokenBusy ? 'Перевірка…' : 'Перевірити токен' }}
          </button>
        </div>
        <p
          v-if="testTokenResult"
          :style="{ color: testTokenResult.ok ? 'var(--success)' : 'var(--danger)' }"
        >
          {{ testTokenResult.detail }}
        </p>
      </div>

      <div
        v-if="account.store.capabilities.transactions && account.status === 'active'"
        class="card"
        style="margin-bottom: 24px"
      >
        <h3 style="margin-top: 0">Виписка</h3>
        <p class="muted" style="margin-top: -8px">
          Отримується за розкладом нижче, або одразу кнопкою.
        </p>
        <div class="toolbar">
          <button :disabled="pollBusy" @click="pollTransactions">
            {{ pollBusy ? 'Отримання…' : 'Отримати зараз' }}
          </button>
          <RouterLink to="/bank-transactions">Переглянути транзакції</RouterLink>
        </div>
        <p v-if="pollMessage" class="muted">{{ pollMessage }}</p>

        <div style="margin-top: 14px; border-top: 1px solid var(--border); padding-top: 12px">
          <button class="secondary" @click="deepHistoryOpen = !deepHistoryOpen">
            {{ deepHistoryOpen ? 'Сховати' : 'Завантажити глибшу історію' }}
          </button>
          <div v-if="deepHistoryOpen" style="margin-top: 10px">
            <p class="muted" style="margin-top: 0">
              Monobank віддає виписку частинами по 31 дню з обмеженням приблизно
              1 запит на хвилину — за старіший період завантаження триває
              кілька хвилин і йде у фоні; сторінка не чекає на нього.
            </p>
            <div class="form-row">
              <label>Отримати з дати</label>
              <input v-model="deepHistoryDate" type="date" />
            </div>
            <button :disabled="deepHistoryBusy || !deepHistoryDate" @click="loadDeepHistory">
              {{ deepHistoryBusy ? 'Запускаємо…' : 'Почати завантаження' }}
            </button>
            <p v-if="deepHistoryMessage" class="muted">{{ deepHistoryMessage }}</p>
          </div>
        </div>
      </div>
    </template>

    <div
      v-if="account.store.capabilities.receipts || account.store.capabilities.transactions"
      class="card"
      style="margin-bottom: 24px"
    >
      <h3 style="margin-top: 0">Розклад збору</h3>
      <div class="form-row">
        <label><input type="checkbox" v-model="scheduleForm.enabled" style="width: auto" /> Увімкнено</label>
      </div>
      <div class="form-row">
        <label>Cron-вираз</label>
        <input v-model="scheduleForm.cron_expression" placeholder="0 12,19 * * *" />
      </div>
      <button @click="saveSchedule">Зберегти розклад</button>
    </div>

    <div v-if="account.store.capabilities.manual_import" class="card" style="margin-bottom: 24px">
      <h3 style="margin-top: 0">Ручний імпорт</h3>
      <p class="muted" style="margin-top: -8px">
        <template v-if="account.store.code === 'prom'">
          Prom.ua також підтримує імпорт через вбудований браузер вище.
          Поле нижче лишається запасним імпортом JSON з масивом <code>orders</code>.
        </template>
        <template v-else-if="account.store.code === 'aliexpress'">
          Вставте JSON історії замовлень AliExpress з масивом <code>orders</code> або
          <code>orderList</code>. Статуси й усі трек-номери оновляться при повторному імпорті.
        </template>
        <template v-else>
          Вставте сирий JSON-відповідь API, перехоплену через mitmproxy з додатку, і
          натисніть "Імпортувати".
        </template>
      </p>
      <p v-if="importError" style="color: var(--danger)">{{ importError }}</p>
      <div class="form-row">
        <label>JSON-відповідь API</label>
        <textarea
          v-model="importJson"
          rows="6"
          style="font-family: monospace; font-size: 12px; width: 100%; resize: vertical"
          :placeholder="['prom', 'aliexpress'].includes(account.store.code) ? '{&quot;orders&quot;: [...]}' : '{&quot;Data&quot;: [...]}'"
        ></textarea>
      </div>
      <button :disabled="importing || !importJson" @click="importReceipts">
        {{ importing ? 'Імпортуємо…' : 'Імпортувати' }}
      </button>
    </div>

    <template v-if="account.store.capabilities.receipts || account.store.capabilities.transactions">
      <h3>Історія запусків</h3>
      <table>
        <thead>
          <tr>
            <th>Коли</th>
            <th>Тип</th>
            <th>Статус</th>
            <th>Знайдено</th>
            <th>Нових</th>
            <th>Прогрес</th>
            <th>Помилка</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in runs" :key="r.id">
            <td>{{ fmtDate(r.started_at) }}</td>
            <td>{{ r.trigger }}</td>
            <td><span class="badge" :class="r.status === 'success' ? 'badge-active' : 'badge-error'">{{ r.status }}</span></td>
            <td>{{ r.receipts_found }}</td>
            <td>{{ r.receipts_new }}</td>
            <td>
              <template v-if="r.progress_total">
                <div style="display: flex; align-items: center; gap: 6px">
                  <div style="width: 60px; height: 6px; border-radius: 3px; background: var(--border); overflow: hidden">
                    <div
                      :style="{
                        width: `${Math.round(((r.progress_current ?? 0) / r.progress_total) * 100)}%`,
                        height: '100%',
                        background: r.status === 'error' ? 'var(--danger)' : 'var(--accent)',
                      }"
                    ></div>
                  </div>
                  <span class="muted" style="font-size: 12px; white-space: nowrap">
                    {{ r.progress_current ?? 0 }}/{{ r.progress_total }}
                  </span>
                </div>
              </template>
              <span v-else class="muted">—</span>
            </td>
            <td class="muted">{{ r.error_message ?? '—' }}</td>
          </tr>
          <tr v-if="!runs.length">
            <td colspan="7" class="muted">Ще не було жодного запуску</td>
          </tr>
        </tbody>
      </table>
    </template>

    <div class="toolbar">
      <h3 style="margin: 0; flex: 1">Чеки</h3>
      <button v-if="account.store.capabilities.manual_receipts" class="secondary" @click="addReceiptHere">
        + Додати чек
      </button>
    </div>
    <table>
      <thead>
        <tr>
          <th>Дата</th>
          <th>Канал</th>
          <th>Магазин</th>
          <th>Сума</th>
          <th>Знижка</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="r in receipts" :key="r.id">
          <td><RouterLink :to="`/receipts/${r.id}`">{{ fmtDate(r.purchased_at) }}</RouterLink></td>
          <td><span class="badge" :class="`badge-channel-${r.channel}`">{{ r.channel === 'online' ? 'Онлайн' : 'Офлайн' }}</span></td>
          <td>{{ r.shop_name ?? '—' }}</td>
          <td>{{ fmtMoney(r.total_amount) }}</td>
          <td class="muted">{{ r.total_discount ? fmtMoney(r.total_discount) : '—' }}</td>
        </tr>
        <tr v-if="!receipts.length">
          <td colspan="5" class="muted">Ще немає жодного чека для цього акаунта</td>
        </tr>
      </tbody>
    </table>
  </template>
</template>
