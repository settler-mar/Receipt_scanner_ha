import { ref, type Ref } from 'vue'

/** One "label: value" line of a chart tooltip. */
export interface TooltipRow {
  label: string
  value: string
  strong?: boolean
  color?: string
}

export interface ChartTooltip<T> {
  /** Hovered datum, or null when the pointer is away from the chart. */
  hovered: Ref<T | null>
  x: Ref<number>
  y: Ref<number>
  show: (item: T, event: MouseEvent) => void
  move: (event: MouseEvent) => void
  hide: () => void
}

/**
 * Cursor tracking for the dashboard charts: widgets keep the hovered datum,
 * this keeps where to draw the <ChartTooltip> for it.
 */
export function useChartTooltip<T>(): ChartTooltip<T> {
  const hovered = ref<T | null>(null) as Ref<T | null>
  const x = ref(0)
  const y = ref(0)

  function move(event: MouseEvent) {
    x.value = event.clientX
    y.value = event.clientY
  }

  function show(item: T, event: MouseEvent) {
    hovered.value = item
    move(event)
  }

  function hide() {
    hovered.value = null
  }

  return { hovered, x, y, show, move, hide }
}

/** "12,3%" — share of a total, for tooltip rows. */
export function fmtShare(part: number, total: number): string {
  if (!total) return '—'
  return `${((part / total) * 100).toFixed(1).replace('.', ',')}%`
}
