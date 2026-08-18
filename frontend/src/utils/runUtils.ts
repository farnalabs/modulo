import { toDate } from '../lib/formatDate'

export function runStatusBadgeClass(status: string): string {
  const map: Record<string, string> = {
    complete: 'bg-success/10 text-success',
    failed: 'bg-destructive/10 text-destructive',
    stalled: 'bg-destructive/10 text-destructive',
    budget_exceeded: 'bg-destructive/10 text-destructive',
    running: 'bg-primary/10 text-primary',
    pending: 'bg-muted text-muted-foreground',
    awaiting_human: 'bg-warning/10 text-warning',
    cancelled: 'bg-muted text-muted-foreground',
    eval_failed: 'bg-destructive/10 text-destructive',
    claimed: 'bg-warning/10 text-warning',
  }
  return map[status] ?? 'bg-muted text-muted-foreground'
}

export function formatRunDate(dateStr: string | null): string {
  if (!dateStr) return '—'
  const d = toDate(dateStr)
  if (!d) return dateStr
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/** Human-readable label for a dotted run error code (e.g. `agent.stall` →
 * "Agent stalled"), looked up in the locale's `errorCodes` section. Falls back
 * to the locale's `errorCodes._unknown` label when the code has no entry. */
export function errorCodeLabel(code: string | null | undefined, t: (key: string) => string): string {
  if (!code) return '—'
  const key = `errorCodes.${code}`
  const translated = t(key)
  return translated === key ? t('errorCodes._unknown') : translated
}
