import type {
  Account,
  AliLoginStep,
  AccountFuelPrices,
  AiBackendCheck,
  AiCheckResult,
  AiConfig,
  AiLogEntry,
  AiModelChoices,
  AiProviderModels,
  AiSettings,
  AppSetting,
  Category,
  CategorySpend,
  CategoryStats,
  ManualApplyResult,
  ManualPrompt,
  ChannelBreakdown,
  DriveColorPalette,
  DriveConfig,
  DriveFolder,
  DriveFoldersIn,
  DriveStatus,
  DuplicateGroup,
  DuplicateMergeChoices,
  DuplicateRun,
  InboxConfig,
  InboxDocument,
  ScanJob,
  ScanStats,
  UploadResult,
  CollectionRun,
  DashboardStats,
  FuelPriceHistory,
  FuelPrice,
  LiveProduct,
  LoyaltyCode,
  McpConfig,
  McpSettings,
  ProductPrice,
  Receipt,
  ReceiptCreate,
  ReceiptItem,
  ReceiptItemInput,
  ReceiptList,
  ShopSuggestion,
  TagSummary,
  TaxCabinetSettings,
  TelegramSettings,
  TelegramMessageLogList,
  Store,
  StoreBreakdown,
  StoreCategoryItem,
  StoreCategoryMatrix,
  BulkScanOut,
  SeriesUploadResult,
  TimeseriesPoint,
  VoiceMessage,
  WarrantyProduct,
  WidgetMeta,
  WidgetSnapshot,
} from '@/types/api'

// Keep browser requests on the same origin as the page. In development Vite
// proxies /api to the backend, so a phone only needs access to port 5173; it no
// longer has to reach Docker's port 8001 separately. `new URL('.')` also keeps
// a Home Assistant Ingress path prefix when the production build is embedded
// below /api/hassio_ingress/<token>/.
const pageBase = new URL('.', window.location.href)
const BASE = import.meta.env.VITE_API_BASE ?? pageBase.href.replace(/\/$/, '')
export { BASE }

/** Shared dashboard query: date window + whether amounts are net of discounts. */
export interface DashboardParams {
  date_from?: string
  date_to?: string
  include_discount?: string
  [key: string]: string | undefined
}

function qs(params?: Record<string, string | number | undefined>): string {
  if (!params) return ''
  const s = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== '') s.set(k, String(v))
  }
  const str = s.toString()
  return str ? `?${str}` : ''
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = body.detail ?? detail
    } catch {
      /* ignore */
    }
    throw new Error(detail)
  }
  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

export const api = {
  stores: {
    list: () => request<Store[]>('/api/stores'),
  },
  accounts: {
    list: () => request<Account[]>('/api/accounts'),
    create: (payload: { store_code: string; label: string; phone_number?: string }) =>
      request<Account>('/api/accounts', { method: 'POST', body: JSON.stringify(payload) }),
    remove: (id: string) => request<void>(`/api/accounts/${id}`, { method: 'DELETE' }),
    loginStart: (id: string) =>
      request<{ pending: boolean }>(`/api/accounts/${id}/login/start`, { method: 'POST' }),
    loginConfirm: (id: string, code: string) =>
      request<{ status: string }>(`/api/accounts/${id}/login/confirm`, {
        method: 'POST',
        body: JSON.stringify({ code }),
      }),
    loginPassword: (id: string, password: string) =>
      request<{ status: string }>(`/api/accounts/${id}/login/password`, {
        method: 'POST',
        body: JSON.stringify({ password }),
      }),
    loginCard: (id: string, cardNumber: string) =>
      request<{ status: string }>(`/api/accounts/${id}/login/card`, {
        method: 'POST',
        body: JSON.stringify({ card_number: cardNumber }),
      }),
    loginDevice: (id: string, deviceUuid: string) =>
      request<{ status: string }>(`/api/accounts/${id}/login/device`, {
        method: 'POST',
        body: JSON.stringify({ device_uuid: deviceUuid }),
      }),
    loginDeviceRegistration: (id: string, cartBarcode: string, cardBarcode: string) =>
      request<{ status: string }>(`/api/accounts/${id}/login/device-registration`, {
        method: 'POST',
        body: JSON.stringify({ cart_barcode: cartBarcode, card_barcode: cardBarcode }),
      }),
    logout: (id: string) => request<Account>(`/api/accounts/${id}/logout`, { method: 'POST' }),
    oauthStart: (id: string) =>
      request<{ authorize_url: string }>(`/api/accounts/${id}/oauth/start`),
    setToken: (id: string, payload: { access_token: string; extra?: Record<string, unknown> }) =>
      request<Account>(`/api/accounts/${id}/token`, { method: 'POST', body: JSON.stringify(payload) }),
    updateSchedule: (id: string, payload: { enabled?: boolean; cron_expression?: string }) =>
      request<{ status: string }>(`/api/accounts/${id}/schedule`, {
        method: 'PUT',
        body: JSON.stringify(payload),
      }),
    collectNow: (id: string) => request<CollectionRun>(`/api/accounts/${id}/collect`, { method: 'POST' }),
    collectAll: (id: string) => request<CollectionRun>(`/api/accounts/${id}/collect-all`, { method: 'POST' }),
    importReceipts: (id: string, payload: unknown) =>
      request<CollectionRun>(`/api/accounts/${id}/import`, {
        method: 'POST',
        body: JSON.stringify({ payload }),
      }),
    // Browser-driven interactive login (headed Chromium with screenshot
    // streaming + manual takeover). `seg` selects the store: 'aliexpress' or
    // 'prom'. AliExpress passes a password on start; Prom is phone + SMS only.
    browserLoginStart: (
      seg: 'aliexpress' | 'prom', id: string, opts: { password?: string; sessionId?: string } = {},
    ) =>
      request<AliLoginStep>(`/api/accounts/${id}/${seg}/login/start`, {
        method: 'POST',
        body: JSON.stringify(
          seg === 'aliexpress'
            ? { password: opts.password ?? '', session_id: opts.sessionId }
            : { session_id: opts.sessionId },
        ),
      }),
    browserLoginStatus: (seg: 'aliexpress' | 'prom', id: string, sessionId: string) =>
      request<AliLoginStep>(`/api/accounts/${id}/${seg}/login/${sessionId}/status`),
    browserLoginScreenshotUrl: (seg: 'aliexpress' | 'prom', id: string, sessionId: string) =>
      `${BASE}/api/accounts/${id}/${seg}/login/${sessionId}/screenshot`,
    browserLoginClick: (seg: 'aliexpress' | 'prom', id: string, sessionId: string, x: number, y: number) =>
      request<void>(`/api/accounts/${id}/${seg}/login/${sessionId}/click`, {
        method: 'POST', body: JSON.stringify({ x, y }),
      }),
    browserLoginPointer: (
      seg: 'aliexpress' | 'prom', id: string, sessionId: string,
      action: 'down' | 'move' | 'up', x: number, y: number,
    ) => request<void>(`/api/accounts/${id}/${seg}/login/${sessionId}/pointer`, {
      method: 'POST', body: JSON.stringify({ action, x, y }),
    }),
    browserLoginTakeover: (seg: 'aliexpress' | 'prom', id: string, sessionId: string) =>
      request<AliLoginStep>(`/api/accounts/${id}/${seg}/login/${sessionId}/takeover`, { method: 'POST' }),
    browserLoginKey: (
      seg: 'aliexpress' | 'prom', id: string, sessionId: string,
      payload: { key: string; alt: boolean; ctrl: boolean; meta: boolean; shift: boolean },
    ) => request<void>(`/api/accounts/${id}/${seg}/login/${sessionId}/key`, {
      method: 'POST', body: JSON.stringify(payload),
    }),
    browserLoginText: (seg: 'aliexpress' | 'prom', id: string, sessionId: string, value: string) =>
      request<void>(`/api/accounts/${id}/${seg}/login/${sessionId}/text`, {
        method: 'POST', body: JSON.stringify({ value }),
      }),
    browserLoginCheck: (seg: 'aliexpress' | 'prom', id: string, sessionId: string) =>
      request<AliLoginStep>(`/api/accounts/${id}/${seg}/login/${sessionId}/check`, { method: 'POST' }),
    browserLoginOtp: (seg: 'aliexpress' | 'prom', id: string, sessionId: string, code: string) =>
      request<AliLoginStep>(`/api/accounts/${id}/${seg}/login/otp`, {
        method: 'POST', body: JSON.stringify({ session_id: sessionId, code }),
      }),
    browserLoginClose: (seg: 'aliexpress' | 'prom', id: string, sessionId: string) =>
      request<void>(`/api/accounts/${id}/${seg}/login/close`, {
        method: 'POST', body: JSON.stringify({ session_id: sessionId }),
      }),
    runs: (id: string) => request<CollectionRun[]>(`/api/accounts/${id}/runs`),
    loyaltyCode: (id: string) => request<LoyaltyCode>(`/api/accounts/${id}/loyalty-code`),
    fuelPrices: (id: string) => request<FuelPrice[]>(`/api/accounts/${id}/fuel-prices`),
    update: (id: string, payload: { label?: string; phone_number?: string | null }) =>
      request<Account>(`/api/accounts/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  },
  receipts: {
    list: (params: Record<string, string | number | undefined> = {}) => {
      const qs = new URLSearchParams()
      for (const [k, v] of Object.entries(params)) {
        if (v !== undefined && v !== '') qs.set(k, String(v))
      }
      const suffix = qs.toString() ? `?${qs}` : ''
      return request<ReceiptList>(`/api/receipts${suffix}`)
    },
    get: (id: string) => request<Receipt>(`/api/receipts/${id}`),
    update: (id: string, payload: Partial<Receipt>) =>
      request<Receipt>(`/api/receipts/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
    addItem: (receiptId: string, payload: ReceiptItemInput) =>
      request<ReceiptItem>(`/api/receipts/${receiptId}/items`, {
        method: 'POST',
        body: JSON.stringify(payload),
      }),
    updateItem: (receiptId: string, itemId: string, payload: Partial<ReceiptItem>) =>
      request<ReceiptItem>(`/api/receipts/${receiptId}/items/${itemId}`, {
        method: 'PATCH',
        body: JSON.stringify(payload),
      }),
    removeItem: (receiptId: string, itemId: string) =>
      request<void>(`/api/receipts/${receiptId}/items/${itemId}`, { method: 'DELETE' }),
    create: (payload: ReceiptCreate) =>
      request<Receipt>('/api/receipts', { method: 'POST', body: JSON.stringify(payload) }),
    remove: (id: string) => request<void>(`/api/receipts/${id}`, { method: 'DELETE' }),
    setTags: (id: string, tags: string[]) =>
      request<Receipt>(`/api/receipts/${id}`, { method: 'PATCH', body: JSON.stringify({ tags }) }),
    tags: () => request<TagSummary[]>('/api/receipts/tags'),
    renameTag: (from: string, to: string) =>
      request<TagSummary[]>('/api/receipts/tags/rename', {
        method: 'POST',
        body: JSON.stringify({ from, to }),
      }),
    removeTag: (tag: string) =>
      request<void>(`/api/receipts/tags/${encodeURIComponent(tag)}`, { method: 'DELETE' }),
    uploadPhoto: async (id: string, file: File) => {
      const form = new FormData()
      form.append('file', file)
      // Not request() — FormData needs the browser to set its own multipart
      // Content-Type (with boundary); request() always forces application/json.
      const res = await fetch(`${BASE}/api/receipts/${id}/photo`, { method: 'POST', body: form })
      if (!res.ok) {
        let detail = res.statusText
        try {
          detail = (await res.json()).detail ?? detail
        } catch {
          /* ignore */
        }
        throw new Error(detail)
      }
      return res.json() as Promise<Receipt>
    },
    removePhoto: (id: string) => request<Receipt>(`/api/receipts/${id}/photo`, { method: 'DELETE' }),
    uploadWarranty: async (id: string, file: File) => {
      const form = new FormData()
      form.append('file', file)
      // Not request() — same reason as uploadPhoto above.
      const res = await fetch(`${BASE}/api/receipts/${id}/warranty`, { method: 'POST', body: form })
      if (!res.ok) {
        let detail = res.statusText
        try {
          detail = (await res.json()).detail ?? detail
        } catch {
          /* ignore */
        }
        throw new Error(detail)
      }
      return res.json() as Promise<Receipt>
    },
    removeWarranty: (id: string) => request<Receipt>(`/api/receipts/${id}/warranty`, { method: 'DELETE' }),
    shops: () => request<ShopSuggestion[]>('/api/receipts/shops'),
  },
  dashboard: {
    stats: (params?: DashboardParams) => request<DashboardStats>(`/api/dashboard${qs(params)}`),
    byStore: (params?: DashboardParams) => request<StoreBreakdown[]>(`/api/dashboard/by-store${qs(params)}`),
    byChannel: (params?: DashboardParams) =>
      request<ChannelBreakdown[]>(`/api/dashboard/by-channel${qs(params)}`),
    timeseries: (params?: DashboardParams) =>
      request<TimeseriesPoint[]>(`/api/dashboard/timeseries${qs(params)}`),
    byCategory: (params?: DashboardParams) =>
      request<CategorySpend[]>(`/api/dashboard/by-category${qs(params)}`),
    storeCategory: (params?: DashboardParams) =>
      request<StoreCategoryMatrix>(`/api/dashboard/store-category${qs(params)}`),
    storeCategoryItems: (params: DashboardParams & { store_code: string; category_id?: string }) =>
      request<StoreCategoryItem[]>(`/api/dashboard/store-category/items${qs(params)}`),
    fuelPrices: () => request<AccountFuelPrices[]>('/api/dashboard/fuel-prices'),
    fuelPriceHistory: (params?: { account_id?: string; date_from?: string; date_to?: string; limit?: number }) =>
      request<FuelPriceHistory[]>(`/api/dashboard/fuel-prices/history${qs(params)}`),
  },
  widgets: {
    list: () => request<WidgetMeta[]>('/api/widgets'),
    get: (key: string, params?: DashboardParams) =>
      request<WidgetSnapshot>(`/api/widgets/${key}${qs(params)}`),
  },
  settings: {
    get: <T = unknown>(key: string) => request<AppSetting<T>>(`/api/settings/${key}`),
    set: <T = unknown>(key: string, value: T) =>
      request<AppSetting<T>>(`/api/settings/${key}`, {
        method: 'PUT',
        body: JSON.stringify({ value }),
      }),
  },
  ai: {
    get: () => request<AiSettings>('/api/settings/ai'),
    save: (config: AiConfig) =>
      request<AiSettings>('/api/settings/ai', { method: 'PUT', body: JSON.stringify(config) }),
    setCloudKey: (provider: string, apiKey: string | null) =>
      request<AiSettings>('/api/settings/ai/cloud-key', {
        method: 'PUT',
        body: JSON.stringify({ provider, api_key: apiKey }),
      }),
    check: () => request<AiCheckResult>('/api/settings/ai/check', { method: 'POST' }),
    testKey: (
      provider: string,
      apiKey?: string | null,
      model?: string | null,
      workspaceId?: string | null,
      baseUrl?: string | null,
      purpose: 'chat' | 'stt' = 'chat',
    ) =>
      request<AiBackendCheck>('/api/settings/ai/test-key', {
        method: 'POST',
        body: JSON.stringify({
          provider,
          purpose,
          api_key: apiKey ?? null,
          model: model ?? null,
          workspace_id: workspaceId ?? null,
          base_url: baseUrl ?? null,
        }),
      }),
    models: () => request<AiModelChoices>('/api/settings/ai/models'),
    providerModels: (provider: string) =>
      request<AiProviderModels>(`/api/settings/ai/models/${encodeURIComponent(provider)}`),
    log: () => request<{ entries: AiLogEntry[] }>('/api/settings/ai/log'),
  },
  taxCabinet: {
    get: () => request<TaxCabinetSettings>('/api/settings/tax-cabinet'),
    save: (token?: string | null, clear = false) =>
      request<TaxCabinetSettings>('/api/settings/tax-cabinet', {
        method: 'PUT',
        body: JSON.stringify({ token: token ?? null, clear }),
      }),
  },
  fiscalQueue: {
    list: () => request<any[]>('/api/fiscal-queue'),
    add: (url: string) => request<any>('/api/fiscal-queue', { method: 'POST', body: JSON.stringify({ url }) }),
    resolve: (url: string, scanJobId?: string) =>
      request<any>('/api/fiscal-queue/resolve', {
        method: 'POST',
        body: JSON.stringify({ url, scan_job_id: scanJobId ?? null }),
      }),
    importResponse: (value: string, scanJobId?: string) =>
      request<any>('/api/fiscal-queue/import-response', {
        method: 'POST',
        body: JSON.stringify({ value, scan_job_id: scanJobId ?? null }),
      }),
    importResponseFile: (file: File, scanJobId?: string) => {
      const body = new FormData()
      body.append('file', file)
      // Ties the import back to the inbox row it was started from, so that row
      // can show the receipt instead of staying on "needs_review".
      if (scanJobId) body.append('scan_job_id', scanJobId)
      return request<any>('/api/fiscal-queue/import-response-file', { method: 'POST', headers: {}, body })
    },
    run: (id: string) => request<any>(`/api/fiscal-queue/${id}/run`, { method: 'POST' }),
    runPublicApi: (id: string) => request<any>(`/api/fiscal-queue/${id}/api`, { method: 'POST' }),
    startBrowser: (id: string) => request<any>(`/api/fiscal-queue/${id}/browser/start`, { method: 'POST' }),
    screenshot: (id: string, sid: string) => `${BASE}/api/fiscal-queue/${id}/browser/screenshot?session_id=${encodeURIComponent(sid)}`,
    click: (id: string, sid: string, x: number, y: number) => request(`/api/fiscal-queue/${id}/browser/click?session_id=${encodeURIComponent(sid)}`, { method: 'POST', body: JSON.stringify({ x, y }) }),
    reload: (id: string, sid: string) => request<any>(`/api/fiscal-queue/${id}/browser/reload?session_id=${encodeURIComponent(sid)}`, { method: 'POST' }),
    state: (id: string, sid: string) => request<any>(`/api/fiscal-queue/${id}/browser/state?session_id=${encodeURIComponent(sid)}`),
    check: (id: string, sid: string) => request<any>(`/api/fiscal-queue/${id}/browser/check?session_id=${encodeURIComponent(sid)}`, { method: 'POST' }),
  },
  duplicates: {
    run: () => request<DuplicateRun>('/api/duplicates/run', { method: 'POST' }),
    runs: () => request<DuplicateRun[]>('/api/duplicates/runs'),
    groups: (status?: string) => request<DuplicateGroup[]>(`/api/duplicates/groups${qs({ status })}`),
    get: (id: string) => request<DuplicateGroup>(`/api/duplicates/groups/${id}`),
    merge: (id: string, choices?: DuplicateMergeChoices) =>
      request<DuplicateGroup>(`/api/duplicates/groups/${id}/merge`, {
        method: 'POST',
        body: choices ? JSON.stringify(choices) : undefined,
      }),
    reject: (id: string) => request<DuplicateGroup>(`/api/duplicates/groups/${id}/reject`, { method: 'POST' }),
    rollback: (id: string) => request<DuplicateGroup>(`/api/duplicates/groups/${id}/rollback`, { method: 'POST' }),
  },
  telegram: {
    get: () => request<TelegramSettings>('/api/settings/telegram'),
    save: (payload: {
      enabled: boolean
      bot_token?: string | null
      clear_token?: boolean
      public_url?: string
      capture_enabled?: boolean
    }) => request<TelegramSettings>('/api/settings/telegram', { method: 'PUT', body: JSON.stringify(payload) }),
    approveChat: (chatId: number) =>
      request<TelegramSettings>(`/api/settings/telegram/chats/${chatId}/approve`, { method: 'POST' }),
    rejectChat: (chatId: number) =>
      request<TelegramSettings>(`/api/settings/telegram/chats/${chatId}/request`, { method: 'DELETE' }),
    revokeChat: (chatId: number) =>
      request<TelegramSettings>(`/api/settings/telegram/chats/${chatId}`, { method: 'DELETE' }),
    messages: (conversationKey?: string) =>
      request<TelegramMessageLogList>(
        `/api/telegram/messages${qs({ conversation_key: conversationKey, limit: 100 })}`,
      ),
  },
  mcp: {
    get: () => request<McpSettings>('/api/settings/mcp'),
    save: (config: McpConfig) =>
      request<McpSettings>('/api/settings/mcp', { method: 'PUT', body: JSON.stringify(config) }),
  },
  products: {
    search: (q: string) => request<ProductPrice[]>(`/api/products/search${qs({ q })}`),
    liveSearch: (q: string, stores?: string[]) =>
      request<LiveProduct[]>(
        `/api/products/live-search${qs({ q, stores: stores && stores.length ? stores.join(',') : undefined })}`,
      ),
  },
  warranties: {
    list: () => request<WarrantyProduct[]>('/api/warranties'),
  },
  voice: {
    list: () => request<VoiceMessage[]>('/api/voice'),
    audioUrl: (id: string) => `${BASE}/api/voice/${id}/audio`,
    // `language` (BCP-47) forces one of the configured dictation languages —
    // the retry for a phrase said in the other one.
    transcribe: (id: string, language?: string) =>
      request<VoiceMessage>(
        `/api/voice/${id}/transcribe${language ? `?language=${encodeURIComponent(language)}` : ''}`,
        { method: 'POST' },
      ),
    apply: (id: string) => request<VoiceMessage>(`/api/voice/${id}/apply`, { method: 'POST' }),
    remove: (id: string) => request<void>(`/api/voice/${id}`, { method: 'DELETE' }),
  },
  inbox: {
    upload: async (file: File) => {
      const form = new FormData()
      form.append('file', file)
      const res = await fetch(`${BASE}/api/inbox/upload`, { method: 'POST', body: form })
      if (!res.ok) {
        let detail = res.statusText
        try {
          detail = (await res.json()).detail ?? detail
        } catch {
          /* ignore */
        }
        throw new Error(detail)
      }
      return res.json() as Promise<UploadResult>
    },
    /** Several overlapping photos of one long receipt → one receipt. Order is
     *  the order they were shot in; `scanJobId` continues an existing scan. */
    uploadSeries: async (files: File[], scanJobId?: string) => {
      const form = new FormData()
      for (const file of files) form.append('files', file)
      if (scanJobId) form.append('scan_job_id', scanJobId)
      const res = await fetch(`${BASE}/api/inbox/upload-series`, { method: 'POST', body: form })
      if (!res.ok) {
        let detail = res.statusText
        try {
          detail = (await res.json()).detail ?? detail
        } catch {
          /* ignore */
        }
        throw new Error(detail)
      }
      return res.json() as Promise<SeriesUploadResult>
    },
    list: (includeDone = false) =>
      request<InboxDocument[]>(`/api/inbox${qs({ include_done: includeDone ? 'true' : undefined })}`),
    process: (id: string, payload: { final_name: string; target_folder_id?: string | null }) =>
      request<InboxDocument>(`/api/inbox/${id}/process`, {
        method: 'POST',
        body: JSON.stringify(payload),
      }),
    remove: (id: string) => request<void>(`/api/inbox/${id}`, { method: 'DELETE' }),
    scans: (includeArchived = false) =>
      request<ScanJob[]>(`/api/inbox/scans?include_archived=${includeArchived}`),
    saveDraft: (jobId: string) =>
      request<ScanJob>(`/api/inbox/scans/${jobId}/save-draft`, { method: 'POST' }),
    archiveScan: (jobId: string, archived = true) =>
      request<ScanJob>(`/api/inbox/scans/${jobId}/archive?archived=${archived}`, { method: 'POST' }),
    scanStats: () => request<ScanStats>('/api/inbox/scans/stats'),
    removeScan: (jobId: string, withReceipt = false) =>
      request<void>(`/api/inbox/scans/${jobId}?with_receipt=${withReceipt}`, { method: 'DELETE' }),
    rescan: (jobId: string, ai = false, replace = false) =>
      request<UploadResult>(
        `/api/inbox/scans/${jobId}/rescan?ai=${ai}&replace=${replace}`,
        { method: 'POST' },
      ),
    /** Answer the question a batch asked: one receipt, or several. */
    resolveSeries: (jobId: string, choice: 'one' | 'separate') =>
      request<SeriesUploadResult>(
        `/api/inbox/scans/${jobId}/resolve-series?choice=${choice}`,
        { method: 'POST' },
      ),
    /** One action over several journal rows; each row reports its own result. */
    bulkScans: (
      ids: string[],
      action: 'archive' | 'unarchive' | 'delete' | 'rescan' | 'rescan_ai' | 'save_draft',
      withReceipt = false,
    ) =>
      request<BulkScanOut>('/api/inbox/scans/bulk', {
        method: 'POST',
        body: JSON.stringify({ ids, action, with_receipt: withReceipt }),
      }),
    config: () => request<InboxConfig>('/api/inbox/config'),
    saveConfig: (cfg: InboxConfig) =>
      request<InboxConfig>('/api/inbox/config', { method: 'PUT', body: JSON.stringify(cfg) }),
    driveConfig: () => request<DriveConfig>('/api/inbox/drive-config'),
    saveDriveConfig: (cfg: DriveConfig) =>
      request<DriveConfig>('/api/inbox/drive-config', { method: 'PUT', body: JSON.stringify(cfg) }),
    driveStatus: () => request<DriveStatus>('/api/inbox/drive/status'),
    driveSaveClient: (client_id: string, client_secret: string | null) =>
      request<DriveStatus>('/api/inbox/drive/client', {
        method: 'PUT',
        body: JSON.stringify({ client_id, client_secret }),
      }),
    driveAuthorize: () => request<{ authorize_url: string }>('/api/inbox/drive/authorize'),
    driveDisconnect: () => request<DriveStatus>('/api/inbox/drive/disconnect', { method: 'POST' }),
    driveFolders: (parent?: string) =>
      request<DriveFolder[]>(`/api/inbox/drive-tree${qs({ parent })}`),
    driveCreateFolder: (name: string, parent_id?: string | null, color?: string | null) =>
      request<DriveFolder>('/api/inbox/drive-folder', {
        method: 'POST',
        body: JSON.stringify({ name, parent_id: parent_id ?? null, color: color ?? null }),
      }),
    driveSetFolders: (payload: DriveFoldersIn) =>
      request<DriveStatus>('/api/inbox/drive/folders', {
        method: 'PUT',
        body: JSON.stringify(payload),
      }),
    driveColorPalette: () => request<DriveColorPalette>('/api/inbox/drive/color-palette'),
    driveSetFolderColor: (folder_id: string, color: string | null) =>
      request<DriveStatus>('/api/inbox/drive/folder-color', {
        method: 'PUT',
        body: JSON.stringify({ folder_id, color }),
      }),
  },
  categories: {
    list: () => request<Category[]>('/api/categories'),
    stats: () => request<CategoryStats>('/api/categories/stats'),
    create: (payload: { name: string; parent_id?: string | null }) =>
      request<Category>('/api/categories', { method: 'POST', body: JSON.stringify(payload) }),
    update: (id: string, payload: { name?: string; parent_id?: string | null; sort_order?: number }) =>
      request<Category>(`/api/categories/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
    remove: (id: string) => request<void>(`/api/categories/${id}`, { method: 'DELETE' }),
    backfill: (force = false) =>
      request<{
        names_classified: number
        batches: number
        reset: { aliases_cleared: number; items_reapplied: number } | null
      }>(`/api/categories/backfill${force ? '?force=true' : ''}`, { method: 'POST' }),
    manualPrompt: (limit = 40, batchSize = 40) =>
      request<ManualPrompt>(`/api/categories/manual-prompt${qs({ limit, batch_size: batchSize })}`),
    reset: () =>
      request<{ aliases_cleared: number; items_cleared: number }>(
        '/api/categories/reset',
        { method: 'POST' },
      ),
    manualApply: (result: string, names?: string[]) =>
      request<ManualApplyResult>('/api/categories/manual-apply', {
        method: 'POST',
        body: JSON.stringify({ result, names }),
      }),
  },
}
