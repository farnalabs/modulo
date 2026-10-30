import { computed, ComputedRef, unref } from 'vue'

/**
 * Returns the arrow character for a delta value.
 * ▲ for positive, ▼ for negative, → for zero.
 */
export function arrowFor(delta: number): string {
  if (delta > 0) return '▲'
  if (delta < 0) return '▼'
  return '→'
}

/**
 * Returns the CSS class for a delta value.
 * For MetricCard (non-inverted): success-text for positive, destructive for negative, muted for zero.
 * For StatCard (with inverted option): respects the inverted flag.
 */
export function classFor(delta: number, inverted = false): string {
  if (delta > 0) return inverted ? 'text-destructive' : 'text-success-text'
  if (delta < 0) return inverted ? 'text-success-text' : 'text-destructive'
  return 'text-muted-foreground'
}

/**
 * Formats a delta value as a string with sign.
 * e.g., +2, -2.5, 0
 */
export function formatDeltaAbs(delta: number | null): string {
  if (delta == null) return ''
  if (delta === 0) return '0'
  const sign = delta > 0 ? '+' : '-'
  const abs = Math.abs(delta)
  const formatted = Number.isInteger(abs) ? String(abs) : abs.toFixed(1)
  return `${sign}${formatted}`
}

/**
 * Formats a delta percentage as a string with % suffix.
 * @param deltaPct - The delta percentage value
 * @param includeSign - Whether to include sign for positive/negative values (default: false = absolute value only)
 * e.g., 16.3%, 16.3%, 0.0% (or +16.3%, -16.3%, 0.0% if includeSign=true)
 */
export function formatDeltaPct(deltaPct: number | null, includeSign = false): string {
  if (deltaPct == null) return ''
  if (includeSign) {
    const sign = deltaPct > 0 ? '+' : deltaPct < 0 ? '-' : ''
    return `${sign}${Math.abs(deltaPct).toFixed(1)}%`
  }
  return `${Math.abs(deltaPct).toFixed(1)}%`
}

/**
 * Computable version of formatDeltaAbs for use in Vue components.
 */
export function useDeltaAbsText(delta: ComputedRef<number | null>) {
  return computed(() => formatDeltaAbs(delta.value))
}

/**
 * Computable version of formatDeltaPct for use in Vue components.
 */
export function useDeltaPctText(deltaPct: ComputedRef<number | null>) {
  return computed(() => formatDeltaPct(deltaPct.value))
}

/**
 * Computable version of arrowFor for use in Vue components.
 */
export function useDeltaArrow(delta: ComputedRef<number | null>) {
  return computed(() => {
    const d = delta.value
    return d == null ? '' : arrowFor(d)
  })
}

/**
 * Computable version of classFor for use in Vue components.
 */
export function useDeltaClass(
  delta: ComputedRef<number | null>,
  inverted: ComputedRef<boolean> | boolean = false,
  neutralTrend: ComputedRef<boolean> | boolean = false
) {
  return computed(() => {
    const d = delta.value
    if (d == null) return ''
    const inv = unref(inverted)
    const neutral = unref(neutralTrend)
    if (neutral) return 'text-muted-foreground'
    return classFor(d, inv)
  })
}
