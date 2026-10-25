import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'

// Type definitions for the risk assessment API (will be in generated schema after regen)
interface RiskAssessmentSummary {
  total_rows: number
  action_counts: Record<string, number>
  facet_degraded: boolean
}

interface RiskAssessmentResponse {
  summary: RiskAssessmentSummary
  rows: Array<{
    id: string
    organisation_id: string
    key: string
    subject_type: string
    subject_id: string
    value: unknown
    observed_at: string
    producer_type: string
    producer_id: string | null
    created_at: string
    updated_at: string
  }>
}

interface RiskAssessmentFilters {
  keyPrefix: string
  pipelineId: string
  days: number
  action: string
  limit: number
}

export const useRiskAssessmentStore = defineStore('riskAssessment', () => {
  const response = ref<RiskAssessmentResponse | null>(null)
  const isLoading = ref(false)
  const error = ref<string | null>(null)
  const filters = ref<RiskAssessmentFilters>({
    keyPrefix: '',
    pipelineId: '',
    days: 7,
    action: '',
    limit: 100,
  })

  const rows = computed(() => response.value?.rows ?? [])
  const summary = computed(() => response.value?.summary)
  const totalRows = computed(() => response.value?.summary?.total_rows ?? 0)
  const actionCounts = computed(() => response.value?.summary?.action_counts ?? {})
  const facetDegraded = computed(() => response.value?.summary?.facet_degraded ?? false)

  async function fetchRiskAssessment(): Promise<void> {
    if (isLoading.value) return
    isLoading.value = true
    error.value = null
    try {
      const params: Record<string, string | number | boolean | undefined> = {}
      if (filters.value.keyPrefix) params.key_prefix = filters.value.keyPrefix
      if (filters.value.pipelineId) params.pipeline_id = filters.value.pipelineId
      if (filters.value.days) params.days = filters.value.days
      if (filters.value.action) params.action = filters.value.action
      if (filters.value.limit) params.limit = filters.value.limit

      const { data, error: responseError, response: httpResponse } = await api.GET(
        '/api/v1/evidence/risk-assessment' as any,
        { params: { query: params } }
      )
      if (responseError || !httpResponse?.ok || !data) {
        throw new Error(formatApiError(responseError) || `HTTP ${httpResponse?.status ?? 0}`)
      }
      response.value = data as RiskAssessmentResponse
    } catch (err) {
      error.value = formatApiError(err)
    } finally {
      isLoading.value = false
    }
  }

  function setFilters(newFilters: Partial<RiskAssessmentFilters>): void {
    filters.value = { ...filters.value, ...newFilters }
  }

  function resetFilters(): void {
    filters.value = {
      keyPrefix: '',
      pipelineId: '',
      days: 7,
      action: '',
      limit: 100,
    }
  }

  return {
    response,
    rows,
    summary,
    totalRows,
    actionCounts,
    facetDegraded,
    isLoading,
    error,
    filters,
    fetchRiskAssessment,
    setFilters,
    resetFilters,
  }
})
