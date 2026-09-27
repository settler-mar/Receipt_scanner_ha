<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { api } from '@/lib/api'
import { fmtDateTime } from '@/lib/datetime'
import type { DuplicateGroup, DuplicateMergeChoices, DuplicateMergeItemDecision, DuplicateRun } from '@/types/api'

const groups = ref<DuplicateGroup[]>([])
const runs = ref<DuplicateRun[]>([])
const status = ref('')
const loading = ref(true)
const running = ref(false)
const error = ref('')
const notice = ref('')
const mergePreview = ref<DuplicateGroup | null>(null)
const previewTab = ref<'fields' | 'items'>('fields')
const itemMode = ref<'identity' | 'money'>('money')
const hoverKey = ref('')
const mergeChoices = ref<DuplicateMergeChoices>({
  receipt_fields: {},
  item_name: {},
  item_barcode: {},
  item_price: {},
  item_discount: {},
  item_identity: {},
  item_money: {},
})

const visibleGroups = computed(() => groups.value)
const resolvableStatuses = new Set(['pending', 'needs_review', 'rolled_back'])
const fieldLabels: Record<string, string> = {
  purchased_at: 'Час',
  total_amount: 'Сума',
  currency: 'Валюта',
  exchange_rate_to_uah: 'Курс',
  shop_name: 'Магазин',
  shop_address: 'Адреса',
  payment_type: 'Оплата',
  total_discount: 'Знижка',
  bonuses: 'Бонуси',
  bonuses_used: 'Списано бонусів',
  receipt_url: 'Посилання',
  order_status: 'Статус замовлення',
  tracking_numbers: 'Трек-номери',
  photo_filename: 'Фото',
  has_warranty: 'Гарантія',
  warranty_expires_at: 'Гарантія до',
  warranty_pdf_filename: 'PDF гарантії',
}

async function refresh() {
  loading.value = true
  error.value = ''
  try {
    ;[groups.value, runs.value] = await Promise.all([
      api.duplicates.groups(status.value || undefined),
      api.duplicates.runs(),
    ])
  } catch (err) {
    error.value = (err as Error).message
  } finally {
    loading.value = false
  }
}

onMounted(refresh)

async function runSearch() {
  running.value = true
  error.value = ''
  notice.value = ''
  try {
    let run = await api.duplicates.run()
    // The server starts the scan in a worker and returns its persistent run
    // row immediately.  Keep the existing button UX while polling the same
    // endpoint used by the history panel.
    for (let attempt = 0; run.status === 'running' && attempt < 240; attempt += 1) {
      await new Promise(resolve => setTimeout(resolve, 500))
      const latest = (await api.duplicates.runs()).find(item => item.id === run.id)
      if (latest) run = latest
    }
    if (run.status === 'running') {
      throw new Error('Пошук дублікатів ще виконується — перевірте журнал запусків пізніше.')
    }
    if (run.status === 'error') {
      throw new Error(run.error_message || 'Пошук дублікатів завершився помилкою.')
    }
    notice.value = `Пошук завершено: перевірено ${run.scanned_receipts}, нових груп ${run.candidates_new}.`
    await refresh()
  } catch (err) {
    error.value = (err as Error).message
  } finally {
    running.value = false
  }
}

async function act(group: DuplicateGroup, action: 'merge' | 'reject' | 'rollback', choices?: DuplicateMergeChoices) {
  error.value = ''
  notice.value = ''
  try {
    const updated = action === 'merge'
      ? await api.duplicates.merge(group.id, choices)
      : await api.duplicates[action](group.id)
    notice.value = action === 'merge'
      ? 'Групу обʼєднано.'
      : action === 'reject'
        ? 'Групу позначено як не дубль.'
        : 'Операцію відкочено.'
    const idx = groups.value.findIndex((g) => g.id === updated.id)
    if (idx >= 0) groups.value[idx] = updated
    await refresh()
  } catch (err) {
    error.value = (err as Error).message
  }
}

function openMergePreview(group: DuplicateGroup) {
  mergePreview.value = group
  previewTab.value = 'fields'
  itemMode.value = 'money'
  hoverKey.value = ''
  mergeChoices.value = defaultChoices(group)
}

async function confirmMerge() {
  if (!mergePreview.value) return
  const group = mergePreview.value
  const choices = mergeChoices.value
  mergePreview.value = null
  await act(group, 'merge', choices)
}

function money(amount: number, currency: string) {
  return `${amount.toFixed(2)} ${currency}`
}

function rateText(receipt: DuplicateGroup['receipts'][number]) {
  if (!receipt.exchange_rate_to_uah || receipt.currency === 'UAH') return ''
  return ` · 1 ${receipt.currency} = ${receipt.exchange_rate_to_uah.toFixed(4)} UAH`
}

function rawHint(raw: Record<string, unknown>) {
  const refs = raw.source_receipts
  if (Array.isArray(refs)) return `${refs.length} джерел`
  if (raw.merged_into_receipt_id) return 'джерело обʼєднане'
  return ''
}

function canResolve(group: DuplicateGroup) {
  return resolvableStatuses.has(group.status)
}

function receiptLabel(group: DuplicateGroup, receiptId: string | null | undefined) {
  const receipt = group.receipts.find((r) => r.id === receiptId)
  return receipt ? `${receipt.store_code} · ${receipt.external_id}` : receiptId || '—'
}

function hasItemDifference(item: DuplicateMergeItemDecision, field: keyof DuplicateMergeItemDecision['result']) {
  const values = item.sources.map((source) => source[field] ?? '')
  return new Set(values.map((value) => String(value))).size > 1
}

function itemNotes(item: DuplicateMergeItemDecision) {
  const notes = []
  if (item.changed_fields.includes('discount')) notes.push('скидка з іншого джерела')
  if (item.changed_fields.includes('barcode')) notes.push('додано артикул')
  if (item.changed_fields.includes('category_id')) notes.push('додано категорію')
  if (item.changed_fields.includes('name')) notes.push('нормалізовано назву')
  if (item.matched < (mergePreview.value?.receipts.length || 0)) notes.push('не всі джерела зіставлені')
  return notes.join(' · ')
}

function fieldLabel(field: string) {
  return fieldLabels[field] || field
}

function formatFieldValue(field: string, value: unknown, group: DuplicateGroup) {
  if (value === null || value === undefined || value === '') return '—'
  if (Array.isArray(value)) return value.length ? value.join(', ') : '—'
  if (typeof value === 'boolean') return value ? 'так' : 'ні'
  if (field === 'purchased_at' || field.endsWith('_at')) return fmtDateTime(String(value))
  if (field === 'total_amount' || field === 'total_discount' || field === 'bonuses' || field === 'bonuses_used') {
    return money(Number(value), group.receipts[0]?.currency || 'UAH')
  }
  if (field === 'exchange_rate_to_uah' && typeof value === 'number') return value.toFixed(4)
  return String(value)
}

function changedFields(group: DuplicateGroup) {
  return group.merge_plan?.fields.filter((field) => field.changed) || []
}

function defaultChoices(group: DuplicateGroup): DuplicateMergeChoices {
  const receipt_fields: Record<string, string> = {}
  const item_name: Record<string, string> = {}
  const item_barcode: Record<string, string> = {}
  const item_price: Record<string, string> = {}
  const item_discount: Record<string, string> = {}
  const item_identity: Record<string, string> = {}
  const item_money: Record<string, string> = {}
  for (const field of group.merge_plan?.fields || []) receipt_fields[field.field] = field.from_receipt_id
  for (const item of group.merge_plan?.items || []) {
    item_name[String(item.index)] = item.name_from_receipt_id || item.identity_from_receipt_id
    item_barcode[String(item.index)] = item.barcode_from_receipt_id || item.identity_from_receipt_id
    item_price[String(item.index)] = item.price_from_receipt_id || item.money_from_receipt_id
    item_discount[String(item.index)] = item.discount_from_receipt_id || item.money_from_receipt_id
    item_identity[String(item.index)] = item.identity_from_receipt_id || item.name_from_receipt_id
    item_money[String(item.index)] = item.money_from_receipt_id
  }
  return { receipt_fields, item_name, item_barcode, item_price, item_discount, item_identity, item_money }
}

function sourceReceipts(group: DuplicateGroup) {
  return group.receipts.slice(0, 2)
}

function sourceName(group: DuplicateGroup, index: number) {
  const receipt = sourceReceipts(group)[index]
  return receipt ? `${receipt.store_code} · ${receipt.external_id}` : `Джерело ${index + 1}`
}

function fieldSourceValue(group: DuplicateGroup, field: ReturnType<typeof changedFields>[number], index: number) {
  const receipt = sourceReceipts(group)[index]
  const source = field.sources.find((item) => item.receipt_id === receipt?.id)
  return source ? formatFieldValue(field.field, source.value, group) : '—'
}

function fieldSourceWins(field: ReturnType<typeof changedFields>[number], group: DuplicateGroup, index: number) {
  return sourceReceipts(group)[index]?.id === (mergeChoices.value.receipt_fields[field.field] || field.from_receipt_id)
}

function chooseFieldSource(field: ReturnType<typeof changedFields>[number], group: DuplicateGroup, index: number) {
  const receiptId = sourceReceipts(group)[index]?.id
  if (receiptId) mergeChoices.value.receipt_fields[field.field] = receiptId
}

function itemSource(item: DuplicateMergeItemDecision, group: DuplicateGroup, index: number) {
  const receipt = sourceReceipts(group)[index]
  return item.sources.find((source) => source.receipt_id === receipt?.id)
}

function itemValue(item: DuplicateMergeItemDecision, group: DuplicateGroup, index: number) {
  const source = itemSource(item, group, index)
  return source ? itemSourceDisplay(source, group) : '—'
}

function itemSourceDisplay(source: DuplicateMergeItemDecision['sources'][number], group: DuplicateGroup) {
  const currency = group.receipts[0]?.currency || 'UAH'
  if (itemMode.value === 'identity') return `${source.name}${source.barcode ? ` · ${source.barcode}` : ''}`
  return `${money(source.total_price, currency)}${source.unit_price !== null ? ` · ${money(source.unit_price, currency)}/од.` : ''}`
    + `${source.discount ? ` · знижка ${money(source.discount, currency)}` : ''}`
}

type ItemChoiceField = 'name' | 'barcode' | 'price' | 'discount'

function itemChoiceRows() {
  return itemMode.value === 'identity'
    ? [
        { key: 'name' as const, label: 'Назва' },
        { key: 'barcode' as const, label: 'Артикул' },
      ]
    : [
        { key: 'price' as const, label: 'Ціна' },
        { key: 'discount' as const, label: 'Знижка' },
      ]
}

function itemFieldValue(item: DuplicateMergeItemDecision, group: DuplicateGroup, index: number, field: ItemChoiceField) {
  const source = itemSource(item, group, index)
  const currency = group.receipts[0]?.currency || 'UAH'
  if (!source) return '—'
  if (field === 'name') return source.name
  if (field === 'barcode') return source.barcode || '—'
  if (field === 'discount') return source.discount ? money(source.discount, currency) : '—'
  return `${money(source.total_price, currency)}${source.unit_price !== null ? ` · ${money(source.unit_price, currency)}/од.` : ''}`
}

function itemFieldSelectedId(item: DuplicateMergeItemDecision, field: ItemChoiceField) {
  const key = String(item.index)
  if (field === 'name') return mergeChoices.value.item_name[key] || item.name_from_receipt_id || item.identity_from_receipt_id
  if (field === 'barcode') return mergeChoices.value.item_barcode[key] || item.barcode_from_receipt_id || item.identity_from_receipt_id
  if (field === 'price') return mergeChoices.value.item_price[key] || item.price_from_receipt_id || item.money_from_receipt_id
  return mergeChoices.value.item_discount[key] || item.discount_from_receipt_id || item.money_from_receipt_id
}

function itemFieldWins(item: DuplicateMergeItemDecision, group: DuplicateGroup, index: number, field: ItemChoiceField) {
  const sourceId = sourceReceipts(group)[index]?.id
  return !!sourceId && itemFieldSelectedId(item, field) === sourceId
}

function chooseItemFieldSource(item: DuplicateMergeItemDecision, group: DuplicateGroup, index: number, field: ItemChoiceField) {
  const receiptId = sourceReceipts(group)[index]?.id
  if (!receiptId) return
  const key = String(item.index)
  if (field === 'name') mergeChoices.value.item_name[key] = receiptId
  else if (field === 'barcode') mergeChoices.value.item_barcode[key] = receiptId
  else if (field === 'price') mergeChoices.value.item_price[key] = receiptId
  else mergeChoices.value.item_discount[key] = receiptId
}

function compareValue(value: unknown) {
  if (value === null || value === undefined) return ''
  const raw = Array.isArray(value) ? value.join('') : String(value)
  return raw
    .normalize('NFKC')
    .replace(/[Aa]/g, 'а')
    .replace(/[Bb]/g, 'в')
    .replace(/[Cc]/g, 'с')
    .replace(/[Ee]/g, 'е')
    .replace(/[Hh]/g, 'н')
    .replace(/[Ii]/g, 'і')
    .replace(/[Kk]/g, 'к')
    .replace(/[Mm]/g, 'м')
    .replace(/[Oo]/g, 'о')
    .replace(/[Pp]/g, 'р')
    .replace(/[Tt]/g, 'т')
    .replace(/[Xx]/g, 'х')
    .replace(/[Yy]/g, 'у')
    .toLocaleLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, '')
}

function fieldSourcesEquivalent(group: DuplicateGroup, field: ReturnType<typeof changedFields>[number]) {
  return compareValue(fieldSourceValue(group, field, 0)) === compareValue(fieldSourceValue(group, field, 1))
}

function itemSourcesEquivalent(item: DuplicateMergeItemDecision, group: DuplicateGroup) {
  return compareValue(itemValue(item, group, 0)) === compareValue(itemValue(item, group, 1))
}

function itemFieldSourcesEquivalent(item: DuplicateMergeItemDecision, group: DuplicateGroup, field: ItemChoiceField) {
  return compareValue(itemFieldValue(item, group, 0, field)) === compareValue(itemFieldValue(item, group, 1, field))
}

function selectedFieldResult(group: DuplicateGroup, field: ReturnType<typeof changedFields>[number]) {
  const selected = mergeChoices.value.receipt_fields[field.field] || field.from_receipt_id
  const source = field.sources.find((item) => item.receipt_id === selected)
  return source ? formatFieldValue(field.field, source.value, group) : formatFieldValue(field.field, field.result, group)
}

function selectedItemResult(item: DuplicateMergeItemDecision, group: DuplicateGroup) {
  const selected = itemMode.value === 'identity'
    ? mergeChoices.value.item_identity[String(item.index)] || item.identity_from_receipt_id || item.name_from_receipt_id
    : mergeChoices.value.item_money[String(item.index)] || item.money_from_receipt_id
  const source = item.sources.find((itemSource) => itemSource.receipt_id === selected)
  if (!source) {
    if (itemMode.value === 'identity') return `${item.result.name}${item.result.barcode ? ` · ${item.result.barcode}` : ''}`
    return `${money(item.result.total_price, group.receipts[0]?.currency || 'UAH')}`
  }
  return itemSourceDisplay(source, group)
}

function rowHoverKey(kind: string, index: string | number) {
  return `${kind}:${index}`
}

function hoverClass(key: string) {
  return { hovered: hoverKey.value === key }
}
</script>

<template>
  <div class="toolbar page-head">
    <div>
      <h2>Дублі чеків</h2>
      <p class="muted">Окремий пошук між джерелами: API магазинів, ДПС, OCR, Telegram і ручні чеки.</p>
    </div>
    <button :disabled="running" @click="runSearch">{{ running ? 'Пошук…' : 'Запустити пошук' }}</button>
  </div>

  <p v-if="notice" class="banner ok">{{ notice }}</p>
  <p v-if="error" class="banner bad">{{ error }}</p>

  <div class="toolbar card filters">
    <label>
      Статус
      <select v-model="status" @change="refresh">
        <option value="">Усі</option>
        <option value="pending">pending</option>
        <option value="needs_review">needs_review</option>
        <option value="auto_merged">auto_merged</option>
        <option value="rejected">rejected</option>
        <option value="rolled_back">rolled_back</option>
      </select>
    </label>
    <span class="muted" style="margin-left: auto">
      <template v-if="runs[0]">
        Останній запуск: {{ fmtDateTime(runs[0].started_at) }} · {{ runs[0].status }}
      </template>
    </span>
  </div>

  <p v-if="loading" class="muted">Завантаження…</p>
  <p v-else-if="!visibleGroups.length" class="muted">Кандидатів немає.</p>

  <section v-for="group in visibleGroups" :key="group.id" class="card group">
    <div class="toolbar">
      <div>
        <strong>Оцінка {{ group.score }}/100</strong>
        <span class="badge" :class="`badge-${group.status}`">{{ group.status }}</span>
        <span class="muted"> · {{ fmtDateTime(group.last_seen_at) }}</span>
      </div>
      <div class="actions">
        <button
          v-if="canResolve(group)"
          @click="openMergePreview(group)"
        >
          Обʼєднати
        </button>
        <button
          v-if="canResolve(group)"
          class="secondary"
          @click="act(group, 'reject')"
        >
          Не дублі
        </button>
        <button v-if="group.status === 'auto_merged'" class="danger" @click="act(group, 'rollback')">
          Відкотити
        </button>
      </div>
    </div>

    <p class="muted reasons">{{ group.reasons.join(' · ') || 'Причини не записані' }}</p>

    <div class="compare">
      <article v-for="receipt in group.receipts" :key="receipt.id" class="receipt-panel">
        <div class="toolbar">
          <strong>{{ receipt.store_code }}</strong>
          <RouterLink :to="`/receipts/${receipt.id}`">Відкрити →</RouterLink>
        </div>
        <dl>
          <dt>Акаунт</dt><dd>{{ receipt.account_label || receipt.account_id }}</dd>
          <dt>Час</dt><dd>{{ fmtDateTime(receipt.purchased_at) }}</dd>
          <dt>Сума</dt><dd>{{ money(receipt.total_amount, receipt.currency) }}{{ rateText(receipt) }}</dd>
          <dt>Магазин</dt><dd>{{ receipt.shop_name || '—' }}</dd>
          <dt>Адреса</dt><dd>{{ receipt.shop_address || '—' }}</dd>
          <dt>Оплата</dt><dd>{{ receipt.payment_type || '—' }}</dd>
          <dt>External ID</dt><dd><code>{{ receipt.external_id }}</code></dd>
          <dt>Сирі дані</dt><dd>{{ rawHint(receipt.raw) || 'збережені' }}</dd>
        </dl>
        <div class="tags" v-if="receipt.tags.length">
          <span v-for="tag in receipt.tags" :key="tag" class="badge badge-tag">#{{ tag }}</span>
        </div>
        <details>
          <summary>Позиції ({{ receipt.items.length }})</summary>
          <table>
            <tbody>
              <tr v-for="item in receipt.items" :key="`${item.name}-${item.total_price}`">
                <td>{{ item.name }}</td>
                <td>{{ item.quantity }} {{ item.unit || '' }}</td>
                <td>{{ money(item.total_price, receipt.currency) }}</td>
              </tr>
            </tbody>
          </table>
        </details>
      </article>
    </div>
  </section>

  <div v-if="mergePreview" class="modal-backdrop" @click.self="mergePreview = null">
    <section class="merge-modal">
      <div class="toolbar">
        <div>
          <h3>План обʼєднання</h3>
          <p class="muted">
            Канонічний чек: {{ receiptLabel(mergePreview, mergePreview.merge_plan?.canonical_receipt_id) }}
          </p>
        </div>
        <button class="secondary" @click="mergePreview = null">Закрити</button>
      </div>

      <p v-if="!mergePreview.merge_plan" class="banner bad">План обʼєднання недоступний для цієї групи.</p>
      <template v-else>
        <p v-for="warning in mergePreview.merge_plan.warnings" :key="warning" class="banner bad">
          {{ warning }}
        </p>
        <div class="preview-tabs">
          <button :class="{ active: previewTab === 'fields' }" class="secondary" @click="previewTab = 'fields'">
            Поля чека
          </button>
          <button :class="{ active: previewTab === 'items' }" class="secondary" @click="previewTab = 'items'">
            Позиції
          </button>
        </div>

        <div v-if="previewTab === 'fields'" class="merge-scroll fields-scroll">
          <table class="merge-table">
            <thead>
              <tr>
                <th>Поле</th>
                <th>{{ sourceName(mergePreview, 0) }}</th>
                <th>{{ sourceName(mergePreview, 1) }}</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="field in changedFields(mergePreview)"
                :key="field.field"
                @mouseenter="hoverKey = rowHoverKey('field', field.field)"
                @mouseleave="hoverKey = ''"
              >
                <td class="field-name">{{ fieldLabel(field.field) }}</td>
                <td
                  v-if="fieldSourcesEquivalent(mergePreview, field)"
                  colspan="2"
                  class="pick-cell merged-value winner"
                  :class="hoverClass(rowHoverKey('field', field.field))"
                >
                  {{ fieldSourceValue(mergePreview, field, 0) }}
                </td>
                <td
                  v-else
                  class="pick-cell"
                  :class="[{ winner: fieldSourceWins(field, mergePreview, 0) }, hoverClass(rowHoverKey('field', field.field))]"
                  @click="chooseFieldSource(field, mergePreview, 0)"
                >
                  {{ fieldSourceValue(mergePreview, field, 0) }}
                </td>
                <td
                  v-if="!fieldSourcesEquivalent(mergePreview, field)"
                  class="pick-cell"
                  :class="[{ winner: fieldSourceWins(field, mergePreview, 1) }, hoverClass(rowHoverKey('field', field.field))]"
                  @click="chooseFieldSource(field, mergePreview, 1)"
                >
                  {{ fieldSourceValue(mergePreview, field, 1) }}
                </td>
              </tr>
              <tr v-if="!changedFields(mergePreview).length">
                <td colspan="3" class="muted">Розбіжностей у загальних полях немає.</td>
              </tr>
            </tbody>
          </table>
        </div>

        <template v-else>
          <div class="mode-tabs">
            <button :class="{ active: itemMode === 'money' }" class="secondary" @click="itemMode = 'money'">Ціна + знижка</button>
            <button :class="{ active: itemMode === 'identity' }" class="secondary" @click="itemMode = 'identity'">Назва + артикул</button>
          </div>
          <div class="merge-scroll">
            <table class="merge-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>Поле</th>
                  <th>{{ sourceName(mergePreview, 0) }}</th>
                  <th>{{ sourceName(mergePreview, 1) }}</th>
                  <th>Статус</th>
                </tr>
              </thead>
              <tbody>
                <template v-for="item in mergePreview.merge_plan.items" :key="item.index">
                  <tr
                    v-for="row in itemChoiceRows()"
                    :key="`${item.index}-${row.key}`"
                    @mouseenter="hoverKey = rowHoverKey('item', `${item.index}-${row.key}`)"
                    @mouseleave="hoverKey = ''"
                  >
                    <td class="field-name">{{ item.index + 1 }}</td>
                    <td class="field-name">{{ row.label }}</td>
                    <td
                      v-if="itemFieldSourcesEquivalent(item, mergePreview, row.key)"
                      colspan="2"
                      class="pick-cell merged-value winner"
                      :class="hoverClass(rowHoverKey('item', `${item.index}-${row.key}`))"
                    >
                      {{ itemFieldValue(item, mergePreview, 0, row.key) }}
                    </td>
                    <td
                      v-else
                      class="pick-cell"
                      :class="[{ winner: itemFieldWins(item, mergePreview, 0, row.key) }, hoverClass(rowHoverKey('item', `${item.index}-${row.key}`))]"
                      @click="chooseItemFieldSource(item, mergePreview, 0, row.key)"
                    >
                      {{ itemFieldValue(item, mergePreview, 0, row.key) }}
                    </td>
                    <td
                      v-if="!itemFieldSourcesEquivalent(item, mergePreview, row.key)"
                      class="pick-cell"
                      :class="[{ winner: itemFieldWins(item, mergePreview, 1, row.key) }, hoverClass(rowHoverKey('item', `${item.index}-${row.key}`))]"
                      @click="chooseItemFieldSource(item, mergePreview, 1, row.key)"
                    >
                      {{ itemFieldValue(item, mergePreview, 1, row.key) }}
                    </td>
                    <td class="muted">{{ itemNotes(item) || 'без змін' }}</td>
                  </tr>
                </template>
              </tbody>
            </table>
          </div>
        </template>
        <div class="toolbar modal-actions">
          <button class="secondary" @click="mergePreview = null">Скасувати</button>
          <button @click="confirmMerge">Обʼєднати за цим планом</button>
        </div>
      </template>
    </section>
  </div>
</template>

<style scoped>
.page-head {
  align-items: flex-start;
  margin-bottom: 18px;
}
.page-head h2 {
  margin-bottom: 6px;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
}
.filters {
  margin-bottom: 18px;
}
.filters label {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-dim);
}
.banner {
  border-radius: var(--radius);
  padding: 10px 12px;
}
.banner.ok {
  background: rgba(74, 222, 128, 0.12);
  color: var(--success);
}
.banner.bad {
  background: rgba(248, 113, 113, 0.12);
  color: var(--danger);
}
.group {
  margin-bottom: 16px;
}
.badge-pending {
  background: rgba(56, 189, 248, 0.15);
  color: var(--accent);
}
.badge-needs_review {
  background: rgba(251, 191, 36, 0.15);
  color: var(--warning);
}
.badge-auto_merged {
  background: rgba(74, 222, 128, 0.15);
  color: var(--success);
}
.badge-rejected,
.badge-rolled_back {
  background: rgba(148, 163, 184, 0.15);
  color: var(--text-dim);
}
.badge-tag {
  background: rgba(56, 189, 248, 0.12);
  color: var(--accent);
  margin-right: 6px;
}
.actions {
  margin-left: auto;
  display: flex;
  gap: 8px;
}
.reasons {
  margin: 8px 0 14px;
}
.compare {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 12px;
}
.receipt-panel {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  background: var(--bg);
}
dl {
  display: grid;
  grid-template-columns: 90px 1fr;
  gap: 6px 10px;
  margin: 10px 0;
}
dt {
  color: var(--text-dim);
}
dd {
  margin: 0;
  min-width: 0;
  overflow-wrap: anywhere;
}
.tags {
  margin-bottom: 10px;
}
details table {
  margin-top: 8px;
}
details td {
  padding: 6px 8px;
}
.modal-backdrop {
  position: fixed;
  inset: 0;
  z-index: 20;
  display: grid;
  place-items: center;
  padding: 20px;
  background: rgba(2, 6, 23, 0.72);
}
.merge-modal {
  width: min(1120px, 100%);
  max-height: min(860px, calc(100vh - 40px));
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: #151b2e;
  padding: 18px;
  box-shadow: 0 24px 80px rgba(0, 0, 0, 0.35);
}
.merge-modal h3 {
  margin: 0 0 4px;
}
.merge-modal h4 {
  margin: 14px 0 8px;
}
.preview-tabs,
.mode-tabs {
  display: flex;
  gap: 6px;
  margin: 12px 0 10px;
}
.preview-tabs button,
.mode-tabs button {
  padding: 7px 12px;
}
.preview-tabs button.active,
.mode-tabs button.active {
  border-color: rgba(74, 222, 128, 0.45);
  background: rgba(74, 222, 128, 0.14);
  color: var(--text);
}
.merge-scroll {
  max-height: calc(100vh - 250px);
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: #0f1224;
}
.fields-scroll {
  max-height: calc(100vh - 230px);
}
.merge-table {
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
  background: #0f1224;
}
.merge-table th {
  position: sticky;
  top: 0;
  z-index: 1;
  background: #151b2e;
}
.merge-table th,
.merge-table td {
  border-bottom: 1px solid var(--border);
  padding: 8px;
  text-align: left;
  vertical-align: top;
  background: #0f1224;
  overflow-wrap: anywhere;
}
.merge-table th:first-child,
.merge-table td:first-child {
  width: 130px;
}
.merge-table th:last-child,
.merge-table td:last-child {
  width: 190px;
}
.field-name {
  color: var(--text-dim);
  font-weight: 600;
}
.pick-cell {
  cursor: pointer;
  transition: background 0.12s ease, box-shadow 0.12s ease;
}
.pick-cell.merged-value {
  cursor: default;
}
.pick-cell.winner {
  background: rgba(74, 222, 128, 0.13);
  box-shadow: inset 3px 0 0 rgba(74, 222, 128, 0.55);
  color: #e9fff1;
}
.pick-cell.hovered {
  background: rgba(56, 189, 248, 0.11);
}
.pick-cell.winner.hovered {
  background: rgba(74, 222, 128, 0.2);
}
.result-cell {
  background: rgba(74, 222, 128, 0.08) !important;
  color: #e9fff1;
  font-weight: 650;
}
.source-line {
  display: grid;
  grid-template-columns: minmax(150px, 220px) minmax(220px, 1fr) auto auto auto auto;
  gap: 8px;
  align-items: start;
  padding: 5px 0;
  border-top: 1px solid var(--border);
  font-size: 12px;
  background: #0f1224;
}
.field-source {
  display: grid;
  grid-template-columns: minmax(160px, 240px) 1fr;
  gap: 8px;
  padding: 3px 0;
}
.diff {
  color: var(--warning);
}
.modal-actions {
  justify-content: flex-end;
  margin-top: 14px;
}
@media (max-width: 720px) {
  .toolbar {
    align-items: stretch;
    flex-direction: column;
  }
  .actions {
    margin-left: 0;
  }
  .modal-backdrop {
    align-items: stretch;
    padding: 10px;
  }
  .merge-modal {
    max-height: calc(100vh - 20px);
    padding: 12px;
  }
  .source-line {
    grid-template-columns: 1fr;
  }
  .field-source {
    grid-template-columns: 1fr;
  }
}
</style>
