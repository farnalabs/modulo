<template>
  <div class="page-wide space-y-6">
    <PageHeader
      :title="$t('views.RiskAssessmentView.title')"
      :subtitle="$t('views.RiskAssessmentView.subtitle')"
    />

    <RiskAssessmentFacets
      :filters="filters"
      :facetDegraded="facetDegraded"
      :isLoading="isLoading"
      :pipelines="pipelines"
      :pipelinesError="pipelinesError ?? undefined"
      @update:filters="updateFilters"
      @apply="applyFilters"
      @reset="resetFilters"
    />

    <div v-if="isLoading" aria-live="polite" class="flex items-center gap-3 text-sm text-muted-foreground" data-testid="risk-assessment-loading">
      <LoadingSpinner />
      <span>{{ $t('views.RiskAssessmentView.loading') }}</span>
    </div>

    <ErrorAlert v-else-if="error" :message="error" :on-retry="applyFilters" data-testid="risk-assessment-error" />

    <template v-else>
      <RiskAssessmentSummaryCard
        :totalRows="totalRows"
        :actionCounts="actionCounts"
        :facetDegraded="facetDegraded"
        :timeWindow="filters.days"
      />

      <RiskAssessmentTable :rows="rows" data-testid="risk-assessment-table" />
    </template>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import {
  defaultRiskAssessmentFilters,
  riskAssessmentRouteQuery,
  type RiskAssessmentFilters,
} from '../lib/riskAssessment'
import PageHeader from '../components/shared/PageHeader.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import RiskAssessmentSummaryCard from '../components/risk-assessment/RiskAssessmentSummaryCard.vue'
import RiskAssessmentTable from '../components/risk-assessment/RiskAssessmentTable.vue'
import RiskAssessmentFacets from '../components/risk-assessment/RiskAssessmentFacets.vue'
import { useRiskAssessmentStore } from '../stores/riskAssessment'

interface Pipeline {
  id: string
  name: string
}

const route = useRoute()
const router = useRouter()

const store = useRiskAssessmentStore()
const { rows, totalRows, actionCounts, facetDegraded, isLoading, error } = storeToRefs(store)

const pipelines = ref<Pipeline[]>([])
const pipelinesError = ref<string | null>(null)

const filters = ref<RiskAssessmentFilters>(defaultRiskAssessmentFilters())

async function loadPipelines(): Promise<void> {
  pipelinesError.value = null
  try {
    const { data, error: responseError, response } = await api.GET('/api/v1/pipelines', {
      params: { query: { limit: 100, page: 1 } },
    })
    if (responseError || !response?.ok) {
      throw new Error(
        responseError ? formatApiError(responseError) : `HTTP ${response?.status ?? 0}`,
      )
    }
    pipelines.value = data?.items ?? []
  } catch (err) {
    // Non-fatal: the projection still loads, but the operator must know the
    // pipeline facet is empty because its source failed (never a silent warn).
    pipelinesError.value = formatApiError(err)
  }
}

function updateFilters(newFilters: RiskAssessmentFilters): void {
  filters.value = { ...newFilters }
}

async function applyFilters(): Promise<void> {
  store.setFilters({ ...filters.value })
  await router.replace({ query: riskAssessmentRouteQuery(filters.value) })
  await store.fetchRiskAssessment()
}

function resetFilters(): void {
  filters.value = defaultRiskAssessmentFilters()
  void applyFilters()
}

function filtersFromRoute(): RiskAssessmentFilters {
  const filters = defaultRiskAssessmentFilters()
  const { key_prefix: keyPrefix, pipeline_id: pipelineId, days, action } = route.query
  if (typeof keyPrefix === 'string') filters.keyPrefix = keyPrefix
  if (typeof pipelineId === 'string') filters.pipelineId = pipelineId
  if (typeof days === 'string') {
    const parsed = parseInt(days, 10)
    if (!Number.isNaN(parsed)) filters.days = parsed
  }
  if (typeof action === 'string') filters.action = action
  return filters
}

onMounted(async () => {
  filters.value = filtersFromRoute()
  await loadPipelines()
  await applyFilters()
})
</script>
