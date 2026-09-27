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
  AiPrompt,
  AiPromptRevision,
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
  Health,
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
  ReceiptProblemAction,
  ReceiptProblemBulkResult,
  RawResponseCleanupResult,
  RawResponseSettings,
  RawResponseSummary,
  ShopSuggestion,
  BankTransaction,
  BankTransactionSummaryRow,
  TokenTestResult,
  BankTransactionRule,
  TagSummary,
  TaxCabinetSettings,
  TelegramSettings,
  TelegramMessageLogList,
  Store,
  StoreClass,
  ShoppingSettings,
  FiscalJob,
  LogPage,
  NavBadges,
  Problems,
  SavingsReceipt,
  StoreBreakdown,
  CategoryItems,
  CategoryItemsReassignResult,
  StoreCategoryItem,
  StoreCategoryMatrix,
  BulkScanOut,
  SeriesUploadResult,
  TimeseriesPoint,
  VoiceMessage,
  WarrantyProduct,
  WidgetMeta,
  WidgetSnapshot,
  XmlImportResult,
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
  health: {
    get: () => request<Health>('/api/health'),
  },
  stores: {
    list: () => request<Store[]>('/api/stores'),
    classes: () => request<StoreClass[]>('/api/stores/classes'),
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
    resetError: (id: string) => request<Account>(`/api/accounts/${id}/reset-error`, { method: 'POST' }),
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
    // 'prom', 'rozetka' or 'olx'. AliExpress passes a password on start; marketplace
    // buyer accounts use the shared RID phone + SMS flow.
    browserLoginStart: (
      seg: 'aliexpress' | 'prom' | 'rozetka' | 'olx', id: string, opts: { password?: string; sessionId?: string } = {},
    ) =>
      request<AliLoginStep>(`/api/accounts/${id}/${seg}/login/start`, {
        method: 'POST',
        body: JSON.stringify(
          seg === 'aliexpress'
            ? { password: opts.password ?? '', session_id: opts.sessionId }
            : { session_id: opts.sessionId },
        ),
      }),
    browserLoginStatus: (seg: 'aliexpress' | 'prom' | 'rozetka' | 'olx', id: string, sessionId: string) =>
      request<AliLoginStep>(`/api/accounts/${id}/${seg}/login/${sessionId}/status`),
    browserLoginScreenshotUrl: (seg: 'aliexpress' | 'prom' | 'rozetka' | 'olx', id: string, sessionId: string) =>
      `${BASE}/api/accounts/${id}/${seg}/login/${sessionId}/screenshot`,
    browserLoginClick: (seg: 'aliexpress' | 'prom' | 'rozetka' | 'olx', id: string, sessionId: string, x: number, y: number) =>
      request<void>(`/api/accounts/${id}/${seg}/login/${sessionId}/click`, {
        method: 'POST', body: JSON.stringify({ x, y }),
      }),
    browserLoginPointer: (
      seg: 'aliexpress' | 'prom' | 'rozetka' | 'olx', id: string, sessionId: string,
      action: 'down' | 'move' | 'up', x: number, y: number,
    ) => request<void>(`/api/accounts/${id}/${seg}/login/${sessionId}/pointer`, {
      method: 'POST', body: JSON.stringify({ action, x, y }),
    }),
    browserLoginTakeover: (seg: 'aliexpress' | 'prom' | 'rozetka' | 'olx', id: string, sessionId: string) =>
      request<AliLoginStep>(`/api/accounts/${id}/${seg}/login/${sessionId}/takeover`, { method: 'POST' }),
    browserLoginKey: (
      seg: 'aliexpress' | 'prom' | 'rozetka' | 'olx', id: string, sessionId: string,
      payload: { key: string; alt: boolean; ctrl: boolean; meta: boolean; shift: boolean },
    ) => request<void>(`/api/accounts/${id}/${seg}/login/${sessionId}/key`, {
      method: 'POST', body: JSON.stringify(payload),
    }),
    browserLoginText: (seg: 'aliexpress' | 'prom' | 'rozetka' | 'olx', id: string, sessionId: string, value: string) =>
      request<void>(`/api/accounts/${id}/${seg}/login/${sessionId}/text`, {
        method: 'POST', body: JSON.stringify({ value }),
      }),
    browserLoginCheck: (seg: 'aliexpress' | 'prom' | 'rozetka' | 'olx', id: string, sessionId: string) =>
      request<AliLoginStep>(`/api/accounts/${id}/${seg}/login/${sessionId}/check`, { method: 'POST' }),
    browserLoginOtp: (seg: 'aliexpress' | 'prom' | 'rozetka' | 'olx', id: string, sessionId: string, code: string) =>
      request<AliLoginStep>(`/api/accounts/${id}/${seg}/login/otp`, {
        method: 'POST', body: JSON.stringify({ session_id: sessionId, code }),
      }),
    browserLoginClose: (seg: 'aliexpress' | 'prom' | 'rozetka' | 'olx', id: string, sessionId: string) =>
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
    problemAction: (id: string, action: 'ignore' | 'unignore') =>
      request<{ reconciliation_ignored_at: string | null }>(
        `/api/receipts/${id}/problem?action=${action}`,
        { method: 'POST' },
      ),
    bulkProblemAction: (receiptIds: string[], action: ReceiptProblemAction) =>
      request<ReceiptProblemBulkResult>('/api/receipts/bulk/problem-actions', {
        method: 'POST',
        body: JSON.stringify({ receipt_ids: receiptIds, action }),
      }),
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
    categorizeItem: (receiptId: string, itemId: string, clarification: string) =>
      request<Receipt>(`/api/receipts/${receiptId}/items/${itemId}/categorize`, {
        method: 'POST',
        body: JSON.stringify({ clarification }),
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
    uploadItemFile: async (
      receiptId: string,
      itemId: string,
      file: File,
      kind: 'warranty' | 'photo' = 'warranty',
    ) => {
      const form = new FormData()
      form.append('file', file)
      // Not request() — same reason as uploadPhoto above.
      const res = await fetch(
        `${BASE}/api/receipts/${receiptId}/items/${itemId}/files?kind=${kind}`,
        { method: 'POST', body: form },
      )
      if (!res.ok) {
        let detail = res.statusText
        try {
          detail = (await res.json()).detail ?? detail
        } catch {
          /* ignore */
        }
        throw new Error(detail)
      }
      return res.json() as Promise<ReceiptItem>
    },
    linkItemFile: (
      receiptId: string,
      itemId: string,
      url: string,
      kind: 'warranty' | 'photo' = 'warranty',
    ) =>
      request<ReceiptItem>(`/api/receipts/${receiptId}/items/${itemId}/files/link`, {
        method: 'POST',
        body: JSON.stringify({ url, kind }),
      }),
    removeItemFile: (receiptId: string, itemId: string, fileId: string) =>
      request<ReceiptItem>(`/api/receipts/${receiptId}/items/${itemId}/files/${fileId}`, {
        method: 'DELETE',
      }),
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
    savingsReceipts: (
      params: DashboardParams & { store_code?: string; sort?: string; limit?: string },
    ) => request<SavingsReceipt[]>(`/api/dashboard/savings/receipts${qs(params)}`),
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
  nav: {
    badges: () => request<NavBadges>('/api/nav/badges'),
  },
  rawResponses: {
    summary: () => request<RawResponseSummary>('/api/raw-responses/summary'),
    settings: () => request<RawResponseSettings>('/api/raw-responses/settings'),
    saveSettings: (payload: RawResponseSettings) =>
      request<RawResponseSettings>('/api/raw-responses/settings', {
        method: 'PUT',
        body: JSON.stringify(payload),
      }),
    cleanup: (mode: 'all' | 'retention') =>
      request<RawResponseCleanupResult>('/api/raw-responses/cleanup', {
        method: 'POST',
        body: JSON.stringify({ mode }),
      }),
  },
  logs: {
    list: (params: { source?: string; level?: string; q?: string; limit?: number; offset?: number }) =>
      request<LogPage>(`/api/logs${qs(params)}`),
    problems: () => request<Problems>('/api/logs/problems'),
  },
  shoppingSettings: {
    get: () => request<ShoppingSettings>('/api/shopping-list/settings'),
    save: (value: ShoppingSettings) =>
      request<ShoppingSettings>('/api/shopping-list/settings', {
        method: 'PUT',
        body: JSON.stringify(value),
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
    prompts: () => request<AiPrompt[]>('/api/settings/prompts'),
    promptHistory: (key: string) =>
      request<AiPromptRevision[]>(`/api/settings/prompts/${encodeURIComponent(key)}/history`),
    savePrompt: (key: string, content: string, note?: string) =>
      request<AiPrompt>(`/api/settings/prompts/${encodeURIComponent(key)}`, {
        method: 'PUT', body: JSON.stringify({ content, note: note || null }),
      }),
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
    list: (openOnly = false) => request<FiscalJob[]>(`/api/fiscal-queue?open_only=${openOnly}`),
    add: (url: string) => request<any>('/api/fiscal-queue', { method: 'POST', body: JSON.stringify({ url }) }),
    resolve: (url: string, scanJobId?: string) =>
      request<any>('/api/fiscal-queue/resolve', {
        method: 'POST',
        body: JSON.stringify({ url, scan_job_id: scanJobId ?? null }),
      }),
    // One entry point for any ДПС XML: with a row named it is checked against
    // that row, without one it finds its own place (or gets a new inbox row).
    importResponse: (value: string, scanJobId?: string, fiscalJobId?: string) =>
      request<XmlImportResult>('/api/fiscal-queue/import-response', {
        method: 'POST',
        body: JSON.stringify({
          value,
          scan_job_id: scanJobId ?? null,
          fiscal_job_id: fiscalJobId ?? null,
        }),
      }),
    importResponseFile: (file: File, scanJobId?: string, fiscalJobId?: string) => {
      const body = new FormData()
      body.append('file', file)
      // Ties the import back to the inbox row it was started from, so that row
      // can show the receipt instead of staying on "needs_review".
      if (scanJobId) body.append('scan_job_id', scanJobId)
      if (fiscalJobId) body.append('fiscal_job_id', fiscalJobId)
      return request<XmlImportResult>('/api/fiscal-queue/import-response-file', { method: 'POST', headers: {}, body })
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
        `/api/telegram/messages${qs({ conversation_key: conversationKey, limit: 200 })}`,
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
  bankTransactions: {
    list: (params: { status?: string; accountId?: string } = {}) =>
      request<BankTransaction[]>(
        `/api/bank-transactions${qs({ status: params.status, account_id: params.accountId })}`,
      ),
    summary: () => request<BankTransactionSummaryRow[]>('/api/bank-transactions/summary'),
    // `since` (ISO datetime) requests a deep-history backfill instead of the
    // quick incremental poll — the backend runs it in the background and
    // responds with { started: true } right away instead of the usual
    // fetched/created/matched_checked counts.
    poll: (accountId: string, since?: string) =>
      request<{ fetched: number; created: number; matched_checked: number } | { started: true }>(
        `/api/bank-transactions/accounts/${accountId}/poll`,
        { method: 'POST', body: JSON.stringify({ since: since ?? null }) },
      ),
    testToken: (accountId: string) => request<TokenTestResult>(
      `/api/bank-transactions/accounts/${accountId}/test-token`, { method: 'POST' },
    ),
    link: (id: string, receiptId: string) =>
      request<BankTransaction>(`/api/bank-transactions/${id}/link`, {
        method: 'POST', body: JSON.stringify({ receipt_id: receiptId }),
      }),
    unlink: (id: string) =>
      request<BankTransaction>(`/api/bank-transactions/${id}/unlink`, { method: 'POST' }),
    ignore: (id: string, reason: string) =>
      request<BankTransaction>(`/api/bank-transactions/${id}/ignore`, {
        method: 'POST', body: JSON.stringify({ reason }),
      }),
    setCategory: (id: string, categoryOverrideId: string | null) =>
      request<BankTransaction>(`/api/bank-transactions/${id}`, {
        method: 'PATCH', body: JSON.stringify({ category_override_id: categoryOverrideId }),
      }),
  },
  bankTransactionRules: {
    list: () => request<BankTransactionRule[]>('/api/bank-transaction-rules'),
    create: (payload: Omit<BankTransactionRule, 'id' | 'updated_at'>) =>
      request<BankTransactionRule>('/api/bank-transaction-rules', {
        method: 'POST', body: JSON.stringify(payload),
      }),
    update: (id: string, payload: Partial<Omit<BankTransactionRule, 'id' | 'updated_at'>>) =>
      request<BankTransactionRule>(`/api/bank-transaction-rules/${id}`, {
        method: 'PATCH', body: JSON.stringify(payload),
      }),
    remove: (id: string) => request<void>(`/api/bank-transaction-rules/${id}`, { method: 'DELETE' }),
    apply: () => request<{ updated: number }>('/api/bank-transaction-rules/apply', { method: 'POST' }),
  },
  voice: {
    list: (includeArchived = false) =>
      request<VoiceMessage[]>(`/api/voice?include_archived=${includeArchived}`),
    archive: (id: string, archived = true) =>
      request<VoiceMessage>(`/api/voice/${id}/archive?archived=${archived}`, { method: 'POST' }),
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
    /**
     * Re-run recognition on the stored photo. `escalate` resumes the AI route
     * after the step this scan already used, reusing its QR verdict instead of
     * paying for the ladder and the spent model a second time.
     */
    rescan: (jobId: string, ai = false, replace = false, escalate = false) =>
      request<UploadResult>(
        `/api/inbox/scans/${jobId}/rescan?ai=${ai}&replace=${replace}&escalate=${escalate}`,
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
    backfill: (force = false, includeOther = false) =>
      request<{
        names_classified: number
        batches: number
        queued?: boolean
        reset: { aliases_cleared: number; items_reapplied: number } | null
      }>(
        `/api/categories/backfill${qs({ force: force ? 'true' : undefined, include_other: includeOther ? 'true' : undefined })}`,
        { method: 'POST' },
      ),
    manualPrompt: (limit = 40, batchSize = 40, includeOther = false) =>
      request<ManualPrompt>(
        `/api/categories/manual-prompt${qs({ limit, batch_size: batchSize, include_other: includeOther ? 'true' : undefined })}`,
      ),
    reset: () =>
      request<{ aliases_cleared: number; items_cleared: number }>(
        '/api/categories/reset',
        { method: 'POST' },
      ),
    manualApply: (result: string, names?: string[], includeOther = false) =>
      request<ManualApplyResult>('/api/categories/manual-apply', {
        method: 'POST',
        body: JSON.stringify({ result, names, include_other: includeOther }),
      }),
    items: (categoryId: string, includeDescendants = false) =>
      request<CategoryItems>(
        `/api/categories/${categoryId}/items${qs({ include_descendants: includeDescendants ? 'true' : undefined })}`,
      ),
    reassignItems: (payload: {
      category_id: string
      normalized_names: string[]
      target_category_id: string
      include_descendants?: boolean
    }) =>
      request<CategoryItemsReassignResult>('/api/categories/items/reassign', {
        method: 'POST',
        body: JSON.stringify(payload),
      }),
  },
}
