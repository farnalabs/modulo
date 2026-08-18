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

export function triggerTypeLabel(type: string | null | undefined): string {
  const map: Record<string, string> = {
    manual: 'Manual',
    webhook: 'Webhook',
    cron: 'Scheduled',
    polling: 'Polling',
    agent_signal: 'Agent signal',
    ongoing: 'Ongoing',
    correction: 'Correction',
    slack_app_mention: 'Slack mention',
  }
  if (!type) return '—'
  return map[type] ?? type
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
