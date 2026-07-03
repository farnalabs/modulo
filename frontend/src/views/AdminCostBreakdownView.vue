<template>
  <PageTabs :tabs="[
    { label: 'Overview', to: '/admin/costs' },
    { label: 'Spend Limits', to: '/admin/costs/limits' },
    { label: 'Cost Controls', to: '/admin/costs/controls' },
  ]" />
  <div data-theme="agent" class="mx-auto max-w-6xl space-y-6 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">{{ $t('views.AdminCostBreakdownView.cost_breakdown') }}</h1>
      <p class="mt-1 text-muted-foreground">{{ $t('views.AdminCostBreakdownView.monthly_cost_report_and_anomaly_detection_across_teams') }}</p>
    </header>

    <FeatureGate feature-name="admin_cost_breakdown" required-tier="team" show-disabled>

      <LoadingSpinner v-if="loading" />

      <ErrorAlert v-else-if="loadError" :message="loadError" :on-retry="loadData" />

      <template v-else>
        <div class="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <Card>
            <CardHeader>
              <CardTitle class="text-sm font-medium text-muted-foreground">Total Spend (This Month)</CardTitle>
            </CardHeader>
            <CardContent>
              <p class="text-3xl font-bold" data-testid="cost-total-spend">${{ totalSpend.toFixed(2) }}</p>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle class="text-sm font-medium text-muted-foreground">Avg Cost per Run</CardTitle>
            </CardHeader>
            <CardContent>
              <p class="text-3xl font-bold" data-testid="cost-avg-per-run">${{ avgCostPerRun.toFixed(2) }}</p>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle class="text-sm font-medium text-muted-foreground">Total Runs</CardTitle>
            </CardHeader>
            <CardContent>
              <p class="text-3xl font-bold" data-testid="cost-total-runs">{{ totalRuns }}</p>
            </CardContent>
          </Card>
        </div>

        <Card>
          <CardHeader>
            <CardTitle>Per-Team Cost Breakdown</CardTitle>
            <CardDescription>Monthly spend, run count, and average cost per run by team</CardDescription>
          </CardHeader>
          <CardContent>
            <div v-if="items.length === 0" class="py-4 text-center text-sm text-muted-foreground">
              No team cost data available.
            </div>
            <div v-else class="overflow-x-auto">
              <table class="w-full text-sm">
                <thead>
                  <tr class="border-b text-left text-muted-foreground">
                    <th class="pb-3 pr-4 font-medium">Team</th>
                    <th class="pb-3 pr-4 font-medium">Total Spend</th>
                    <th class="pb-3 pr-4 font-medium">Runs</th>
                    <th class="pb-3 font-medium">Avg per Run</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="team in items" :key="team.entity_id" class="border-b last:border-b-0" :data-testid="'cost-team-row-' + team.entity_id">
                    <td class="py-3 pr-4 font-medium">{{ team.entity_name }}</td>
                    <td class="py-3 pr-4">${{ team.total_spend_usd.toFixed(2) }}</td>
                    <td class="py-3 pr-4">{{ team.total_runs }}</td>
                    <td class="py-3">${{ team.total_runs > 0 ? (team.total_spend_usd / team.total_runs).toFixed(2) : '0.00' }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Cost Anomaly Alerts</CardTitle>
            <CardDescription>Days where spend exceeded 2x the rolling 7-day average</CardDescription>
          </CardHeader>
          <CardContent>
            <LoadingSpinner v-if="anomaliesLoading" />
            <div v-else-if="anomaliesError" class="text-sm text-destructive">{{ anomaliesError }}</div>
            <div v-else-if="anomalies.length === 0" class="py-4 text-center text-sm text-muted-foreground">
              No anomalies detected. Spend patterns are within expected ranges.
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
                  {{ dismissLoading[anomaly.id] ? '...' : 'Dismiss' }}
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
import { ref, computed, onMounted } from 'vue'
import { api } from '../lib/api/client'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from '../components/ui/card'
import { Button } from '../components/ui/button'
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

const loading = ref(true)
const loadError = ref<string | null>(null)
const items = ref<CostReportRow[]>([])

const anomaliesLoading = ref(true)
const anomaliesError = ref<string | null>(null)
const anomalies = ref<AnomalyResponse[]>([])

const totalSpend = computed(() => items.value.reduce((sum, i) => sum + i.total_spend_usd, 0))
const totalRuns = computed(() => items.value.reduce((sum, i) => sum + i.total_runs, 0))
const avgCostPerRun = computed(() => totalRuns.value > 0 ? totalSpend.value / totalRuns.value : 0)

const activeAnomalies = computed(() => anomalies.value.filter((a) => !a.dismissed))
const dismissedAnomalies = computed(() => anomalies.value.filter((a) => a.dismissed))

async function loadData() {
  loading.value = true
  loadError.value = null
  try {
    const { data, error: err } = await (api as any).GET('/api/v1/admin/costs', {
      params: { query: { group_by: 'team', period: 'month' } },
    })
    if (err) {
      loadError.value = `Failed to load cost report: ${err}`
    } else if (data) {
      items.value = (data as CostReportResponse).items ?? []
    }
  } catch (e: unknown) {
    loadError.value = `Failed to load cost report: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
}

async function loadAnomalies() {
  anomaliesLoading.value = true
  anomaliesError.value = null
  try {
    const { data, error: err } = await (api as any).GET('/api/v1/admin/costs/anomalies')
    if (err) {
      anomaliesError.value = `Failed to load anomalies: ${err}`
    } else if (data) {
      anomalies.value = (data as AnomalyResponse[]) ?? []
    }
  } catch (e: unknown) {
    anomaliesError.value = `Failed to load anomalies: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    anomaliesLoading.value = false
  }
}

const dismissLoading = ref<Record<string, boolean>>({})

async function dismissAnomaly(id: string) {
  dismissLoading.value[id] = true
  try {
    await (api as any).GET(`/api/v1/admin/costs/anomalies/dismiss/${id}`)
    await loadAnomalies()
  } catch {
    anomaliesError.value = 'Failed to dismiss anomaly'
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
