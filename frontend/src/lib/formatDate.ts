import { format } from 'date-fns'

function toDate(date: Date | string | number | null | undefined): Date | null {
  if (date == null) return null
  const d = date instanceof Date ? date : new Date(date)
  return isNaN(d.getTime()) ? null : d
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
