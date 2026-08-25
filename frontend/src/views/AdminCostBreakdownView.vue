<template>
  <PageTabs :tabs="[
    { label: 'Overview', to: '/admin/costs' },
    { label: 'Spend Limits', to: '/admin/costs/limits' },
    { label: 'Cost Components', to: '/admin/costs/components' },
    { label: 'Cost Controls', to: '/admin/costs/controls' },
  ]" />
  <div data-theme="agent" class="page-wide">
    <PageHeader :title="$t('views.AdminCostBreakdownView.cost_breakdown')" :subtitle="$t('views.AdminCostBreakdownView.monthly_cost_report_and_anomaly_detection_across_teams')" />

    <FeatureGate feature-name="admin_cost_breakdown" required-tier="team" show-disabled>
      <div class="space-y-6">
      <LoadingSpinner v-if="loading" />

      <ErrorAlert v-else-if="loadError" :message="loadError" :on-retry="loadData" />

      <template v-else>
        <div class="grid grid-cols-1 gap-4 sm:grid-cols-3">
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
                  { key: 'annotations', label: $t('views.AdminCostBreakdownView.annotations') },
                ]"
                :rows="tableRows"
              >
                <template #cell-total_spend_usd="{ value }">
                  {{ formatMoney(value as number, currencyCode) }}
                </template>
                <template #cell-avg_per_run="{ row }">
                  {{ formatMoney((row as any).total_runs > 0 ? (row as any).total_spend_usd / (row as any).total_runs : 0, currencyCode) }}
                </template>
                <template #cell-annotations="{ row }">
                  <div class="text-xs">
                    <p v-if="(row as any).refused_total_usd != null" class="text-warning" data-testid="cost-annotation-refused">
                      {{ $t('views.AdminCostBreakdownView.refused_limit_line', { amount: (row as any).refused_total_usd.toFixed(2) }) }}
                    </p>
                    <p v-if="(row as any).clamped_total_usd != null" class="text-muted-foreground" data-testid="cost-annotation-clamped">
                      {{ $t('views.AdminCostBreakdownView.day_ledger_clamped_line') }}
                    </p>
                    <span v-if="(row as any).refused_total_usd == null && (row as any).clamped_total_usd == null">—</span>
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
                    Spend: <strong>{{ formatMoney(anomaly.amount, currencyCode) }}</strong>
                    (baseline: {{ formatMoney(anomaly.baseline, currencyCode) }},
                    {{ anomaly.percent_above > 0 ? '+' : '' }}{{ anomaly.percent_above.toFixed(0) }}% above)
                  </p>
                </div>
                <Button size="small" severity="secondary" outlined :disabled="dismissLoading[anomaly.id]" :data-testid="'cost-anomaly-dismiss-' + anomaly.id" @click="dismissAnomaly(anomaly.id)">
                  {{ dismissLoading[anomaly.id] ? '...' : $t('views.AdminCostBreakdownView.dismiss') }}
                </Button>
              </div>
              <p v-if="dismissedAnomalies.length > 0" class="pt-2 text-xs text-muted-foreground">
                {{ dismissedAnomalies.length }} dismissed anomaly{{ dismissedAnomalies.length === 1 ? '' : 'ies' }}
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
import { ref, computed, onMounted } from 'vue'
import { api } from '../lib/api/client'
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

const planStore = usePlanStore()
const { currencyCode, loadCurrency } = useOrgCurrency()

interface CostReportRow {
  entity_id: string
  entity_name: string
  total_spend_usd: number
  total_runs: number
  components?: Array<{ name: string; amount_usd: string }>
  annotations?: { refused_total_usd?: number | null; clamped_total_usd?: number | null } | null
}

interface CostReportResponse {
  period: string
  group_by: string
  items: CostReportRow[]
  org_unassigned_components?: string | null
  legacy_total?: string | null
  org_total?: string | null
  org_run_count?: number | null
  has_more?: boolean
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
  refused_total_usd: item.annotations?.refused_total_usd ?? null,
  clamped_total_usd: item.annotations?.clamped_total_usd ?? null,
})))

const anomaliesLoading = ref(true)
const anomaliesError = ref<string | null>(null)
const anomalies = ref<AnomalyResponse[]>([])

const totalSpend = computed(() => {
  const ot = (data.value as CostReportResponse)?.org_total
  if (ot != null) return Number.parseFloat(ot)
  return items.value.reduce((sum, i) => sum + i.total_spend_usd, 0)
})
const totalRuns = computed(() => {
  const orc = (data.value as CostReportResponse)?.org_run_count
  if (orc != null) return orc
  return items.value.reduce((sum, i) => sum + i.total_runs, 0)
})
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
  loadCurrency()
})
</script>
