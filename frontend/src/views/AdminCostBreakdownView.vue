<template>
  <PageTabs :tabs="[
    { label: 'Overview', to: '/admin/costs' },
    { label: 'Spend Limits', to: '/admin/costs/limits' },
    { label: 'Cost Controls', to: '/admin/costs/controls' },
  ]" />
  <div data-theme="agent" class="page-wide">
    <PageHeader :title="$t('views.AdminCostBreakdownView.cost_breakdown')" :subtitle="$t('views.AdminCostBreakdownView.monthly_cost_report_and_anomaly_detection_across_teams')" />

    <FeatureGate feature-name="admin_cost_breakdown" required-tier="team" show-disabled>

      <LoadingSpinner v-if="loading" />

      <ErrorAlert v-else-if="loadError" :message="loadError" :on-retry="loadData" />

      <template v-else>
        <div class="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <Card>
            <CardHeader>
              <CardTitle class="text-sm font-medium text-muted-foreground">{{ $t('views.AdminCostBreakdownView.total_spend_this_month') }}</CardTitle>
            </CardHeader>
            <CardContent>
              <p class="text-2xl font-semibold tabular-nums" data-testid="cost-total-spend">${{ totalSpend.toFixed(2) }}</p>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle class="text-sm font-medium text-muted-foreground">{{ $t('views.AdminCostBreakdownView.avg_cost_per_run') }}</CardTitle>
            </CardHeader>
            <CardContent>
              <p class="text-2xl font-semibold tabular-nums" data-testid="cost-avg-per-run">${{ avgCostPerRun.toFixed(2) }}</p>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle class="text-sm font-medium text-muted-foreground">{{ $t('views.AdminCostBreakdownView.total_runs') }}</CardTitle>
            </CardHeader>
            <CardContent>
              <p class="text-2xl font-semibold tabular-nums" data-testid="cost-total-runs">{{ totalRuns }}</p>
            </CardContent>
          </Card>
        </div>

        <Card>
          <CardHeader>
            <CardTitle>{{ $t('views.AdminCostBreakdownView.per_team_cost_breakdown') }}</CardTitle>
            <CardDescription>{{ $t('views.AdminCostBreakdownView.monthly_spend_run_count_and_avg_by_team') }}</CardDescription>
          </CardHeader>
          <CardContent>
            <div v-if="items.length === 0" class="py-4 text-center text-sm text-muted-foreground">
              {{ $t('views.AdminCostBreakdownView.no_team_cost_data_available') }}
            </div>
            <div v-else class="overflow-x-auto">
              <DataTable
                :columns="[
                  { key: 'entity_name', label: $t('views.AdminCostBreakdownView.team') },
                  { key: 'total_spend_usd', label: $t('views.AdminCostBreakdownView.total_spend'), numeric: true },
                  { key: 'total_runs', label: $t('views.AdminCostBreakdownView.runs'), numeric: true },
                  { key: 'avg_per_run', label: $t('views.AdminCostBreakdownView.avg_per_run'), numeric: true },
                ]"
                :rows="tableRows"
              >
                <template #cell-total_spend_usd="{ value }">
                  ${{ (value as number).toFixed(2) }}
                </template>
                <template #cell-avg_per_run="{ row }">
                  ${{ (row as any).total_runs > 0 ? ((row as any).total_spend_usd / (row as any).total_runs).toFixed(2) : '0.00' }}
                </template>
              </DataTable>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>{{ $t('views.AdminCostBreakdownView.cost_anomaly_alerts') }}</CardTitle>
            <CardDescription>{{ $t('views.AdminCostBreakdownView.days_where_spend_exceeded_2x_avg') }}</CardDescription>
          </CardHeader>
          <CardContent>
            <LoadingSpinner v-if="anomaliesLoading" />
            <div v-else-if="anomaliesError" class="text-sm text-destructive">{{ anomaliesError }}</div>
            <div v-else-if="anomalies.length === 0" class="py-4 text-center text-sm text-muted-foreground">
              {{ $t('views.AdminCostBreakdownView.no_anomalies_detected') }}
            </div>
            <div v-else class="space-y-3">
              <div
                v-for="anomaly in activeAnomalies"
                :key="anomaly.id"
                class="flex items-center justify-between rounded-lg border border-warning/30 bg-warning/5 p-4"
                :data-testid="'cost-anomaly-' + anomaly.id"
              >
                <div>
                  <p class="text-sm font-medium">{{ anomaly.anomaly_date }}</p>
                  <p class="text-xs text-muted-foreground">
                    Spend: <strong>${{ anomaly.amount.toFixed(2) }}</strong>
                    (baseline: ${{ anomaly.baseline.toFixed(2) }},
                    {{ anomaly.percent_above > 0 ? '+' : '' }}{{ anomaly.percent_above.toFixed(0) }}% above)
                  </p>
                </div>
                <Button size="sm" variant="outline" :disabled="dismissLoading[anomaly.id]" :data-testid="'cost-anomaly-dismiss-' + anomaly.id" @click="dismissAnomaly(anomaly.id)">
                  {{ dismissLoading[anomaly.id] ? '...' : $t('views.AdminCostBreakdownView.dismiss') }}
                </Button>
              </div>
              <p v-if="dismissedAnomalies.length > 0" class="pt-2 text-xs text-muted-foreground">
                {{ dismissedAnomalies.length }} dismissed anomaly{{ dismissedAnomalies.length === 1 ? '' : 'ies' }}
              </p>
            </div>
          </CardContent>
        </Card>
      </template>
    </FeatureGate>
  </div>
</template>

<script setup lang="ts">
import PageHeader from '../components/shared/PageHeader.vue'
import { ref, computed, onMounted } from 'vue'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import { useDataFetch } from '../composables/useDataFetch'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from '../components/ui/card'
import { Button } from '../components/ui/button'
import { DataTable } from '../components/ui/data-table'
import PageTabs from "../components/PageTabs.vue"

const planStore = usePlanStore()

interface CostReportRow {
  entity_id: string
  entity_name: string
  total_spend_usd: number
  total_runs: number
}

interface CostReportResponse {
  period: string
  group_by: string
  items: CostReportRow[]
}

interface AnomalyResponse {
  id: string
  anomaly_date: string
  pipeline_id: string | null
  amount: number
  baseline: number
  percent_above: number
  dismissed: boolean
}

const { loading, error: loadError, data, load: loadData } = useDataFetch(
  () => (api as any).GET('/api/v1/admin/costs', {
    params: { query: { group_by: 'team', period: 'month' } },
  }),
  { immediate: false },
)

const items = computed(() => (data.value as CostReportResponse)?.items ?? [])

const tableRows = computed(() => items.value.map(item => ({
  ...item,
  avg_per_run: item.total_runs > 0 ? item.total_spend_usd / item.total_runs : 0,
})))

const anomaliesLoading = ref(true)
const anomaliesError = ref<string | null>(null)
const anomalies = ref<AnomalyResponse[]>([])

const totalSpend = computed(() => items.value.reduce((sum, i) => sum + i.total_spend_usd, 0))
const totalRuns = computed(() => items.value.reduce((sum, i) => sum + i.total_runs, 0))
const avgCostPerRun = computed(() => totalRuns.value > 0 ? totalSpend.value / totalRuns.value : 0)

const activeAnomalies = computed(() => anomalies.value.filter((a) => !a.dismissed))
const dismissedAnomalies = computed(() => anomalies.value.filter((a) => a.dismissed))

async function loadAnomalies() {
  anomaliesLoading.value = true
  anomaliesError.value = null
  try {
    const { data, error: err } = await (api as any).GET('/api/v1/admin/costs/anomalies')
    if (err) {
      anomaliesError.value = `Failed to load anomalies: ${formatApiError(err)}`
    } else if (data) {
      anomalies.value = (data as AnomalyResponse[]) ?? []
    }
  } catch (e: unknown) {
    anomaliesError.value = `Failed to load anomalies: ${formatApiError(e)}`
  } finally {
    anomaliesLoading.value = false
  }
}

const dismissLoading = ref<Record<string, boolean>>({})

async function dismissAnomaly(id: string) {
  dismissLoading.value[id] = true
  try {
    await (api as any).POST(`/api/v1/admin/costs/anomalies/dismiss/${id}`)
    await loadAnomalies()
  } catch (e) {
    anomaliesError.value = `Failed to dismiss anomaly: ${formatApiError(e)}`
  } finally {
    dismissLoading.value[id] = false
  }
}

onMounted(() => {
  planStore.fetchPlan()
  loadData()
  loadAnomalies()
})
</script>
