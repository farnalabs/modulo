<template>
  <PageTabs :tabs="[
    { label: 'Overview', to: '/admin/costs' },
    { label: 'Spend Limits', to: '/admin/costs/limits' },
    { label: 'Cost Components', to: '/admin/costs/components' },
    { label: 'Cost Controls', to: '/admin/costs/controls' },
  ]" />
  <div data-theme="agent" class="page-wide">
    <PageHeader :title="$t('views.AdminCostBreakdownView.spend_limits')" :subtitle="$t('views.AdminSpendLimitsView.configure_daily_spend_limits_at_the_org_and_team_level')" />

    <FeatureGate feature-name="admin_spend_limits" required-tier="team" show-disabled>

      <LoadingSpinner v-if="loading" />

      <ErrorAlert v-else-if="loadError" :message="loadError" :on-retry="loadData" />

      <template v-else>
        <Card>
          <CardHeader>
            <CardTitle>{{ $t('views.AdminSpendLimitsView.org_level_daily_spend_limit') }}</CardTitle>
            <CardDescription>{{ $t('views.AdminSpendLimitsView.maximum_daily_spend_across_all_teams_in_usd') }}</CardDescription>
          </CardHeader>
          <CardContent>
            <div class="flex items-end gap-3">
              <div class="flex-1">
                <span class="mb-1.5 block text-xs font-medium text-muted-foreground">{{ $t('views.AdminSpendLimitsView.daily_limit_usd') }}</span>
                <Input aria-label="Form control" :model-value="orgLimit ?? undefined" @update:model-value="(v: any) => orgLimit = v === '' ? null : Number(v)" type="number" min="0" step="0.01" placeholder="No limit" data-testid="admin-spend-limits-org-limit" />
              </div>
              <Button :disabled="savingOrg" data-testid="admin-spend-limits-org-save" @click="saveOrgLimit">
                {{ savingOrg ? 'Saving...' : 'Save' }}
              </Button>
            </div>
            <p v-if="orgSaveError" class="mt-2 text-xs text-destructive">{{ orgSaveError }}</p>
            <p v-if="orgSaveSuccess" class="mt-2 text-xs text-success">{{ $t('views.AdminSpendLimitsView.org_limit_updated') }}</p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>{{ $t('views.AdminSpendLimitsView.per_team_spend_limits') }}</CardTitle>
            <CardDescription>{{ $t('views.AdminSpendLimitsView.override_the_org_level_limit_for_specific_teams') }}</CardDescription>
          </CardHeader>
          <CardContent>
            <div v-if="teams.length === 0" class="py-4 text-center text-sm text-muted-foreground">
              No teams found.
            </div>
            <table v-else class="w-full text-sm">
              <thead>
                <tr>
                  <th class="table-header">{{ $t('views.AdminSpendLimitsView.team') }}</th>
                  <th class="table-header">{{ $t('views.AdminSpendLimitsView.daily_limit_usd_caps') }}</th>
                  <th class="table-header" />
                </tr>
              </thead>
              <tbody>
                <tr v-for="team in teams" :key="team.id" class="border-b last:border-b-0">
                  <td class="table-cell font-medium">{{ team.name }}</td>
                  <td class="table-cell">
                    <Input aria-label="Form control"
                      :model-value="team.editingLimit ?? undefined" @update:model-value="(v: any) => team.editingLimit = v === '' ? null : Number(v)"
                      type="number"
                      min="0"
                      step="0.01"
                      placeholder="Inherit org limit"
                      class="max-w-40"
                      :data-testid="'admin-spend-limits-team-limit-' + team.id"
                    />
                    <p v-if="team.saveError" class="mt-1 text-xs text-destructive">{{ team.saveError }}</p>
                  </td>
                  <td class="table-cell-numeric">
                    <Button size="sm" :disabled="team.saving" :data-testid="'admin-spend-limits-team-save-' + team.id" @click="saveTeamLimit(team)">
                      {{ team.saving ? 'Saving...' : 'Save' }}
                    </Button>
                  </td>
                </tr>
              </tbody>
            </table>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>{{ $t('views.AdminSpendLimitsView.current_spend') }}</CardTitle>
            <CardDescription>{{ $t('views.AdminSpendLimitsView.todays_accrued_costs_across_all_teams') }}</CardDescription>
          </CardHeader>
          <CardContent>
            <LoadingSpinner v-if="costsLoading" />
            <div v-else-if="costsError" class="text-sm text-destructive">{{ costsError }}</div>
            <div v-else class="space-y-4">
              <div class="flex items-center justify-between rounded-lg border bg-muted p-4">
                <span class="text-sm font-medium">{{ $t('views.AdminSpendLimitsView.org_total') }}</span>
                <span class="text-lg font-semibold" :class="overageClass(orgTotalCost, orgLimit)">
                  {{ formatMoney(orgTotalCost, currencyCode) }}
                </span>
              </div>
              <div v-if="teamCosts.length > 0" class="space-y-2">
                <div
                  v-for="tc in teamCosts"
                  :key="tc.team_id"
                  class="flex items-center justify-between rounded-lg border p-3"
                >
                  <span class="text-sm">{{ tc.team_name }}</span>
                  <span class="text-sm font-medium" :class="overageClass(tc.cost_usd, tc.limit_usd)">
                    {{ formatMoney(tc.cost_usd, currencyCode) }}
                  </span>
                </div>
              </div>
              <p v-else class="text-sm text-muted-foreground">{{ $t('views.AdminSpendLimitsView.no_team_cost_data_available') }}</p>
            </div>
          </CardContent>
        </Card>
      </template>
    </FeatureGate>
  </div>
</template>

<script setup lang="ts">
import PageHeader from '../components/shared/PageHeader.vue'
import { ref, computed, watch } from 'vue'
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import { formatApiError } from '../lib/api/formatError'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from '../components/ui/card'
import { Input } from '../components/ui/input'
import { Button } from '../components/ui/button'
import PageTabs from "../components/PageTabs.vue"
import { formatMoney } from '../lib/money'
import { useOrgCurrency } from '../composables/useOrgCurrency'

const planStore = usePlanStore()
const { currencyCode, loadCurrency } = useOrgCurrency()

interface SpendLimitData {
  org_daily_limit_usd: number | null
  teams: Array<{
    id: string
    name: string
    daily_limit_usd: number | null
  }>
}

interface CostReportData {
  period: string
  group_by: string
  items: Array<{
    entity_id: string
    entity_name: string
    total_spend_usd: number
    total_runs: number
    components: Array<{ name: string; amount_usd: string }>
  }>
  org_unassigned_components?: string | null
  legacy_total?: string | null
  org_total?: string | null
  has_more?: boolean
}

interface TeamCostItem {
  team_id: string
  team_name: string
  cost_usd: number
  limit_usd: number | null
}

function parseDecimalString(value: string | null | undefined): number | null {
  if (value == null) return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

interface TeamRow {
  id: string
  name: string
  daily_limit_usd: number | null
  editingLimit: number | null
  saving: boolean
  saveError: string | null
}

const { data: limitsData, loading, error: loadError, load: loadData } = useDataFetch(
  () => (api as any).GET('/api/v1/admin/costs/limits'),
)

const orgLimit = ref<number | null>(null)
const teams = ref<TeamRow[]>([])

watch(() => limitsData.value, (data) => {
  if (data) {
    const resp = data as SpendLimitData
    orgLimit.value = resp.org_daily_limit_usd
    teams.value = (resp.teams ?? []).map(t => ({
      ...t,
      editingLimit: t.daily_limit_usd,
      saving: false,
      saveError: null,
    }))
  }
})

const { data: costsResp, loading: costsLoading, error: costsError } = useDataFetch(
  () => (api as any).GET('/api/v1/admin/costs'),
  { initialValue: { period: 'month', group_by: 'team', items: [], org_total: '0', legacy_total: '0', org_unassigned_components: '0' } }
)

const orgTotalCost = computed(() => {
  const resp = costsResp.value as CostReportData | null
  return parseDecimalString(resp?.org_total) ?? 0
})
const teamCosts = computed<TeamCostItem[]>(() => {
  const resp = costsResp.value as CostReportData | null
  return (resp?.items ?? []).map((item) => ({
    team_id: item.entity_id,
    team_name: item.entity_name,
    cost_usd: item.total_spend_usd,
    limit_usd: null,
  }))
})

const savingOrg = ref(false)
const orgSaveError = ref<string | null>(null)
const orgSaveSuccess = ref(false)

function overageClass(cost: number, limit: number | null): string {
  if (limit === null || limit <= 0) return ''
  return cost > limit ? 'text-destructive' : 'text-success'
}

async function saveOrgLimit() {
  savingOrg.value = true
  orgSaveError.value = null
  orgSaveSuccess.value = false
  try {
    const { error: err } = await (api as any).PUT('/api/v1/admin/costs/limits/org', {
      body: { daily_limit_usd: orgLimit.value },
    })
    if (err) {
      orgSaveError.value = `Failed to save: ${formatApiError(err)}`
    } else {
      orgSaveSuccess.value = true
    }
  } catch (e: unknown) {
    orgSaveError.value = `Failed to save: ${formatApiError(e)}`
  } finally {
    savingOrg.value = false
  }
}

async function saveTeamLimit(team: TeamRow) {
  team.saving = true
  team.saveError = null
  try {
    const { error: err } = await (api as any).PUT(`/api/v1/admin/costs/limits/teams/${team.id}`, {
      body: { daily_limit_usd: team.editingLimit },
    })
    if (err) {
      team.saveError = `Failed to save: ${formatApiError(err)}`
    } else {
      team.daily_limit_usd = team.editingLimit
    }
  } catch (e: unknown) {
    team.saveError = `Failed to save: ${formatApiError(e)}`
  } finally {
    team.saving = false
  }
}

planStore.fetchPlan()
loadCurrency()
</script>
