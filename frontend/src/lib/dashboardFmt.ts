// Shared dashboard helpers — formatters + date-range math used by every widget
// and by the standalone widget page. Previously local to DashboardView.vue.
import type { DashboardRangeKey } from '@/types/api'

export const RANGES: { key: DashboardRangeKey; label: string; days: number | null }[] = [
  { key: '30d', label: '30 днів', days: 30 },
  { key: '90d', label: '90 днів', days: 90 },
  { key: 'year', label: 'Рік', days: 365 },
  { key: 'all', label: 'Весь час', days: null },
]

export const DEFAULT_RANGE: DashboardRangeKey = 'year'

export function isRangeKey(v: unknown): v is DashboardRangeKey {
  return typeof v === 'string' && RANGES.some((r) => r.key === v)
}

/** Full ISO instant for the start of a range, or undefined for "all". */
export function rangeDateFrom(range: DashboardRangeKey): string | undefined {
  const r = RANGES.find((x) => x.key === range)
  if (!r || r.days === null) return undefined
  const d = new Date()
  d.setDate(d.getDate() - r.days)
  return d.toISOString()
}

/** Query params for `api.dashboard.*` / `api.widgets.get`. `includeDiscount`
 * false asks the backend for pre-discount (gross) amounts; the default (true =
 * what was actually paid) is left off the query string. */
export function rangeParams(
  range: DashboardRangeKey,
  includeDiscount = true,
): { date_from?: string; include_discount?: string } {
  const from = rangeDateFrom(range)
  return {
    ...(from ? { date_from: from } : {}),
    ...(includeDiscount ? {} : { include_discount: 'false' }),
  }
}

/** Plain YYYY-MM-DD — for linking into ReceiptsView's <input type="date"> filters. */
export function rangeDateFromShort(range: DashboardRangeKey): string | undefined {
  return rangeDateFrom(range)?.slice(0, 10)
}

/** One colour per dashboard metric, shared by the chart lines, the legend, the
 * tooltip rows and the savings table — so a number is always the colour of the
 * line it belongs to. Bonuses get their own hue: --accent is the spend bars. */
export const METRIC_COLORS = {
  spend: 'var(--accent)',
  total_discount: 'var(--success)',
  total_bonuses: 'var(--bonus)',
  total_bonuses_used: 'var(--warning)',
} as const

export function fmtMoney(v: number): string {
  return new Intl.NumberFormat('uk-UA', {
    style: 'currency',
    currency: 'UAH',
    maximumFractionDigits: 0,
  }).format(v)
}

export function fmtMoneyFull(v: number): string {
  return new Intl.NumberFormat('uk-UA', { style: 'currency', currency: 'UAH' }).format(v)
}

export function fmtPeriod(period: string): string {
  const [y = period, m = '', d = ''] = period.split('-')
  if (period.length === 4) return period // year
  if (period.length === 7) return `${m}.${y.slice(2)}` // month
  return `${d}.${m}` // day
}

// Plain YYYY-MM-DD — matches what ReceiptsView's <input type="date"> and its own
// day-boundary expansion expect.
export function periodRange(period: string): { date_from: string; date_to: string } {
  if (period.length === 4) {
    return { date_from: `${period}-01-01`, date_to: `${period}-12-31` }
  }
  if (period.length === 7) {
    const [y, m] = period.split('-')
    const lastDay = new Date(Number(y), Number(m), 0).getDate()
    return { date_from: `${period}-01`, date_to: `${period}-${String(lastDay).padStart(2, '0')}` }
  }
  return { date_from: period, date_to: period }
}
