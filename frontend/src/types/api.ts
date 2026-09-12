// What this store's adapter actually implements — mirrors BaseAdapter.capabilities
// on the backend (app/adapters/base.py). Use this instead of hardcoding store
// codes in the UI to decide whether to show a button/column.
export interface StoreCapabilities {
  receipts: boolean
  manual_receipts: boolean
  phone_otp_login: boolean
  phone_password_login: boolean
  oauth_browser_login: boolean
  card_number_login: boolean
  refresh: boolean
  manual_import: boolean
  loyalty_code: boolean
  balance: boolean
  fuel_prices: boolean
  catalog_search: boolean
  warranty_document: boolean
}

export interface Store {
  id: string
  code: string
  name: string
  auth_type: 'phone_otp' | 'phone_password' | 'phone_call' | 'device_uuid' | 'manual' | 'oauth_browser' | 'card_number'
  enabled: boolean
  network_type: 'supermarket' | 'azs' | 'tech' | 'pharmacy' | 'beauty' | 'home' | 'other' | null
  capabilities: StoreCapabilities
}

export interface Category {
  id: string
  parent_id: string | null
  name: string
  slug: string
  path: string
  sort_order: number
}

export interface CategoryStat {
  category_id: string
  direct_items: number
  rollup_items: number
}

export interface CategoryStats {
  total_items: number
  uncategorized_items: number
  classified_names: number
  per_category: CategoryStat[]
}

export interface CategorySpend {
  category_id: string | null
  path: string
  name: string
  depth: number
  direct_total: number
  rollup_total: number
  item_count: number
}

// Manual "bring your own AI" classification (api/categories.py).
export interface ManualBatch {
  index: number
  count: number
  filename: string
  prompt: string
}

export interface ManualPrompt {
  count: number
  names: string[]
  system: string
  prompt: string
  filename: string
  preamble: string
  batches: ManualBatch[]
}

export interface ManualApplyResult {
  names: number
  mapped: number
  unknown_path: number
  low_confidence: number
  no_guess: number
  skipped_manual: number
  items_tagged: number
}

// GET /api/dashboard/store-category — spend cross-tab of store × category branch.
export interface StoreCategoryCell {
  store_code: string
  total: number
  item_count: number
}

export interface StoreCategoryRow {
  category_id: string | null
  path: string
  name: string
  depth: number
  total: number
  item_count: number
  per_store: StoreCategoryCell[]
}

export interface StoreCategoryMatrix {
  stores: StoreBreakdown[]
  categories: StoreCategoryRow[]
}

export interface StoreCategoryItem {
  receipt_id: string
  item_id: string
  name: string
  quantity: number
  unit: string | null
  unit_price: number | null
  total_price: number
  discount: number
  net: number
  purchased_at: string
  store_code: string
  category_id: string | null
  category_path: string | null
}

export interface ScheduleOut {
  id: string
  enabled: boolean
  cron_expression: string
  last_run_at: string | null
  next_run_at: string | null
}

export type AccountStatus = 'pending_otp' | 'active' | 'needs_reauth' | 'error'

export interface Account {
  id: string
  label: string
  phone_number: string | null
  status: AccountStatus
  last_error: string | null
  created_at: string
  store: Store
  schedule: ScheduleOut | null
  receipt_count: number
  last_collected_at: string | null
  bonus_balance: number | null
  bonus_balance_updated_at: string | null
  discount_percent: number | null
}

export interface CollectionRun {
  id: string
  trigger: 'manual' | 'schedule' | 'login' | 'import'
  status: 'running' | 'success' | 'partial' | 'error'
  started_at: string
  finished_at: string | null
  receipts_found: number
  receipts_new: number
  error_message: string | null
}

export interface DuplicateRun {
  id: string
  trigger: 'manual' | 'schedule'
  status: 'running' | 'success' | 'error'
  started_at: string
  finished_at: string | null
  scanned_receipts: number
  candidates_found: number
  candidates_new: number
  error_message: string | null
}

export interface DuplicateReceiptItem {
  name: string
  quantity: number
  unit: string | null
  unit_price: number | null
  total_price: number
  discount: number
  barcode: string | null
  category_id: string | null
  product_url: string | null
  product_image_url: string | null
  merchant_name: string | null
}

export interface DuplicateMergeItemSource {
  receipt_id: string
  store_code: string | null
  name: string
  quantity: number
  unit: string | null
  unit_price: number | null
  total_price: number
  discount: number
  barcode: string | null
  category_id: string | null
  product_url: string | null
  product_image_url: string | null
  merchant_name: string | null
}

export interface DuplicateMergeItemDecision {
  index: number
  matched: number
  result: DuplicateMergeItemSource
  sources: DuplicateMergeItemSource[]
  identity_from_receipt_id: string
  name_from_receipt_id: string
  barcode_from_receipt_id: string
  money_from_receipt_id: string
  price_from_receipt_id: string
  discount_from_receipt_id: string
  changed_fields: string[]
}

export interface DuplicateMergeChoices {
  receipt_fields: Record<string, string>
  item_name: Record<string, string>
  item_barcode: Record<string, string>
  item_price: Record<string, string>
  item_discount: Record<string, string>
  item_identity: Record<string, string>
  item_money: Record<string, string>
}

export interface DuplicateMergeFieldSource {
  receipt_id: string
  store_code: string
  value: unknown
}

export interface DuplicateMergeFieldDecision {
  field: string
  result: unknown
  from_receipt_id: string
  sources: DuplicateMergeFieldSource[]
  changed: boolean
}

export interface DuplicateMergePlan {
  canonical_receipt_id: string
  base_items_receipt_id: string
  warnings: string[]
  fields: DuplicateMergeFieldDecision[]
  items: DuplicateMergeItemDecision[]
}

export interface DuplicateReceipt {
  id: string
  account_id: string
  account_label: string
  store_code: string
  external_id: string
  purchased_at: string
  total_amount: number
  currency: string
  exchange_rate_to_uah: number | null
  shop_name: string | null
  shop_address: string | null
  payment_type: string | null
  channel: string
  receipt_url: string | null
  tags: string[]
  items: DuplicateReceiptItem[]
  raw: Record<string, unknown>
}

export interface DuplicateGroup {
  id: string
  status: 'pending' | 'auto_merged' | 'needs_review' | 'rejected' | 'rolled_back'
  score: number
  reasons: string[]
  receipt_ids: string[]
  canonical_receipt_id: string | null
  first_seen_at: string
  last_seen_at: string
  merged_at: string | null
  rolled_back_at: string | null
  merge_log: Record<string, unknown>[]
  merge_plan: DuplicateMergePlan | null
  receipts: DuplicateReceipt[]
}

export interface AliLoginStep {
  session_id: string | null
  step: string
  message: string | null
  run: CollectionRun | null
}

export interface ReceiptItem {
  id: string
  name: string
  barcode: string | null
  category_raw: string | null
  category_id: string | null
  category_path?: string | null
  quantity: number
  unit: string | null
  unit_price: number | null
  total_price: number
  discount: number
  product_url?: string | null
  product_image_url?: string | null
  merchant_name?: string | null
  product_image_urls?: string[]
  has_warranty?: boolean | null
  warranty_text?: string | null
  warranty_expires_at?: string | null
  warranty_checked_at?: string | null
}

export interface Receipt {
  id: string
  account_id: string
  store_code: string
  external_id: string
  purchased_at: string
  total_amount: number
  currency: string
  exchange_rate_to_uah: number | null
  shop_name: string | null
  shop_address: string | null
  payment_type: string | null
  channel: 'offline' | 'online'
  total_discount: number
  bonuses: number | null
  bonuses_used: number | null
  receipt_url: string | null
  order_status: string | null
  tracking_numbers: string[]
  receipt_file_url: string | null
  photo_url: string | null
  has_warranty: boolean | null
  warranty_text: string | null
  warranty_expires_at: string | null
  warranty_pdf_url: string | null
  account_label: string
  tags: string[]
  items: ReceiptItem[]
  duplicate?: boolean
}

export interface ShopSuggestion {
  shop_name: string
  shop_address: string | null
  count: number
}

// Receipt hashtags (api/receipts.py) — group spend across a trip, renovation, etc.
export interface TagSummary {
  tag: string
  receipt_count: number
  total_amount: number
}

export interface ReceiptCreate {
  account_id: string
  purchased_at: string
  total_amount: number
  currency?: string
  exchange_rate_to_uah?: number | null
  shop_name?: string | null
  shop_address?: string | null
  payment_type?: string | null
  channel?: 'offline' | 'online'
  total_discount?: number
  receipt_url?: string | null
  items: ReceiptItemInput[]
}

export interface ReceiptItemInput {
  name: string
  barcode?: string | null
  category_raw?: string | null
  category_id?: string | null
  quantity: number
  unit?: string | null
  unit_price?: number | null
  total_price: number
  discount?: number
}

export interface ReceiptList {
  total: number
  items: Receipt[]
}

export interface LoyaltyCode {
  value: string
  display: 'qr' | 'barcode' | 'pdf417'
  expires_at: string | null
}

export interface DashboardStats {
  total_receipts: number
  total_spent: number
  total_discount: number
  total_bonuses: number
  total_bonuses_used: number
  accounts_active: number
  accounts_needs_attention: number
  last_run_at: string | null
}

export interface StoreBreakdown {
  store_code: string
  store_name: string
  receipt_count: number
  total_spent: number
  total_discount: number
  total_bonuses: number
}

export interface ChannelBreakdown {
  channel: 'offline' | 'online'
  receipt_count: number
  total_spent: number
}

export interface TimeseriesPoint {
  period: string
  total_spent: number
  receipt_count: number
}

export interface FuelPrice {
  name: string
  price: number
  currency: string
  discount: number | null
  code: string | null
  previous_price?: number | null
}

export interface AccountFuelPrices {
  account_id: string
  account_label: string
  store_code: string
  store_name: string
  prices: FuelPrice[]
  checked_at?: string | null
  bonus_balance?: number | null
  bonus_balance_updated_at?: string | null
  discount_allowance?: number | null
  discount_allowance_unit?: string | null
  discount_allowance_resets_at?: string | null
}

export interface AppSetting<T = unknown> {
  key: string
  value: T | null
}

// app/api/widgets.py — compact per-widget JSON snapshots (for Home Assistant etc.)
export interface WidgetMeta {
  key: string
  label: string
  supports_range: boolean
}

export interface WidgetSnapshot {
  widget: string
  title: string
  generated_at: string
  summary: Record<string, number | string | null>
  items: Record<string, unknown>[]
}

export type DashboardRangeKey = '30d' | '90d' | 'year' | 'all'

// app_settings key `dashboard.layout` — which dashboard widgets show, their
// order (array order), and an optional per-widget date-range override.
export interface DashboardLayout {
  version: 1
  widgets: { key: string; visible: boolean; range: DashboardRangeKey | null }[]
  /** false = show pre-discount (gross) amounts on the dashboard. Default true. */
  include_discount?: boolean
}

// app/services/ai.py — AI configuration, stored in app_settings. Two blocks:
// which services are switched on (providers), and which service+model each task
// tries, in order (routing).
export type AiTask = 'classify' | 'normalize' | 'ocr' | 'intent'

// The local Ollama backend takes part in routing under this pseudo-provider id.
export const AI_LOCAL = 'local'

export interface AiProviderConfig {
  id: string
  label: string
  enabled: boolean
  base_url: string
  usage_url: string
  /** Models the user marked active — what the routing block may point at. */
  models: string[]
  workspace_id: string
  custom: boolean
}

/** One attempt in a task's priority list. Empty model = the provider's first. */
export interface AiRouteTarget {
  provider: string
  model: string
}

export interface SpeechLanguage {
  code: string
  label: string
}

export interface AiSpeechConfig {
  enabled: boolean
  provider: string
  model: string
  language: string
  alternative_languages: string[]
}

export interface AiConfig {
  local: { enabled: boolean; url: string; model: string; num_ctx: number }
  providers: AiProviderConfig[]
  speech: AiSpeechConfig
  routing: Record<AiTask, AiRouteTarget[]>
  monthly_token_budget: number
}

/** Catalogue entry: a service that can be switched on. */
export interface AiProviderMeta {
  id: string
  label: string
  kind: 'anthropic' | 'openai'
  custom: boolean
  usage_url: string
  fallback_models: string[]
}

export interface AiUsage {
  month: string
  cloud_tokens_in: number
  cloud_tokens_out: number
  cloud_calls: number
  cloud_errors: number
  local_tokens_in: number
  local_tokens_out: number
  local_calls: number
  local_errors: number
}

export interface AiLogEntry {
  ts: string
  task: string
  backend: string
  model: string
  tokens_in: number
  tokens_out: number
  duration_ms: number
  ok: boolean
  error: string
  note: string
  prompt: string
  response: string
}

export interface AiSettings {
  config: AiConfig
  providers: AiProviderMeta[]
  stt_providers: { id: string; label: string; kind: string; models: string[]; usage_url: string }[]
  /** Dictation languages the page offers, in menu order. */
  speech_languages: SpeechLanguage[]
  cloud_key_hints: Record<string, string | null>
  usage: AiUsage
}

export interface AiBackendCheck {
  ok: boolean
  detail: string
  models?: string[]
}

export interface AiCheckResult {
  local: AiBackendCheck
  providers: Record<string, AiBackendCheck>
}

export interface AiModelChoices {
  local: string[]
  /** Catalogue per configured provider id — live where the API answered, the
   *  static fallback set otherwise. */
  providers: Record<string, { id: string; display_name: string }[]>
  /** Providers whose catalogue really came from their API (the rest are static). */
  live?: string[]
  /** Transcription models per speech provider id. */
  stt?: Record<string, string[]>
  // Present when the endpoint auto-healed a retired model in the stored config.
  config?: AiConfig
}

/** GET /api/settings/ai/models/{provider} — one service's catalogue, for its
 *  own refresh button. */
export interface AiProviderModels {
  provider: string
  models: { id: string; display_name: string }[]
  live: boolean
  stt: string[]
  config?: AiConfig
}

export interface TaxCabinetSettings {
  configured: boolean
  token_hint: string | null
  api_guard: { day?: string; count?: number; blocked_until?: string; last_status?: number; last_error?: string }
}

export interface TelegramSettings {
  config: {
    enabled: boolean
    public_url: string
    authorized_chat_ids: number[]
    authorized_chats: { chat_id: number; username: string | null; display_name: string | null; approved_at: string | null }[]
    pending_chats: { chat_id: number; username: string | null; display_name: string | null; requested_at: string }[]
    capture_enabled: boolean
  }
  token_hint: string | null
  bot_username: string | null
}

export interface TelegramMessageLog {
  id: string
  telegram_update_id: number
  message_id: number
  chat_id: number
  chat_type: string | null
  chat_title: string | null
  sender_id: number | null
  sender_username: string | null
  sender_display_name: string | null
  sender_is_bot: boolean
  message_thread_id: number | null
  reply_to_message_id: number | null
  root_message_id: number
  conversation_key: string
  content_type: string
  text: string | null
  media_meta: Record<string, unknown>
  authorized: boolean
  status: string
  outcome: string | null
  error: string | null
  received_at: string
}

export interface TelegramMessageLogList {
  total: number
  items: TelegramMessageLog[]
}

export interface FuelPriceHistory {
  account_id: string
  account_label: string
  store_code: string
  store_name: string
  product_code: string | null
  product_name: string
  price: number
  discount: number | null
  currency: string
  recorded_at: string
}

export interface ProductPrice {
  name: string
  store_code: string
  store_name: string
  unit: string | null
  last_price: number
  last_purchased_at: string
  min_price: number
  max_price: number
  avg_price: number
  times_bought: number
}

export interface LiveProduct {
  store_code: string
  store_name: string
  name: string
  price: number
  currency: string
  unit: string | null
  brand: string | null
  image_url: string | null
  product_url: string | null
  in_stock: boolean | null
}

export interface WarrantyProduct {
  item_id: string
  receipt_id: string
  name: string
  store_code: string
  store_name: string
  account_label: string
  purchased_at: string
  purchase_price: number
  warranty_text: string | null
  warranty_expires_at: string | null
  expired: boolean
  product_url: string | null
  image_url: string | null
}

// --- Document inbox / receipt scanner (ROADMAP.md Крок 3) ---
export interface UploadResult {
  kind: 'receipt' | 'document'
  ratio: number | null
  scan_job_id: string | null
  receipt_id: string | null
  method: string | null
  confidence: string | null
  needs_review: boolean
  duplicate: boolean
  replaced_receipt_id: string | null
  kept_receipt_id: string | null
  document_id: string | null
  storage: string | null
}

export interface VoiceMessage {
  id: string
  source: string
  status: 'stored' | 'transcribed' | 'failed'
  duration_s: number | null
  size_bytes: number | null
  content_type: string | null
  transcript: string | null
  /** BCP-47 code the transcript came out of, when it has been recognised. */
  language: string | null
  error: string | null
  applied: string | null
  created_at: string
  transcribed_at: string | null
  audio_url: string
  stt_ready: boolean
  /** Configured dictation languages, primary first — the "іншою мовою" retries. */
  languages: SpeechLanguage[]
}

export interface BulkScanOut {
  done: number
  failed: number
  results: { id: string; ok: boolean; detail: string }[]
}

export interface SeriesUploadResult {
  /** "one" = glued into a single receipt, "separate" = one row per receipt,
   *  "ask" = the QR codes did not settle it and the row awaits an answer. */
  kind: 'one' | 'separate' | 'ask'
  merged: boolean
  needs_choice: boolean
  note: string
  scan_job_id: string | null
  results: UploadResult[]
}

export interface InboxDocument {
  id: string
  storage: string
  original_filename: string | null
  content_type: string | null
  width: number | null
  height: number | null
  ratio: number | null
  status: 'unprocessed' | 'done'
  final_name: string | null
  target_folder_id: string | null
  created_at: string
  processed_at: string | null
  file_url: string | null
}

export interface ScanJob {
  id: string
  source_filename: string | null
  status: 'pending' | 'parsed' | 'needs_review' | 'failed'
  method: string | null
  confidence: string | null
  receipt_id: string | null
  raw_ocr: Record<string, unknown>
  error: string | null
  created_at: string
  input_url?: string | null
  /** Frames of a batch, in the order they were shot (empty for a single photo). */
  part_urls?: string[]
  /** The batch is waiting to be told whether it is one receipt or several. */
  needs_choice?: boolean
  shop_name: string | null
  total_amount: number | null
  currency: string | null
  purchased_at: string | null
  receipt_deleted: boolean
  archived_at: string | null
  duplicate_of: string | null
}

export interface ScanStats {
  total: number
  by_method: Record<string, number>
  ai_share: number
}

export interface InboxConfig {
  receipt_ratio: number
  color_max_saturation: number
  color_max_nongray_fraction: number
  max_file_mb: number
}

// app/services/mcp_config.py — own MCP server (ROADMAP Крок 4), stored in app_settings.
// Per-tool access level: 'off' hidden, 'read' available if read-only, 'write' full.
export type McpAccess = 'off' | 'read' | 'write'
export type McpToolKind = 'read' | 'write'

export interface McpConfig {
  enabled: boolean
  tools: Record<string, McpAccess>
}

export interface McpToolInfo {
  name: string
  kind: McpToolKind // 'read' | 'write' — 'write' tools mutate the DB
  label: string
  access: McpAccess // stored level; the UI shows a single checkbox (off ⇄ read/write by kind)
  available: boolean
}

export interface McpSettings {
  config: McpConfig
  tools: McpToolInfo[]
  mount_path: string
}

export interface DriveConfig {
  connected: boolean
  root_folder_id: string | null
  root_folder_name?: string | null
  root_folder_color?: string | null
  inbox_folder_id: string | null
  inbox_folder_name?: string | null
  inbox_folder_color?: string | null
}

export interface DriveStatus {
  configured: boolean
  connected: boolean
  redirect_uri: string
  client_id: string
  client_secret_masked: string | null
  client_source: 'settings' | 'env' | 'none'
  root_folder_id: string | null
  root_folder_name: string | null
  root_folder_color: string | null
  inbox_folder_id: string | null
  inbox_folder_name: string | null
  inbox_folder_color: string | null
}

export interface DriveFoldersIn {
  root_folder_id?: string | null
  root_folder_name?: string | null
  root_folder_color?: string | null
  inbox_folder_id?: string | null
  inbox_folder_name?: string | null
  inbox_folder_color?: string | null
  clear_root?: boolean
  clear_inbox?: boolean
}

export interface DriveFolder {
  id: string
  name: string
  color: string | null
}

export interface DriveColorPalette {
  colors: string[]
  default: string
}
