import { format } from 'date-fns'

export function formatDateShort(date: Date | string | number): string {
  return format(new Date(date), 'MMM d, yyyy')
}

export function formatDateShortWithTime(date: Date | string | number): string {
  return format(new Date(date), 'MMM d, yyyy, h:mm a')
}

export function formatDateFilename(date: Date | string | number): string {
  return format(new Date(date), 'yyyy-MM-dd')
}
