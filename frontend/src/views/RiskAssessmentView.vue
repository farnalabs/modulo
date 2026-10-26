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
      @retryPipelines="loadPipelines"
    />

    <div v-if="isLoading" aria-live="polite" class="flex items-center gap-3 text-sm text-muted-foreground" data-testid="risk-assessment-loading">
      <LoadingSpinner />
      <span>{{ $t('views.RiskAssessmentView.loading') }}</span>
    </div>

    <ErrorAlert v-else-if="error" :message="t('views.RiskAssessmentView.load_failed')" :on-retry="retryAll" data-testid="risk-assessment-error" />

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
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../lib/api/client'
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
const { t } = useI18n()

const store = useRiskAssessmentStore()
const { rows, totalRows, actionCounts, facetDegraded, isLoading, error } = storeToRefs(store)

const pipelines = ref<Pipeline[]>([])
const pipelinesError = ref<string | null>(null)

const filters = ref<RiskAssessmentFilters>(defaultRiskAssessmentFilters())

// The pipeline list endpoint ignores `limit` and caps `page_size` at 100; the
// facet therefore has to page through the offset API or it silently renders
// only the first page (20 items by default) of selectable pipelines.
const PIPELINES_PAGE_SIZE = 100
const PIPELINES_MAX_PAGES = 20

interface PipelinePage {
  items: Pipeline[]
  hasMore: boolean
}

async function fetchPipelinePage(page: number): Promise<PipelinePage> {
  const { data, error: responseError, response } = await api.GET('/api/v1/pipelines', {
    params: { query: { page, page_size: PIPELINES_PAGE_SIZE } },
  })
  if (responseError || !response?.ok) {
    throw new Error(t('views.RiskAssessmentView.pipelines_load_failed'))
  }
  return {
    items: data?.items ?? [],
    hasMore: Boolean(data?.has_more),
  }
}

async function loadPipelines(): Promise<void> {
  pipelinesError.value = null
  try {
    const collected: Pipeline[] = []
    for (let page = 1; page <= PIPELINES_MAX_PAGES; page += 1) {
      const result = await fetchPipelinePage(page)
      collected.push(...result.items)
      if (!result.hasMore) {
        pipelines.value = collected
        return
      }
    }
    // Exhausting the page budget with pages still available means the list we
    // have is a prefix. Say so rather than dressing it up as the whole set (a
    // missing pipeline is an unfilterable facet, not an empty one).
    pipelines.value = collected
    pipelinesError.value = t('views.RiskAssessmentView.pipelines_truncated')
  } catch {
    // A page failure must not publish the prefix collected so far: a partial
    // dropdown silently hides pipelines. Keep the previous list, surface the
    // failure, and let the retry replace it.
    pipelinesError.value = t('views.RiskAssessmentView.pipelines_load_failed')
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

async function retryAll(): Promise<void> {
  await loadPipelines()
  await applyFilters()
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
    const parsed = Number.parseInt(days, 10)
    if (!Number.isNaN(parsed)) filters.days = parsed
  }
  if (typeof action === 'string') filters.action = action
  return filters
}

onMounted(async () => {
  filters.value = filtersFromRoute()
  await retryAll()
})
</script>
