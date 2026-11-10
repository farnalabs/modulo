import i18n from '../i18n'

/**
 * Human-readable, i18n-aware byte formatting shared by every admin surface that
 * renders a byte count (housekeeping, run retention, DB capacity).
 *
 * Rendering is intentionally uniform: the value keeps its localized (thousands)
 * grouping and at most one fraction digit, with trailing zeros trimmed so an
 * exact unit reads as `8 GB` and a fractional one as `1.5 MB`. Units and the
 * zero label are locale keys, so the existing i18n sweep covers this copy too.
 */

const UNIT_KEYS = [
  'bytes_unit_B',
  'bytes_unit_KB',
  'bytes_unit_MB',
  'bytes_unit_GB',
  'bytes_unit_TB',
] as const

const DEFAULT_INVALID_LABEL = '—'

export interface FormatBytesOptions {
  /** Text returned for null, undefined, non-finite, or negative input. */
  invalidLabel?: string
}

export function formatBytes(
  bytes: number | null | undefined,
  options: FormatBytesOptions = {},
): string {
  const { invalidLabel = DEFAULT_INVALID_LABEL } = options
  if (bytes == null || typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes < 0) {
    return invalidLabel
  }
  if (bytes === 0) {
    return i18n.global.t('common.bytes_zero')
  }

  const units = UNIT_KEYS.map((key) => i18n.global.t(`common.${key}`))
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
  const value = bytes / Math.pow(1024, index)
  const formattedValue = value.toLocaleString(undefined, {
    minimumFractionDigits: 0,
    maximumFractionDigits: 1,
  })
  return `${formattedValue} ${units[index]}`
}
