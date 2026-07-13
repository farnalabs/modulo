export function runStatusBadgeClass(status: string): string {
  const map: Record<string, string> = {
    complete: 'bg-success/10 text-success',
    failed: 'bg-destructive/10 text-destructive',
    running: 'bg-primary/10 text-primary',
    pending: 'bg-muted text-muted-foreground',
    awaiting_human: 'bg-warning/10 text-warning',
    cancelled: 'bg-muted text-muted-foreground',
    eval_failed: 'bg-destructive/10 text-destructive',
    claimed: 'bg-warning/10 text-warning',
    waiting_for_lock: 'bg-muted text-muted-foreground',
  }
  return map[status] ?? 'bg-muted text-muted-foreground'
}

export function formatRunDate(dateStr: string | null): string {
  if (!dateStr) return '—'
  const d = new Date(dateStr)
  if (isNaN(d.getTime())) return dateStr
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}
