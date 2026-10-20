import { shortId } from './format'

/** Badge class for an error group's peak severity level. */
export function levelBadgeClass(level: string): string {
  if (level === 'critical') return 'badge badge-status-destructive'
  if (level === 'warning') return 'badge badge-status-warning'
  return 'badge badge-context-blue'
}

/** Badge class for an error group's triage status. */
export function statusBadgeClass(status: string): string {
  if (status === 'new') return 'badge badge-status-destructive'
  if (status === 'acknowledged') return 'badge badge-status-warning'
  if (status === 'resolved') return 'badge badge-status-success'
  if (status === 'archived') return 'badge badge-status-muted'
  return 'badge'
}

/** Short timestamp shared by the error list and detail surfaces. */
export function formatErrorDate(dateStr: string | null | undefined): string {
  if (!dateStr) return '—'
  const d = new Date(dateStr)
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/** Stable string id for an error-group table row, tolerant of a malformed payload. */
export function errorGroupRowId(row: Record<string, unknown>): string {
  return String(row.id ?? '')
}

/** Assignee column: shortened user id, or an em dash when unassigned. */
export function formatErrorAssignee(row: Record<string, unknown>): string {
  const assignedTo = row.assigned_to
  return typeof assignedTo === 'string' && assignedTo ? shortId(assignedTo) : '—'
}
