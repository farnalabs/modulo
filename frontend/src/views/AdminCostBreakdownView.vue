<template>
  <div data-theme="agent" class="page-wide">
    <PageTabs :tabs="[
      { label: $t('views.AdminCostBreakdownView.tabs_overview'), to: '/admin/costs' },
      { label: $t('views.AdminCostBreakdownView.tabs_spend_limits'), to: '/admin/costs/limits' },
      { label: $t('views.AdminCostBreakdownView.tabs_cost_components'), to: '/admin/costs/components' },
      { label: $t('views.AdminCostBreakdownView.tabs_cost_controls'), to: '/admin/costs/controls' },
    ]" />
    <PageHeader :title="$t('views.AdminCostBreakdownView.cost_breakdown')" :subtitle="$t('views.AdminCostBreakdownView.monthly_cost_report_and_anomaly_detection_across_teams')" />

    <FeatureGate feature-name="admin_cost_breakdown" required-tier="team" show-disabled>
      <div class="space-y-6">
      <LoadingSpinner v-if="loading" />

      <ErrorAlert v-else-if="loadError" :message="loadError" :on-retry="loadData" />

      <template v-else>
        <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Card>
            <template #title><span class="text-sm font-medium text-muted-foreground">{{ $t('views.AdminCostBreakdownView.total_spend_this_month') }}</span></template>
            <template #content>
              <p class="text-2xl font-semibold tabular-nums" data-testid="cost-total-spend">{{ formatMoney(totalSpend, currencyCode) }}</p>
            </template>
          </Card>
          <Card>
            <template #title><span class="text-sm font-medium text-muted-foreground">{{ $t('views.AdminCostBreakdownView.avg_cost_per_run') }}</span></template>
            <template #content>
              <p class="text-2xl font-semibold tabular-nums" data-testid="cost-avg-per-run">{{ formatMoney(avgCostPerRun, currencyCode) }}</p>
            </template>
          </Card>
          <Card>
            <template #title><span class="text-sm font-medium text-muted-foreground">{{ $t('views.AdminCostBreakdownView.cost_per_successful_run') }}</span></template>
            <template #content>
              <p class="text-2xl font-semibold tabular-nums" data-testid="cost-per-successful-run">
                <template v-if="successfulRuns > 0">{{ formatMoney(costPerSuccessfulRun, currencyCode) }}</template>
                <template v-else>—</template>
              </p>
              <p class="mt-1 text-xs text-muted-foreground" data-testid="cost-successful-runs-count">
                {{ successfulRuns > 0 ? $t('views.AdminCostBreakdownView.successful_runs_count', { count: successfulRuns }) : $t('views.AdminCostBreakdownView.no_successful_runs') }}
              </p>
            </template>
          </Card>
          <Card>
            <template #title><span class="text-sm font-medium text-muted-foreground">{{ $t('views.AdminCostBreakdownView.total_runs') }}</span></template>
            <template #content>
              <p class="text-2xl font-semibold tabular-nums" data-testid="cost-total-runs">{{ totalRuns }}</p>
            </template>
          </Card>
        </div>

        <Card>
          <template #title>{{ $t('views.AdminCostBreakdownView.per_team_cost_breakdown') }}</template>
          <template #subtitle>{{ $t('views.AdminCostBreakdownView.monthly_spend_run_count_and_avg_by_team') }}</template>
          <template #content>
            <EmptyState
              v-if="items.length === 0"
              :title="$t('views.AdminCostBreakdownView.no_team_cost_data_available')"
              data-testid="cost-team-empty"
            />
            <div v-else class="overflow-x-auto" data-testid="cost-team-table">
              <DataTable
                :columns="[
                  { key: 'entity_name', label: $t('views.AdminCostBreakdownView.team') },
                  { key: 'total_spend_usd', label: $t('views.AdminCostBreakdownView.total_spend'), numeric: true },
                  { key: 'total_runs', label: $t('views.AdminCostBreakdownView.runs'), numeric: true },
                  { key: 'avg_per_run', label: $t('views.AdminCostBreakdownView.avg_per_run'), numeric: true },
                  { key: 'annotations', label: $t('views.AdminCostBreakdownView.annotations') },
                ]"
                :rows="tableRows"
              >
                <template #cell-total_spend_usd="{ value }">
                  {{ formatMoney(value as number, currencyCode) }}
                </template>
                <template #cell-avg_per_run="{ row }">
                  {{ formatMoney((row as TeamTableRow).total_runs > 0 ? (row as TeamTableRow).avg_per_run : 0, currencyCode) }}
                </template>
                <template #cell-annotations="{ row }">
                  <div class="text-xs">
                    <p v-if="(row as TeamTableRow).refused_total_usd != null" class="text-warning" data-testid="cost-annotation-refused">
                      {{ $t('views.AdminCostBreakdownView.refused_limit_line', { amount: (row as TeamTableRow).refused_total_usd!.toFixed(2) }) }}
                    </p>
                    <p v-if="(row as TeamTableRow).clamped_total_usd != null" class="text-muted-foreground" data-testid="cost-annotation-clamped">
                      {{ $t('views.AdminCostBreakdownView.day_ledger_clamped_line') }}
                    </p>
                    <span v-if="(row as TeamTableRow).refused_total_usd == null && (row as TeamTableRow).clamped_total_usd == null">—</span>
                  </div>
                </template>
              </DataTable>
            </div>
          </template>
        </Card>

        <Card>
          <template #title>{{ $t('views.AdminCostBreakdownView.cost_anomaly_alerts') }}</template>
          <template #subtitle>{{ $t('views.AdminCostBreakdownView.days_where_spend_exceeded_2x_avg') }}</template>
          <template #content>
            <LoadingSpinner v-if="anomaliesLoading" />
            <ErrorAlert
              v-else-if="anomaliesError"
              :message="anomaliesError"
              :on-retry="loadAnomalies"
              data-testid="cost-anomalies-error"
            />
            <EmptyState
              v-else-if="anomalies.length === 0"
              :title="$t('views.AdminCostBreakdownView.no_anomalies_detected')"
              data-testid="cost-anomalies-empty"
            />
            <div v-else class="space-y-3" data-testid="cost-anomalies-list">
              <div
                v-for="anomaly in activeAnomalies"
                :key="anomaly.id"
                class="flex items-center justify-between rounded-lg border border-warning/30 bg-warning/5 p-4"
                :data-testid="'cost-anomaly-' + anomaly.id"
              >
                <div>
                  <p class="text-sm font-medium">{{ anomaly.anomaly_date }}</p>
                  <p class="text-xs text-muted-foreground">
                    {{ $t('views.AdminCostBreakdownView.anomaly_spend_detail', { amount: formatMoney(anomaly.amount, currencyCode), baseline: formatMoney(anomaly.baseline, currencyCode), percent: (anomaly.percent_above > 0 ? '+' : '') + anomaly.percent_above.toFixed(0) + '%' }) }}
                  </p>
                </div>
                <Button size="small" severity="secondary" outlined :disabled="dismissLoading[anomaly.id]" :data-testid="'cost-anomaly-dismiss-' + anomaly.id" @click="dismissAnomaly(anomaly.id)">
                  {{ $t('views.AdminCostBreakdownView.dismiss') }}
                </Button>
              </div>
              <p v-if="dismissedAnomalies.length > 0" class="pt-2 text-xs text-muted-foreground">
                {{ $t('views.AdminCostBreakdownView.dismissed_anomalies_count', { count: dismissedAnomalies.length }) }}
              </p>
            </div>
          </template>
        </Card>
      </template>
      </div>
    </FeatureGate>
  </div>
</template>

<script setup lang="ts">
import PageHeader from '../components/shared/PageHeader.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import { ref, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import type { components } from '../lib/api/schema'
import { formatApiError } from '../lib/api/formatError'
import { useDataFetch } from '../composables/useDataFetch'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import Card from 'primevue/card'
import Button from 'primevue/button'
import { DataTable } from '../components/ui/data-table'
import PageTabs from "../components/PageTabs.vue"
import { formatMoney } from '../lib/money'
import { useOrgCurrency } from '../composables/useOrgCurrency'

const { t } = useI18n()
const planStore = usePlanStore()
const { currencyCode, loadCurrency } = useOrgCurrency()

type CostReportRow = components['schemas']['CostReportRow']
type CostReportResponse = components['schemas']['CostReportResponse']
type AnomalyResponse = components['schemas']['AnomalyResponse']
type TeamTableRow = CostReportRow & {
  avg_per_run: number
  refused_total_usd: number | null
  clamped_total_usd: number | null
}

const { loading, error: loadError, data, load: loadData } = useDataFetch(
  () => api.GET('/api/v1/admin/costs', {
    params: { query: { group_by: 'team', period: 'month' } },
  }),
  { immediate: false },
)

const items = computed<CostReportRow[]>(() => (data.value as CostReportResponse | undefined)?.items ?? [])

const tableRows = computed<TeamTableRow[]>(() => items.value.map(item => ({
  ...item,
  avg_per_run: item.total_runs > 0 ? item.total_spend_usd / item.total_runs : 0,
  refused_total_usd: item.annotations?.refused_total_usd ?? null,
  clamped_total_usd: item.annotations?.clamped_total_usd ?? null,
})))

const anomaliesLoading = ref(true)
const anomaliesError = ref<string | null>(null)
const anomalies = ref<AnomalyResponse[]>([])

const totalSpend = computed(() => {
  const ot = (data.value as CostReportResponse | undefined)?.org_total
  if (ot != null) return Number.parseFloat(ot)
  return items.value.reduce((sum, i) => sum + i.total_spend_usd, 0)
})
const totalRuns = computed(() => {
  const orc = (data.value as CostReportResponse | undefined)?.org_run_count
  if (orc != null) return orc
  return items.value.reduce((sum, i) => sum + i.total_runs, 0)
})
const avgCostPerRun = computed(() => totalRuns.value > 0 ? totalSpend.value / totalRuns.value : 0)

// Cost per successful run — derived from the existing analytics buckets endpoint.
// Each bucket carries `count` (total runs) and `success_rate` (complete / count).
// Summing count * success_rate across all day-buckets for the period gives the
// total successful-run count. No new backend endpoint needed.
type AnalyticsBucket = components['schemas']['AnalyticsBucket']
type AnalyticsResponse = components['schemas']['AnalyticsResponse']

const successfulRuns = ref(0)

async function loadSuccessCount() {
  try {
    const now = new Date()
    const dateFrom = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-01`
    const dateTo = now.toISOString().slice(0, 10)

    const { data: analyticsData, error: analyticsErr } = await api.GET('/api/v1/analytics/query', {
      params: { query: { group_by: 'day', date_from: dateFrom, date_to: dateTo } },
    })
    if (!analyticsErr && analyticsData) {
      const resp = analyticsData as AnalyticsResponse
      successfulRuns.value = (resp.buckets ?? []).reduce((sum: number, b: AnalyticsBucket) => {
        if (b.success_rate != null) {
          return sum + Math.round(b.count * b.success_rate)
        }
        return sum
      }, 0)
    }
  } catch {
    // Analytics endpoint may lack permission — degrade gracefully to 0.
    successfulRuns.value = 0
  }
}

const costPerSuccessfulRun = computed(() => successfulRuns.value > 0 ? totalSpend.value / successfulRuns.value : 0)

const activeAnomalies = computed(() => anomalies.value.filter((a) => !a.dismissed))
const dismissedAnomalies = computed(() => anomalies.value.filter((a) => a.dismissed))

async function loadAnomalies() {
  anomaliesLoading.value = true
  anomaliesError.value = null
  try {
    const { data, error: err } = await api.GET('/api/v1/admin/costs/anomalies')
    if (err) {
      anomaliesError.value = `${t('views.AdminCostBreakdownView.failed_to_load_anomalies')}: ${formatApiError(err)}`
    } else if (data) {
      anomalies.value = data
    }
  } catch (e: unknown) {
    anomaliesError.value = `${t('views.AdminCostBreakdownView.failed_to_load_anomalies')}: ${formatApiError(e)}`
  } finally {
    anomaliesLoading.value = false
  }
}

const dismissLoading = ref<Record<string, boolean>>({})

async function dismissAnomaly(id: string) {
  dismissLoading.value[id] = true
  try {
    await api.POST('/api/v1/admin/costs/anomalies/dismiss/{anomaly_id}', {
      params: { path: { anomaly_id: id } },
    })
    await loadAnomalies()
  } catch (e) {
    anomaliesError.value = `${t('views.AdminCostBreakdownView.failed_to_dismiss_anomaly')}: ${formatApiError(e)}`
  } finally {
    dismissLoading.value[id] = false
  }
}

onMounted(() => {
  planStore.fetchPlan()
  loadData()
  loadSuccessCount()
  loadAnomalies()
  loadCurrency()
})
</script>
