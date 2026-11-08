import { format, formatDistanceToNow } from 'date-fns'

/** A value that can be coerced to a Date — parameter type shared by all formatters. */
export type DateLike = Date | string | number | null | undefined

/**
 * Safely parses a date-like value, returning null for invalid input.
 * Shared single source of truth for parsing user/API-supplied timestamps.
 */
export function toDate(date: DateLike): Date | null {
  if (date == null) return null
  const d = date instanceof Date ? date : new Date(date) // nosemgrep: new-date-without-guard
  return Number.isNaN(d.getTime()) ? null : d
}

export function formatDateShort(date: DateLike): string {
  const d = toDate(date)
  if (!d) return '—'
  return format(d, 'MMM d, yyyy')
}

export function formatDateShortWithTime(date: DateLike): string {
  const d = toDate(date)
  if (!d) return '—'
  return format(d, 'MMM d, yyyy, h:mm a')
}

/**
 * Short log timestamp ("Mar 15, 02:05 PM") shared by the log/detail surfaces.
 * Dash placeholder for invalid input so a malformed value never renders
 * "Invalid Date".
 */
export function formatDateLogTimestamp(date: DateLike): string {
  const d = toDate(date)
  if (!d) return '—'
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function formatDateFilename(date: DateLike): string {
  const d = toDate(date)
  if (!d) return '—'
  return format(d, 'yyyy-MM-dd')
}

/**
 * Humanised relative time ("3 minutes ago"). Dash placeholder for invalid input.
 * Shared single source of truth for relative-time formatting.
 */
export function formatRelativeTime(date: DateLike): string {
  const d = toDate(date)
  if (!d) return '—'
  return formatDistanceToNow(d, { addSuffix: true })
}

/**
 * Format a Date as the `YYYY-MM-DD` value a native `<input type="date">` uses.
 *
 * Uses LOCAL calendar parts on purpose: the picker is local-time, so a date the
 * user sees must round-trip unchanged rather than shifting across a UTC
 * boundary. (The backend normalises a bare date to UTC midnight; that is the
 * API's contract for a date-only bound, not this formatter's concern.)
 */
export function formatDateInput(date: Date): string {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

/** Calendar days covered by the delivery-log default range. */
export const DEFAULT_DATE_RANGE_DAYS = 30

/**
 * The default `from`/`to` range: the last 30 calendar days, INCLUDING today.
 *
 * `from` is today minus 29 days — subtracting a full 30 would span 31 days.
 * `now` is injectable so a test can pin the range instead of racing the clock.
 */
export function defaultDateRange(now: Date = new Date()): { from: string; to: string } {
  // Clone the caller's Date so `setDate` below cannot mutate it. `now` is
  // already a valid Date, so no invalid-date guard applies here.
  const from = new Date(now) // nosemgrep: new-date-without-guard
  from.setDate(from.getDate() - (DEFAULT_DATE_RANGE_DAYS - 1))
  return { from: formatDateInput(from), to: formatDateInput(now) }
}

/**
 * Build the `from`/`to` query params for a date range, omitting any bound the
 * user cleared. An empty input means "no bound on that side" and must never be
 * sent as an empty string — the backend rejects that as an invalid date.
 */
export function dateRangeQuery(
  from: string | null | undefined,
  to: string | null | undefined,
): { from?: string; to?: string } {
  const range: { from?: string; to?: string } = {}
  if (from) range.from = from
  if (to) range.to = to
  return range
}
