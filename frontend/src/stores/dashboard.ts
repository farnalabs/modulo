import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api } from '../lib/api/client'

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

export const useDashboardStore = defineStore('dashboard', () => {
  const summary = ref<DashboardSummary | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  const totalSpend = computed(() => {
    if (!summary.value?.trend) return 0
    return summary.value.trend.reduce((sum, d) => sum + d.token_spend_usd, 0)
  })

  async function fetchSummary() {
    loading.value = true
    error.value = null
    try {
      const { data: result, error: err } = await api.GET('/api/v1/dashboard/summary')
      if (err) {
        error.value = String(err)
      } else {
        summary.value = result as unknown as DashboardSummary
      }
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  return { summary, loading, error, totalSpend, fetchSummary }
})
