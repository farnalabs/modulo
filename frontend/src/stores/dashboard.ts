import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api } from '../lib/api/client'
import { registerHandler } from './syncRegistry'
import type { EventBusEvent } from '@/types/events'

interface TeamMetrics {
  id: string
  name: string
  total_runs: number
  active_pipelines: number
  run_counts_by_status: {
    running: number
    awaiting_human: number
    failed: number
    idle: number
  }
  eval_pass_rate?: {
    total_evals: number
    passed_evals: number
    pass_rate: number
  }
}

interface TrendDay {
  date: string
  run_count: number
  eval_pass_rate: number | null
  token_spend_usd: number
}

interface RecentRun {
  id: string
  pipeline_name: string
  status: string
  created_at: string
  trigger_type: string
}

export interface DashboardSummary {
  total_runs: number
  active_pipelines: number
  run_counts_by_status: {
    running: number
    awaiting_human: number
    failed: number
    idle: number
  }
  teams: TeamMetrics[]
  eval_pass_rate: {
    overall_pass_rate: number
    total_evals: number
    passed_evals: number
    per_pipeline: Record<string, { total_evals: number; passed_evals: number; pass_rate: number }>
    per_team_pipeline: Record<string, Record<string, { total_evals: number; passed_evals: number; pass_rate: number }>>
  } | null
  trend: TrendDay[]
  recent_runs: RecentRun[]
}

function validateDashboardSummary(data: unknown): DashboardSummary | null {
  if (!data || typeof data !== 'object') {
    console.warn('[Dashboard] validateDashboardSummary failed: data is not an object', typeof data)
    return null
  }
  const d = data as Record<string, unknown>
  if (typeof d.total_runs !== 'number') {
    console.warn('[Dashboard] validateDashboardSummary failed: total_runs is not a number', d.total_runs)
    return null
  }
  if (typeof d.active_pipelines !== 'number') {
    console.warn('[Dashboard] validateDashboardSummary failed: active_pipelines is not a number', d.active_pipelines)
    return null
  }
  if (!d.run_counts_by_status || typeof d.run_counts_by_status !== 'object') {
    console.warn('[Dashboard] validateDashboardSummary failed: run_counts_by_status is missing or not an object')
    return null
  }
  const rcs = d.run_counts_by_status as Record<string, unknown>
  if (typeof rcs.running !== 'number' || typeof rcs.awaiting_human !== 'number' || typeof rcs.failed !== 'number' || typeof rcs.idle !== 'number') {
    console.warn('[Dashboard] validateDashboardSummary failed: run_counts_by_status fields are not all numbers')
    return null
  }
  if (!Array.isArray(d.teams)) {
    console.warn('[Dashboard] validateDashboardSummary failed: teams is not an array')
    return null  
  }
  if (!Array.isArray(d.trend)) {
    console.warn('[Dashboard] validateDashboardSummary failed: trend is not an array')
    return null
  }
  if (!Array.isArray(d.recent_runs)) {
    console.warn('[Dashboard] validateDashboardSummary failed: recent_runs is not an array')
    return null
  }
  return d as unknown as DashboardSummary
}

export const useDashboardStore = defineStore('dashboard', () => {
  const summary = ref<DashboardSummary | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)
  const syncingIds = ref(new Set<string>())
  const unsubHandlers: (() => void)[] = []

  const totalSpend = computed(() => {
    if (!summary.value?.trend) return 0
    return summary.value.trend.reduce((sum, d) => sum + d.token_spend_usd, 0)
  })

  async function fetchSummary() {
    if (loading.value) return
    loading.value = true
    error.value = null
    try {
      const { data: result, error: err } = await api.GET('/api/v1/dashboard/summary')
      if (err) {
        error.value = String(err)
      } else {
        summary.value = validateDashboardSummary(result)
      }
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  const trends = ref<{
    run_counts: Array<{ date: string; run_count: number }>
    eval_pass_rates: Array<{ date: string; total_evals: number; passed_evals: number; pass_rate: number | null }>
    token_spend: Array<{ date: string; total_spend_usd: number }>
  } | null>(null)

  const trendsLoading = ref(false)

  async function fetchTrends(days: number) {
    if (trendsLoading.value) return
    trendsLoading.value = true
    try {
      const { data: result, error: err } = await api.GET('/api/v1/dashboard/trends', {
        params: { query: { days } },
      })
      if (err) {
        console.warn('[Dashboard] fetchTrends failed:', err)
      } else if (result) {
        trends.value = result as any
      }
    } catch (e: unknown) {
      console.warn('[Dashboard] fetchTrends error:', e)
    } finally {
      trendsLoading.value = false
    }
  }

  function handleSyncEvent(event: EventBusEvent): void {
    if (!syncingIds.value.has(event.id)) {
      syncingIds.value.add(event.id)
      if (event.type === 'run' || event.type === 'pipeline') {
        void fetchSummary().finally(() => {
          syncingIds.value.delete(event.id)
        })
      }
    }
  }

  unsubHandlers.push(registerHandler('run', handleSyncEvent))
  unsubHandlers.push(registerHandler('pipeline', handleSyncEvent))

  if (import.meta.hot) {
    import.meta.hot.dispose(() => { disposeHandlers() })
  }

  function disposeHandlers(): void {
    for (const unsub of unsubHandlers) unsub()
    unsubHandlers.length = 0
    syncingIds.value.clear()
  }

  return { summary, trends, loading, trendsLoading, error, totalSpend, fetchSummary, fetchTrends, disposeHandlers }
})
