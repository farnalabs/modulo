<template>
  <PageTabs :tabs="[
    { label: 'Evals', to: '/evals/editor' },
    { label: 'Proposals', to: '/evals/proposals' },
    { label: 'Variants', to: '/variants/compare' },
  ]" />

  <div class="page-wide">
    <LoadingSpinner v-if="loading" />
    <ErrorAlert v-else-if="error" :message="error" />
    <template v-else>
      <PageHeader :title="$t('views.variantBatch.title')" :subtitle="$t('views.variantBatch.subtitle')" />

      <div v-if="batch" class="space-y-6">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div class="space-y-1">
            <h2 class="text-lg font-semibold">{{ batch.name }}</h2>
            <div class="flex flex-wrap items-center gap-3 text-sm text-muted-foreground">
              <output
                data-testid="variant-batch-status"
                class="inline-flex items-center gap-1.5"
              >
                <span class="h-2 w-2 rounded-full" :class="statusDotClass"></span>
                {{ $t(batchStatusKey) }}
              </output>
              <span v-if="batch.pipeline_name">{{ batch.pipeline_name }}</span>
              <span class="tabular-nums">{{ batch.runs.length }} {{ $t('views.variantBatch.runs') }}</span>
            </div>
          </div>
          <div class="flex flex-wrap gap-3">
            <Button
              type="button"
              data-testid="variant-batch-refire"
              :disabled="refiring"
              @click="handleReFire"
            >
              <span v-if="refiring" class="inline-block h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
              {{ refiring ? $t('views.variantBatch.refiring') : $t('views.variantBatch.reFire') }}
            </Button>
            <Button
              type="button"
              data-testid="variant-batch-back"
              variant="outlined"
              class="border border-input bg-background text-sm font-medium hover:bg-muted/50"
              @click="router.push('/variants/compare')"
            >
              {{ $t('views.variantBatch.backToComparisons') }}
            </Button>
          </div>
        </div>

        <output v-if="hasPartialResults" data-testid="variant-batch-partial" class="rounded-lg border border-warning/40 bg-warning/10 px-4 py-3 text-sm text-warning">
          {{ $t('views.variantBatch.partialResults') }}
        </output>
        <output v-if="batchFailed" data-testid="variant-batch-failed" class="rounded-lg border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
          {{ $t('views.variantBatch.batchFailed') }}
        </output>

        <div class="overflow-x-auto rounded-lg border bg-card">
          <table class="w-full text-left text-sm">
            <thead>
              <tr class="border-b bg-muted/50">
                <th class="px-4 py-3 font-semibold">{{ $t('views.variantBatch.label') }}</th>
                <th class="px-4 py-3 font-semibold">{{ $t('views.variantBatch.input') }}</th>
                <th class="px-4 py-3 font-semibold">{{ $t('views.variantBatch.statusHeader') }}</th>
                <th class="px-4 py-3 font-semibold">{{ $t('views.variantBatch.passRate') }}</th>
                <th class="px-4 py-3 font-semibold">{{ $t('views.variantBatch.cost') }}</th>
                <th class="px-4 py-3 font-semibold"></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="run in rankedRuns" :key="run.run_id" class="border-b last:border-b-0 hover:bg-muted/30">
                <td class="px-4 py-3 font-mono text-xs">{{ run.variant_name }}</td>
                <td class="px-4 py-3 text-xs text-muted-foreground">
                  <div v-if="run.snapshot_label || run.input_label" class="space-y-0.5">
                    <div v-if="run.snapshot_label" class="font-medium">{{ run.snapshot_label }}</div>
                    <div v-if="run.input_label">{{ run.input_label }}</div>
                  </div>
                  <span v-else>&mdash;</span>
                </td>
                <td class="px-4 py-3">
                  <span :class="statusBadgeClass(run)" data-testid="variant-batch-status-badge">
                    {{ statusLabel(run) }}
                  </span>
                </td>
                <td class="px-4 py-3">
                  <span v-if="run.pass_rate !== null" :class="passRateClass(run.pass_rate)" data-testid="variant-batch-pass-rate">
                    {{ run.pass_rate.toFixed(0) }}%
                  </span>
                  <span v-else class="text-muted-foreground">&mdash;</span>
                </td>
                <td class="px-4 py-3 font-mono text-xs tabular-nums">
                  <span v-if="run.total_cost_usd !== null">{{ formatMoney(Number(run.total_cost_usd), currencyCode, 6) }}</span>
                  <span v-else class="text-muted-foreground">&mdash;</span>
                </td>
                <td class="px-4 py-3 text-right">
                  <button
                    type="button"
                    class="inline-flex items-center gap-1 rounded px-2 py-1 text-xs font-medium text-muted-foreground hover:bg-muted/50 hover:text-foreground"
                    :aria-label="$t(expandedRunIds.has(run.run_id) ? 'views.variantBatch.collapseDetail' : 'views.variantBatch.expandDetail', { label: run.variant_name })"
                    :aria-expanded="expandedRunIds.has(run.run_id)"
                    :data-testid="`variant-batch-expand-${run.run_id}`"
                    @click="toggleExpand(run.run_id)"
                  >
                    <span class="inline-block transition-transform" :class="expandedRunIds.has(run.run_id) ? 'rotate-90' : ''">&#9654;</span>
                    {{ expandedRunIds.has(run.run_id) ? $t('views.variantBatch.collapse') : $t('views.variantBatch.expand') }}
                  </button>
                </td>
              </tr>
              <tr v-if="rankedRuns.length === 0">
                <td colspan="6" class="px-4 py-8 text-center text-sm text-muted-foreground">
                  {{ $t('views.variantBatch.noRuns') }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div v-if="expandedRunIds.size > 0" class="space-y-4">
          <h3 class="text-base font-semibold">{{ $t('views.variantBatch.detailTitle') }}</h3>
          <div v-for="run in rankedRuns.filter(r => expandedRunIds.has(r.run_id))" :key="run.run_id" class="rounded-lg border bg-card p-4" :data-testid="`variant-batch-detail-${run.run_id}`">
            <div class="mb-2 flex items-center justify-between">
              <span class="font-mono text-xs font-semibold">{{ run.variant_name }}</span>
              <a :href="`/runs/${run.run_id}`" class="text-xs text-primary hover:underline" :data-testid="`variant-batch-run-link-${run.run_id}`">
                {{ $t('views.variantBatch.viewRun') }}
              </a>
            </div>
            <div v-if="run.eval_results.length > 0" class="mb-3">
              <div class="mb-1 text-xs font-medium text-muted-foreground">{{ $t('views.variantBatch.evals') }}</div>
              <div class="flex flex-wrap gap-2">
                <span
                  v-for="er in run.eval_results"
                  :key="er.eval_id"
                  class="inline-flex items-center gap-1 rounded bg-muted px-1.5 py-0.5 text-[10px] tabular-nums"
                  :class="er.passed ? 'text-success' : 'text-destructive'"
                  :title="er.detail ?? undefined"
                >
                  {{ er.node_id }}: {{ er.score !== null ? er.score.toFixed(2) : '-' }} {{ $t(er.passed ? 'views.variantBatch.pass' : 'views.variantBatch.fail') }}
                </span>
              </div>
            </div>
            <div v-if="run.node_outputs" class="overflow-auto rounded border">
              <JsonViewer :data="run.node_outputs" :show-toolbar="false" :max-height="'16rem'" />
            </div>
            <div v-else class="text-xs text-muted-foreground">{{ $t('views.variantBatch.noOutput') }}</div>
          </div>
        </div>
      </div>

      <EmptyState
        v-else-if="!batch"
        :title="$t('views.variantBatch.notFoundTitle')"
        :description="$t('views.variantBatch.notFoundDescription')"
      />
    </template>
  </div>

  <div class="page-wide mt-8 border-t pt-6">
    <div class="mb-3 flex items-center justify-between">
      <h2 class="text-base font-semibold">{{ $t('views.variantBatch.myComparisons') }}</h2>
      <router-link to="/variants/compare" class="text-sm text-primary hover:underline" data-testid="variant-batch-new-comparison">
        {{ $t('views.variantBatch.createComparison') }}
      </router-link>
    </div>
    <LoadingSpinner v-if="comparisonsLoading" />
    <ErrorAlert v-else-if="comparisonsError" :message="comparisonsError" />
    <template v-else>
      <div v-if="comparisons.length === 0" class="rounded-lg border bg-card p-8 text-center text-sm text-muted-foreground">
        {{ $t('views.variantBatch.noComparisons') }}
      </div>
      <div v-else class="overflow-x-auto rounded-lg border bg-card">
        <table class="w-full text-left text-sm">
          <thead>
            <tr class="border-b bg-muted/50">
              <th class="px-4 py-3 font-semibold">{{ $t('views.variantBatch.name') }}</th>
              <th class="px-4 py-3 font-semibold">{{ $t('views.variantBatch.statusHeader') }}</th>
              <th class="px-4 py-3 font-semibold">{{ $t('views.variantBatch.runs') }}</th>
              <th class="px-4 py-3 font-semibold"></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="cmp in comparisons" :key="cmp.batch_id" class="border-b last:border-b-0 hover:bg-muted/30">
              <td class="px-4 py-3">
                <router-link :to="`/variants/compare/${cmp.batch_id}`" class="font-medium text-primary hover:underline" :data-testid="`variant-batch-link-${cmp.batch_id}`">
                  {{ cmp.name }}
                </router-link>
              </td>
              <td class="px-4 py-3">
                  <span :class="batchStatusBadgeClass(cmp.status)">
                    {{ batchStatusLabel(cmp.status) }}
                  </span>
              </td>
              <td class="px-4 py-3 tabular-nums text-muted-foreground">{{ cmp.run_count }}</td>
              <td class="px-4 py-3 text-right">
                <button
                  type="button"
                  class="text-xs text-destructive hover:underline disabled:opacity-50"
                  :disabled="deletingId === cmp.batch_id"
                  :data-testid="`variant-batch-delete-${cmp.batch_id}`"
                  @click="handleSoftDelete(cmp)"
                >
                  {{ deletingId === cmp.batch_id ? $t('views.variantBatch.deleting') : $t('views.variantBatch.delete') }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </div>
</template>


<script setup lang="ts">
import { ref, computed, watch, onBeforeUnmount } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import JsonViewer from '../components/shared/JsonViewer.vue'
import PageHeader from '../components/shared/PageHeader.vue'
import PageTabs from '../components/PageTabs.vue'
import Button from 'primevue/button'
import EmptyState from '../components/shared/EmptyState.vue'
import { formatMoney } from '../lib/money'
import { useOrgCurrency } from '../composables/useOrgCurrency'
import {
  fetchVariantBatch,
  fetchVariantBatches,
  softDeleteVariantBatch,
  reFireVariantBatch,
  type VariantBatchDetail,
  type VariantBatchRun,
  type VariantBatchSummary,
} from '../lib/api/variantBatches'
import { TERMINAL_STATUSES, NON_TERMINAL_STATUSES } from '../constants/runStatuses'
import { formatApiError } from '../lib/api/formatError'

const { t } = useI18n()
const { currencyCode, loadCurrency } = useOrgCurrency()
const route = useRoute()
const router = useRouter()

const batchId = computed(() => (route.params.batchId as string) ?? '')

const loading = ref(false)
const error = ref<string | null>(null)
const batch = ref<VariantBatchDetail | null>(null)
const expandedRunIds = ref<Set<string>>(new Set())
const refiring = ref(false)
const deletingId = ref<string | null>(null)
const comparisonsLoading = ref(false)
const comparisons = ref<VariantBatchSummary[]>([])
const comparisonsError = ref<string | null>(null)

const terminalStatuses: Set<string> = new Set(TERMINAL_STATUSES)
const nonTerminalStatuses: Set<string> = new Set(NON_TERMINAL_STATUSES)

const rankedRuns = computed<VariantBatchRun[]>(() => {
  const runs = batch.value?.runs ?? []
  return [...runs].sort((a, b) => {
    const aPass = a.pass_rate ?? -1
    const bPass = b.pass_rate ?? -1
    if (bPass !== aPass) return bPass - aPass
    return a.variant_name.localeCompare(b.variant_name)
  })
})

const passedCount = computed(() => rankedRuns.value.filter(r => r.run_status === 'complete').length)
const failedCount = computed(() =>
  rankedRuns.value.filter(r => terminalStatuses.has(r.run_status) && r.run_status !== 'complete').length,
)

const hasPartialResults = computed(() => passedCount.value > 0 && failedCount.value > 0)

const batchComplete = computed(() => batch.value?.status === 'complete')
const batchFailed = computed(() => batch.value?.status === 'failed')
const batchRunning = computed(() => batch.value?.status === 'running' || batch.value?.status === 'pending')

const batchStatusKey = computed(() => {
  const key = `views.variantBatch.batchStatus.${batch.value?.status ?? 'unknown'}`
  return key
})

const statusDotClass = computed(() => {
  if (batchComplete.value) return 'bg-success'
  if (batchFailed.value) return 'bg-destructive'
  if (batchRunning.value) return 'bg-warning animate-pulse'
  return 'bg-muted-foreground'
})

function statusBadgeClass(run: VariantBatchRun): string {
  if (run.run_status === 'complete') return 'badge badge-status-success capitalize'
  if (run.run_status === 'failed' || run.run_status === 'stalled' || run.run_status === 'budget_exceeded') {
    return 'badge badge-status-destructive capitalize'
  }
  if (run.run_status === 'cancelled' || run.run_status === 'eval_failed') return 'badge badge-status-warning capitalize'
  if (nonTerminalStatuses.has(run.run_status)) return 'badge badge-status-info capitalize'
  return 'badge badge-status-muted capitalize'
}

function statusLabel(run: VariantBatchRun): string {
  const key = `views.variantBatch.status.${run.run_status}`
  const translated = t(key)
  return translated === key ? run.run_status : translated
}

function batchStatusBadgeClass(status: string): string {
  if (status === 'complete') return 'badge badge-status-success capitalize'
  if (status === 'failed') return 'badge badge-status-destructive capitalize'
  if (status === 'partial' || status === 'cancelled') return 'badge badge-status-warning capitalize'
  if (status === 'running' || status === 'pending') return 'badge badge-status-info capitalize'
  return 'badge badge-status-muted capitalize'
}

function batchStatusLabel(status: string): string {
  const key = `views.variantBatch.batchStatus.${status}`
  const translated = t(key)
  return translated === key ? status : translated
}


function passRateClass(rate: number): string {
  if (rate >= 80) return 'badge badge-status-success'
  if (rate >= 40) return 'badge badge-status-warning'
  return 'badge badge-status-destructive'
}

function toggleExpand(runId: string) {
  const next = new Set(expandedRunIds.value)
  if (next.has(runId)) next.delete(runId)
  else next.add(runId)
  expandedRunIds.value = next
}

async function loadBatch(id: string) {
  const thisId = id
  loading.value = true
  error.value = null
  try {
    const { data, error: err } = await fetchVariantBatch(id)
    if (thisId !== batchId.value) return
    if (err) {
      error.value = `${t('views.variantBatch.failedToLoadBatch')} ${formatApiError(err)}`
      return
    }
    batch.value = data ?? null
  } catch (e: unknown) {
    if (thisId !== batchId.value) return
    error.value = `${t('views.variantBatch.failedToLoadBatch')} ${formatApiError(e)}`
  } finally {
    if (thisId === batchId.value) loading.value = false
  }
}

async function loadComparisons() {
  comparisonsLoading.value = true
  comparisonsError.value = null
  try {
    const { data, error: err } = await fetchVariantBatches()
    if (err) {
      comparisonsError.value = err
      return
    }
    comparisons.value = data?.items ?? []
  } catch (e: unknown) {
    comparisonsError.value = formatApiError(e)
  } finally {
    comparisonsLoading.value = false
  }
}

async function handleSoftDelete(batch: VariantBatchSummary) {
  deletingId.value = batch.batch_id
  error.value = null
  try {
    const { error: err } = await softDeleteVariantBatch(batch.batch_id)
    if (err) {
      error.value = `${t('views.variantBatch.failedToDelete')} ${formatApiError(err)}`
      return
    }
    await loadComparisons()
  } catch (e: unknown) {
    error.value = `${t('views.variantBatch.failedToDelete')} ${formatApiError(e)}`
  } finally {
    deletingId.value = null
  }
}

async function handleReFire() {
  if (!batchId.value) return
  const thisId = batchId.value
  refiring.value = true
  error.value = null
  try {
    const { data, error: err } = await reFireVariantBatch(thisId)
    if (thisId !== batchId.value) return
    if (err) {
      error.value = `${t('views.variantBatch.refireFailed')} ${formatApiError(err)}`
      return
    }
    if (data) {
      batch.value = data
      expandedRunIds.value = new Set()
      await router.replace(`/variants/compare/${data.batch_id}`)
      await loadComparisons()
    }
  } catch (e: unknown) {
    if (thisId !== batchId.value) return
    error.value = `${t('views.variantBatch.refireFailed')} ${formatApiError(e)}`
  } finally {
    refiring.value = false
  }
}

watch(
  batchId,
  (id) => {
    if (id) loadBatch(id)
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  /* no timers to clear */
})

loadComparisons()
loadCurrency()
</script>
