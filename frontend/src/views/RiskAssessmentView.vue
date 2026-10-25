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
import { onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
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

interface FilterState {
  keyPrefix: string
  pipelineId: string
  days: number
  action: string
  limit: number
}

interface EvidenceRow {
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
}

const route = useRoute()

const store = useRiskAssessmentStore()

const pipelines = ref<Pipeline[]>([])
const isLoadingPipelines = ref(false)

const filters = ref<FilterState>({
  keyPrefix: '',
  pipelineId: '',
  days: 7,
  action: '',
  limit: 100,
})

const rows = ref<EvidenceRow[]>([])
const totalRows = ref(0)
const actionCounts = ref<Record<string, number>>({})
const facetDegraded = ref(false)
const isLoading = ref(false)
const error = ref<string | null>(null)

async function loadPipelines(): Promise<void> {
  if (isLoadingPipelines.value) return
  isLoadingPipelines.value = true
  try {
    const { data, error: responseError, response } = await api.GET('/api/v1/pipelines', {
      params: { query: { limit: 100, page: 1 } },
    })
    if (responseError || !response?.ok) {
      throw new Error(formatApiError(responseError) || `HTTP ${response?.status ?? 0}`)
    }
    pipelines.value = data?.items ?? []
  } catch (err) {
    console.warn('Failed to load pipelines for risk assessment filter:', err)
  } finally {
    isLoadingPipelines.value = false
  }
}

function updateFilters(newFilters: FilterState): void {
  filters.value = { ...newFilters }
}

async function applyFilters(): Promise<void> {
  store.setFilters({
    keyPrefix: filters.value.keyPrefix || undefined,
    pipelineId: filters.value.pipelineId || undefined,
    days: filters.value.days,
    action: filters.value.action || undefined,
    limit: filters.value.limit,
  })
  await store.fetchRiskAssessment()
  syncFromStore()
}

function resetFilters(): void {
  filters.value = {
    keyPrefix: '',
    pipelineId: '',
    days: 7,
    action: '',
    limit: 100,
  }
  store.resetFilters()
  applyFilters()
}

function syncFromStore(): void {
  rows.value = store.rows as EvidenceRow[]
  totalRows.value = store.totalRows
  actionCounts.value = store.actionCounts
  facetDegraded.value = store.facetDegraded
  isLoading.value = store.isLoading
  error.value = store.error
}

watch(
  () => [store.rows, store.totalRows, store.actionCounts, store.facetDegraded, store.isLoading, store.error],
  () => {
    syncFromStore()
  },
  { deep: true }
)

onMounted(async () => {
  await loadPipelines()

  // Initialize from URL query params
  const queryKeyPrefix = route.query.key_prefix
  const queryPipelineId = route.query.pipeline_id
  const queryDays = route.query.days
  const queryAction = route.query.action

  if (typeof queryKeyPrefix === 'string') filters.value.keyPrefix = queryKeyPrefix
  if (typeof queryPipelineId === 'string') filters.value.pipelineId = queryPipelineId
  if (typeof queryDays === 'string') filters.value.days = parseInt(queryDays, 10) || 7
  if (typeof queryAction === 'string') filters.value.action = queryAction

  await applyFilters()
})
</script>
