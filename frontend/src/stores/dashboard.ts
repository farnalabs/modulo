import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '../lib/api/client'

interface DashboardSummary {
  total_runs: number
  active_pipelines: number
  run_counts_by_status: {
    running: number
    awaiting_human: number
    failed: number
    idle: number
  }
}

export const useDashboardStore = defineStore('dashboard', () => {
  const summary = ref<DashboardSummary | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

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

  return { summary, loading, error, fetchSummary }
})
