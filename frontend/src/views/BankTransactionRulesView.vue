<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { api } from '@/lib/api'
import type { BankTransactionRule, BankTransactionRuleBranch, BankTransactionRuleCondition, Category } from '@/types/api'
import { IGNORE_PRESETS } from '@/lib/bankTransactionPresets'

const rules = ref<BankTransactionRule[]>([])
const categories = ref<Category[]>([])
const loading = ref(true)
const error = ref('')

const categoryOptions = computed(() =>
  [...categories.value]
    .sort((a, b) => a.path.localeCompare(b.path))
    .map((c) => ({ id: c.id, label: `${'— '.repeat(c.path.split('/').length - 1)}${c.name}` })),
)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [ruleList, cats] = await Promise.all([api.bankTransactionRules.list(), api.categories.list()])
    rules.value = ruleList
    categories.value = cats
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

onMounted(load)

type ConditionKind = BankTransactionRuleCondition['kind']

const CONDITION_LABELS: Record<ConditionKind, string> = {
  always: 'Завжди',
  position_in_month: 'N-та операція в місяці',
  count_in_month: 'Рівно N операцій за місяць',
  amount_gte: 'Сума ≥',
  amount_lte: 'Сума ≤',
  amount_between: 'Сума від–до',
}
const CONDITION_KINDS = Object.keys(CONDITION_LABELS) as ConditionKind[]

interface BranchDraft {
  kind: ConditionKind
  position: number
  count: number
  amount: number
  min: number
  max: number
  category_id: string
  ignore_reason: string
}

function blankBranch(): BranchDraft {
  return { kind: 'always', position: 1, count: 1, amount: 0, min: 0, max: 0, category_id: '', ignore_reason: '' }
}

function fromApiBranch(branch: BankTransactionRuleBranch): BranchDraft {
  const c = branch.condition
  const draft = blankBranch()
  draft.kind = c.kind
  if (c.kind === 'position_in_month') draft.position = c.position
  if (c.kind === 'count_in_month') draft.count = c.count
  if (c.kind === 'amount_gte' || c.kind === 'amount_lte') draft.amount = c.amount
  if (c.kind === 'amount_between') { draft.min = c.min; draft.max = c.max }
  draft.category_id = branch.category_id ?? ''
  draft.ignore_reason = branch.ignore_reason ?? ''
  return draft
}

function toApiBranch(b: BranchDraft): BankTransactionRuleBranch {
  let condition: BankTransactionRuleCondition
  if (b.kind === 'position_in_month') condition = { kind: 'position_in_month', position: b.position }
  else if (b.kind === 'count_in_month') condition = { kind: 'count_in_month', count: b.count }
  else if (b.kind === 'amount_gte') condition = { kind: 'amount_gte', amount: b.amount }
  else if (b.kind === 'amount_lte') condition = { kind: 'amount_lte', amount: b.amount }
  else if (b.kind === 'amount_between') condition = { kind: 'amount_between', min: b.min, max: b.max }
  else condition = { kind: 'always' }
  return { condition, category_id: b.category_id || null, ignore_reason: b.ignore_reason || null }
}

const editingId = ref<string | null>(null)
const isNew = ref(false)
const form = ref({ label: '', counterparty_match: '', priority: 0, branches: [] as BranchDraft[] })
const saving = ref(false)
const formError = ref('')

function startCreate() {
  isNew.value = true
  editingId.value = null
  form.value = { label: '', counterparty_match: '', priority: 0, branches: [blankBranch()] }
  formError.value = ''
}

function startEdit(rule: BankTransactionRule) {
  isNew.value = false
  editingId.value = rule.id
  form.value = {
    label: rule.label,
    counterparty_match: rule.counterparty_match,
    priority: rule.priority,
    branches: rule.branches.length ? rule.branches.map(fromApiBranch) : [blankBranch()],
  }
  formError.value = ''
}

function cancelEdit() {
  editingId.value = null
  isNew.value = false
}

function addBranch() {
  form.value.branches.push(blankBranch())
}

function removeBranch(index: number) {
  form.value.branches.splice(index, 1)
}

async function saveRule() {
  if (!form.value.label.trim() || !form.value.counterparty_match.trim()) {
    formError.value = "Вкажіть назву і текст для пошуку відправника"
    return
  }
  saving.value = true
  formError.value = ''
  try {
    const payload = {
      label: form.value.label.trim(),
      counterparty_match: form.value.counterparty_match.trim(),
      priority: form.value.priority,
      branches: form.value.branches.map(toApiBranch),
    }
    if (isNew.value) {
      rules.value.push(await api.bankTransactionRules.create(payload))
    } else if (editingId.value) {
      const updated = await api.bankTransactionRules.update(editingId.value, payload)
      const idx = rules.value.findIndex((r) => r.id === editingId.value)
      if (idx >= 0) rules.value[idx] = updated
    }
    cancelEdit()
  } catch (e) {
    formError.value = (e as Error).message
  } finally {
    saving.value = false
  }
}

async function removeRule(id: string) {
  await api.bankTransactionRules.remove(id)
  rules.value = rules.value.filter((r) => r.id !== id)
  if (editingId.value === id) cancelEdit()
}

const applyBusy = ref(false)
const applyMessage = ref('')

async function applyNow() {
  applyBusy.value = true
  applyMessage.value = ''
  try {
    const result = await api.bankTransactionRules.apply()
    applyMessage.value = `Змінено транзакцій: ${result.updated}`
  } catch (e) {
    applyMessage.value = (e as Error).message
  } finally {
    applyBusy.value = false
  }
}

function branchSummary(branch: BankTransactionRuleBranch): string {
  const target = branch.ignore_reason
    ? IGNORE_PRESETS.find((p) => p.value === branch.ignore_reason)?.label ?? branch.ignore_reason
    : branch.category_id
      ? categoryOptions.value.find((c) => c.id === branch.category_id)?.label ?? 'категорія'
      : '—'
  const c = branch.condition
  const cond = c.kind === 'always' ? 'завжди'
    : c.kind === 'position_in_month' ? `${c.position}-та за місяць`
    : c.kind === 'count_in_month' ? `рівно ${c.count} за місяць`
    : c.kind === 'amount_gte' ? `сума ≥ ${c.amount}`
    : c.kind === 'amount_lte' ? `сума ≤ ${c.amount}`
    : `сума ${c.min}–${c.max}`
  return `${cond} → ${target}`
}
</script>

<template>
  <div class="toolbar">
    <div style="flex: 1">
      <h2 style="margin-bottom: 4px">Правила автоматизації</h2>
      <div class="muted">
        Автоматична категоризація переказів іншим людям за іменем відправника —
        <RouterLink to="/bank-transactions">назад до транзакцій</RouterLink>.
      </div>
    </div>
    <button :disabled="applyBusy" @click="applyNow">
      {{ applyBusy ? 'Застосовуємо…' : 'Застосувати всі правила зараз' }}
    </button>
  </div>
  <p v-if="applyMessage" class="muted">{{ applyMessage }}</p>

  <div v-if="loading" class="muted">Завантаження…</div>
  <div v-else-if="error" style="color: var(--danger)">{{ error }}</div>

  <template v-else>
    <button class="secondary" style="margin-bottom: 14px" @click="startCreate">+ Нове правило</button>

    <div v-if="isNew || editingId" class="card" style="margin-bottom: 16px">
      <h3 style="margin-top: 0">{{ isNew ? 'Нове правило' : 'Редагувати правило' }}</h3>
      <div class="form-row">
        <label>Назва</label>
        <input v-model="form.label" placeholder="Женя" />
      </div>
      <div class="form-row">
        <label>Текст для пошуку у відправнику</label>
        <input v-model="form.counterparty_match" placeholder="женя" />
      </div>
      <p class="muted" style="margin-top: -6px">
        Шукається без урахування регістру як частина опису транзакції — банк
        зазвичай пише туди ім'я відправника/отримувача переказу.
      </p>
      <div class="form-row">
        <label>Пріоритет (менше — раніше)</label>
        <input v-model.number="form.priority" type="number" style="max-width: 100px" />
      </div>

      <h4>Умови (перша, що підходить, спрацьовує)</h4>
      <div
        v-for="(branch, idx) in form.branches"
        :key="idx"
        class="card"
        style="margin-bottom: 10px; background: var(--bg-secondary, transparent)"
      >
        <div class="toolbar" style="flex-wrap: wrap">
          <select v-model="branch.kind">
            <option v-for="kind in CONDITION_KINDS" :key="kind" :value="kind">{{ CONDITION_LABELS[kind] }}</option>
          </select>
          <input
            v-if="branch.kind === 'position_in_month'"
            v-model.number="branch.position" type="number" min="1" style="max-width: 80px"
          />
          <input
            v-if="branch.kind === 'count_in_month'"
            v-model.number="branch.count" type="number" min="1" style="max-width: 80px"
          />
          <input
            v-if="branch.kind === 'amount_gte' || branch.kind === 'amount_lte'"
            v-model.number="branch.amount" type="number" style="max-width: 100px"
          />
          <template v-if="branch.kind === 'amount_between'">
            <input v-model.number="branch.min" type="number" placeholder="від" style="max-width: 100px" />
            <input v-model.number="branch.max" type="number" placeholder="до" style="max-width: 100px" />
          </template>
          <span class="muted">→</span>
          <select v-model="branch.ignore_reason">
            <option value="">— причина ігнорування —</option>
            <option v-for="p in IGNORE_PRESETS" :key="p.value" :value="p.value">{{ p.label }}</option>
          </select>
          <select v-model="branch.category_id">
            <option value="">— або категорія —</option>
            <option v-for="c in categoryOptions" :key="c.id" :value="c.id">{{ c.label }}</option>
          </select>
          <button class="secondary" @click="removeBranch(idx)">✕</button>
        </div>
      </div>
      <button class="secondary" @click="addBranch">+ Додати умову</button>

      <p v-if="formError" style="color: var(--danger)">{{ formError }}</p>
      <div class="toolbar" style="margin-top: 14px">
        <button :disabled="saving" @click="saveRule">{{ saving ? 'Збереження…' : 'Зберегти' }}</button>
        <button class="secondary" @click="cancelEdit">Скасувати</button>
      </div>
    </div>

    <div v-if="!rules.length" class="muted">Ще немає жодного правила.</div>
    <div v-for="rule in rules" :key="rule.id" class="card" style="margin-bottom: 12px">
      <div class="toolbar">
        <div style="flex: 1">
          <strong>{{ rule.label }}</strong>
          <span class="muted"> · шукає «{{ rule.counterparty_match }}» · пріоритет {{ rule.priority }}</span>
        </div>
        <button class="secondary" @click="startEdit(rule)">Редагувати</button>
        <button class="secondary" @click="removeRule(rule.id)">Видалити</button>
      </div>
      <ul style="margin: 8px 0 0; padding-left: 18px">
        <li v-for="(branch, idx) in rule.branches" :key="idx" class="muted">{{ branchSummary(branch) }}</li>
      </ul>
    </div>
  </template>
</template>
