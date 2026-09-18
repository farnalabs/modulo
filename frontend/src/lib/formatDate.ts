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
