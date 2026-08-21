export function shortId(id: string | null | undefined): string {
  if (!id) return '\u2014'
  return '#' + id.slice(0, 8)
}

export function formatRun(run: { pipeline_name?: string | null; run_number?: number | null; run_id?: string } | null | undefined): string {
  if (!run) return '\u2014'
  const name = run.pipeline_name || 'Run'
  const num = run.run_number != null ? `#${run.run_number}` : shortId(run.run_id)
  return `${name} ${num}`
}
