import { format, formatDistanceToNow } from 'date-fns'

/**
 * Safely parses a date-like value, returning null for invalid input.
 * Shared single source of truth for parsing user/API-supplied timestamps.
 */
export function toDate(date: Date | string | number | null | undefined): Date | null {
  if (date == null) return null
  const d = date instanceof Date ? date : new Date(date) // nosemgrep: new-date-without-guard
  return Number.isNaN(d.getTime()) ? null : d
}

export function formatDateShort(date: Date | string | number | null | undefined): string {
  const d = toDate(date)
  if (!d) return '—'
  return format(d, 'MMM d, yyyy')
}

export function formatDateShortWithTime(date: Date | string | number | null | undefined): string {
  const d = toDate(date)
  if (!d) return '—'
  return format(d, 'MMM d, yyyy, h:mm a')
}

export function formatDateFilename(date: Date | string | number | null | undefined): string {
  const d = toDate(date)
  if (!d) return '—'
  return format(d, 'yyyy-MM-dd')
}

export function formatRelativeTime(date: Date | string | number | null | undefined): string {
  const d = toDate(date)
  if (!d) return '—'
  return formatDistanceToNow(d, { addSuffix: true })
}
