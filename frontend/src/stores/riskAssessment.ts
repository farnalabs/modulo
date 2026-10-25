import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import {
  buildRiskAssessmentQuery,
  defaultRiskAssessmentFilters,
  type RiskAssessmentFilters,
  type RiskAssessmentResponse,
} from '../lib/riskAssessment'

export const useRiskAssessmentStore = defineStore('riskAssessment', () => {
  const response = ref<RiskAssessmentResponse | null>(null)
  const isLoading = ref(false)
  const error = ref<string | null>(null)
  const filters = ref<RiskAssessmentFilters>(defaultRiskAssessmentFilters())

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
      const { data, error: responseError, response: httpResponse } = await api.GET(
        '/api/v1/evidence/risk-assessment',
        { params: { query: buildRiskAssessmentQuery(filters.value) } },
      )
      if (responseError || !httpResponse?.ok || !data) {
        throw new Error(
          responseError ? formatApiError(responseError) : `HTTP ${httpResponse?.status ?? 0}`,
        )
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
    filters.value = defaultRiskAssessmentFilters()
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
