<template>
  <div data-theme="agent" class="page-wide">
    <PageHeader
      :title="$t('views.AdminCostBreakdownView.spend_limits')"
      :subtitle="$t('views.AdminSpendLimitsView.configure_daily_spend_limits_at_the_org_and_team_level')"
       :tabs="costPageTabs"
    />

    <FeatureGate feature-name="admin_spend_limits" required-tier="team" show-disabled class="space-y-6">
      <!-- STATE-3 (ux-conformance): teams/form layout is known before data
           arrives, so the loading placeholder is a determinate pulse skeleton
           instead of an indeterminate spinner. -->
      <div v-if="loading" class="space-y-6" aria-busy="true" data-testid="admin-spend-limits-loading">
        <span class="sr-only" role="status">{{ $t('common.loading') }}</span>
        <div v-for="n in 3" :key="n" class="card space-y-3 p-6">
          <SkeletonBlock height-class="h-5 w-48" />
          <SkeletonBlock height-class="h-4 w-72" />
          <SkeletonBlock height-class="h-10 w-full" />
        </div>
      </div>

      <ErrorAlert v-else-if="loadError" :message="loadError" :on-retry="loadData" />

      <template v-else>
         <DataCard
           heading-level="h2"
           :title="$t('views.AdminSpendLimitsView.org_level_daily_spend_limit')"
          :description="$t('views.AdminSpendLimitsView.maximum_daily_spend_across_all_teams_in_usd')"
        >
          <template #default>
            <div class="flex items-end gap-3">
              <div class="flex-1">
                <label for="admin-spend-limits-org-limit" class="mb-1.5 block text-xs font-medium text-muted-foreground">{{ $t('views.AdminSpendLimitsView.daily_limit_usd') }}</label>
                <InputText id="admin-spend-limits-org-limit" :model-value="orgLimit == null ? '' : String(orgLimit)" @update:model-value="onUpdateOrgLimit" type="number" min="0" step="0.01" :placeholder="$t('views.AdminSpendLimitsView.no_limit')" data-testid="admin-spend-limits-org-limit" />
              </div>
              <Button :disabled="savingOrg" data-testid="admin-spend-limits-org-save" @click="saveOrgLimit">
                {{ savingOrg ? $t('views.AdminSpendLimitsView.saving') : $t('views.AdminSpendLimitsView.save') }}
              </Button>
            </div>
            <p v-if="orgSaveError" class="mt-2 text-xs text-destructive">{{ orgSaveError }}</p>
            <p v-if="orgSaveSuccess" class="mt-2 text-xs text-success-text">{{ $t('views.AdminSpendLimitsView.org_limit_updated') }}</p>
           </template>
         </DataCard>

        <FeatureGate feature-name="admin_cost_controls" required-tier="team" show-disabled>
           <DataCard
             heading-level="h2"
             :title="$t('views.AdminSpendLimitsView.hard_spend_ceilings')"
            :description="$t('views.AdminSpendLimitsView.hard_spend_ceilings_subtitle')"
          >
            <template #default>
              <div v-if="ceilingLoading" class="space-y-3" aria-busy="true" data-testid="admin-spend-limits-ceiling-loading">
                <SkeletonBlock height-class="h-10 w-full" />
                <SkeletonBlock height-class="h-10 w-full" />
                <SkeletonBlock height-class="h-16 w-full" />
              </div>
              <ErrorAlert v-else-if="ceilingLoadError" :message="ceilingLoadError" :on-retry="loadCeiling" />

              <template v-else>
                <div class="space-y-4">
                  <div class="flex items-end gap-3">
                    <div class="flex-1">
                      <label for="admin-spend-limits-max-run-cost" class="mb-1.5 block text-xs font-medium text-muted-foreground">{{ $t('views.AdminSpendLimitsView.per_run_ceiling_usd') }}</label>
                      <InputText
                        id="admin-spend-limits-max-run-cost"
                        :model-value="ceilingMaxRun == null ? '' : String(ceilingMaxRun)"
                        @update:model-value="v => ceilingMaxRun = parseNumericInput(v)"
                        type="number"
                        min="0"
                        step="0.01"
                        :placeholder="$t('views.AdminSpendLimitsView.no_limit')"
                        data-testid="admin-spend-limits-max-run-cost"
                      />
                      <p class="mt-1 text-xs text-muted-foreground">{{ $t('views.AdminSpendLimitsView.per_run_ceiling_help') }}</p>
                    </div>
                  </div>

                  <div class="flex items-end gap-3">
                    <div class="flex-1">
                      <label for="admin-spend-limits-spend-ceiling" class="mb-1.5 block text-xs font-medium text-muted-foreground">{{ $t('views.AdminSpendLimitsView.org_lifetime_ceiling_usd') }}</label>
                      <InputText
                        id="admin-spend-limits-spend-ceiling"
                        :model-value="ceilingSpend == null ? '' : String(ceilingSpend)"
                        @update:model-value="v => ceilingSpend = parseNumericInput(v)"
                        type="number"
                        min="0"
                        step="0.01"
                        :placeholder="$t('views.AdminSpendLimitsView.no_limit')"
                        data-testid="admin-spend-limits-spend-ceiling"
                      />
                      <p class="mt-1 text-xs text-muted-foreground">{{ $t('views.AdminSpendLimitsView.org_lifetime_ceiling_help') }}</p>
                    </div>
                  </div>

                  <div class="flex items-center justify-between rounded-lg border bg-muted p-4">
                    <span class="text-sm font-medium">{{ $t('views.AdminSpendLimitsView.remaining_budget') }}</span>
                    <span class="text-lg font-semibold" :class="ceilingRemaining === null ? '' : (ceilingRemaining === 0 ? 'text-destructive' : 'text-success-text')">
                      {{ ceilingRemaining === null ? $t('views.AdminSpendLimitsView.no_value') : formatMoney(ceilingRemaining, currencyCode) }}
                    </span>
                  </div>

                  <div class="flex items-center gap-3">
                    <Button :disabled="savingCeiling" data-testid="admin-spend-limits-ceiling-save" @click="saveCeiling">
                      {{ savingCeiling ? $t('views.AdminSpendLimitsView.saving') : $t('views.AdminSpendLimitsView.save') }}
                    </Button>
                    <p v-if="ceilingSaveError" class="text-xs text-destructive">{{ ceilingSaveError }}</p>
                    <p v-if="ceilingSaveSuccess" class="text-xs text-success-text">{{ $t('views.AdminSpendLimitsView.ceiling_updated') }}</p>
                  </div>
                </div>
              </template>
            </template>
          </DataCard>
        </FeatureGate>

         <DataCard
           heading-level="h2"
           :title="$t('views.AdminSpendLimitsView.per_team_spend_limits')"
          :description="$t('views.AdminSpendLimitsView.override_the_org_level_limit_for_specific_teams')"
        >
          <template #default>
            <EmptyState
              v-if="teams.length === 0"
              :title="$t('views.AdminSpendLimitsView.no_teams_found')"
              :description="$t('views.AdminSpendLimitsView.no_teams_found_description')"
              data-testid="admin-spend-limits-teams-empty"
            />
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
                    <InputText :aria-label="$t('views.AdminSpendLimitsView.team_daily_limit_aria', { team: team.name })"
                      :model-value="team.editingLimit == null ? '' : String(team.editingLimit)" @update:model-value="v => team.editingLimit = parseNumericInput(v)"
                      type="number"
                      min="0"
                      step="0.01"
                      :placeholder="$t('views.AdminSpendLimitsView.inherit_org_limit')"
                      class="max-w-40"
                      :data-testid="'admin-spend-limits-team-limit-' + team.id"
                    />
                    <p v-if="team.saveError" class="mt-1 text-xs text-destructive">{{ team.saveError }}</p>
                  </td>
                  <td class="table-cell-numeric">
                    <Button size="small" :disabled="team.saving" :data-testid="'admin-spend-limits-team-save-' + team.id" @click="saveTeamLimit(team)">
                      {{ team.saving ? $t('views.AdminSpendLimitsView.saving') : $t('views.AdminSpendLimitsView.save') }}
                    </Button>
                  </td>
                </tr>
              </tbody>
            </table>
          </template>
        </DataCard>

         <DataCard
           heading-level="h2"
           :title="$t('views.AdminSpendLimitsView.current_spend')"
          :description="$t('views.AdminSpendLimitsView.todays_accrued_costs_across_all_teams')"
        >
          <template #default>
            <div v-if="costsLoading" class="space-y-3" aria-busy="true" data-testid="admin-spend-limits-costs-loading">
              <SkeletonBlock height-class="h-16 w-full" />
              <SkeletonBlock height-class="h-12 w-full" />
            </div>
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
              <EmptyState
                v-else
                :title="$t('views.AdminSpendLimitsView.no_team_cost_data_available')"
                :description="$t('views.AdminSpendLimitsView.no_team_cost_data_description')"
                data-testid="admin-spend-limits-costs-empty"
              />
            </div>
          </template>
        </DataCard>
      </template>
    </FeatureGate>
  </div>
</template>

<script setup lang="ts">
import PageHeader from '../components/shared/PageHeader.vue'
import { ref, computed, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import { formatApiError } from '../lib/api/formatError'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import SkeletonBlock from '../components/shared/SkeletonBlock.vue'
import DataCard from '../components/ui/DataCard.vue'
import InputText from 'primevue/inputtext'
import Button from 'primevue/button'
import { formatMoney } from '../lib/money'
import { useOrgCurrency } from '../composables/useOrgCurrency'
import type { components } from '../lib/api/schema'
import { useCostPageTabs } from '../composables/useCostPageTabs'

const { t } = useI18n()
const planStore = usePlanStore()
const costPageTabs = useCostPageTabs()
const { currencyCode, loadCurrency } = useOrgCurrency()

type SpendLimitResponse = components['schemas']['SpendLimitResponse']
type CostReportResponse = components['schemas']['CostReportResponse']
type SetSpendLimitRequest = components['schemas']['SetSpendLimitRequest']

interface TeamLimitRow {
  team_id: string
  team_name: string
  daily_spend_limit: number | null
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

function parseNumericInput(value: unknown): number | null {
  if (value === '' || value == null) return null
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
  () => api.GET('/api/v1/admin/costs/limits').then((res) => ({
    data: res.data as SpendLimitResponse | undefined,
    error: res.error,
  })),
)

const orgLimit = ref<number | null>(null)
const teams = ref<TeamRow[]>([])

function onUpdateOrgLimit(v: unknown) {
  orgLimit.value = parseNumericInput(v)
}

watch(() => limitsData.value, (data) => {
  if (data) {
    orgLimit.value = data.org_daily_spend_limit ?? null
    teams.value = ((data.team_limits ?? []) as unknown as TeamLimitRow[]).map((t) => ({
      id: t.team_id,
      name: t.team_name,
      daily_limit_usd: t.daily_spend_limit ?? null,
      editingLimit: t.daily_spend_limit ?? null,
      saving: false,
      saveError: null,
    }))
  }
})

const { data: costsResp, loading: costsLoading, error: costsError } = useDataFetch(
  () => api.GET('/api/v1/admin/costs').then((res) => ({
    data: res.data as CostReportResponse | undefined,
    error: res.error,
  })),
  { initialValue: { period: 'month', group_by: 'team', items: [], org_total: '0', legacy_total: '0', org_unassigned_components: '0', has_more: false } },
)

const orgTotalCost = computed(() => {
  return parseDecimalString(costsResp.value.org_total) ?? 0
})
const teamCosts = computed<TeamCostItem[]>(() => {
  return (costsResp.value.items ?? []).map((item) => ({
    team_id: item.entity_id,
    team_name: item.entity_name,
    cost_usd: item.total_spend_usd,
    limit_usd: null,
  }))
})

const savingOrg = ref(false)
const orgSaveError = ref<string | null>(null)
const orgSaveSuccess = ref(false)

const ceilingMaxRun = ref<number | null>(null)
const ceilingSpend = ref<number | null>(null)
const ceilingRemaining = ref<number | null>(null)
const ceilingLoading = ref(false)
const ceilingLoadError = ref<string | null>(null)
const savingCeiling = ref(false)
const ceilingSaveError = ref<string | null>(null)
const ceilingSaveSuccess = ref(false)

async function loadCeiling() {
  ceilingLoading.value = true
  ceilingLoadError.value = null
  try {
    const { data, error: err } = await api.GET('/api/v1/admin/costs/ceiling')
    if (err) {
      ceilingLoadError.value = t('views.AdminSpendLimitsView.failed_to_load', { detail: formatApiError(err) })
    } else if (data) {
      const resp: components['schemas']['SpendCeilingResponse'] = data
      ceilingMaxRun.value = resp.max_run_cost ?? null
      ceilingSpend.value = resp.spend_ceiling ?? null
      ceilingRemaining.value = resp.remaining_budget_usd ?? null
    }
  } catch (e: unknown) {
    ceilingLoadError.value = t('views.AdminSpendLimitsView.failed_to_load', { detail: formatApiError(e) })
  } finally {
    ceilingLoading.value = false
  }
}

async function saveCeiling() {
  savingCeiling.value = true
  ceilingSaveError.value = null
  ceilingSaveSuccess.value = false
  try {
    const { error: err } = await api.PUT('/api/v1/admin/costs/ceiling', {
      body: { max_run_cost: ceilingMaxRun.value, spend_ceiling: ceilingSpend.value },
    })
    if (err) {
      ceilingSaveError.value = t('views.AdminSpendLimitsView.failed_to_save', { detail: formatApiError(err) })
    } else {
      ceilingSaveSuccess.value = true
      await loadCeiling()
    }
  } catch (e: unknown) {
    ceilingSaveError.value = t('views.AdminSpendLimitsView.failed_to_save', { detail: formatApiError(e) })
  } finally {
    savingCeiling.value = false
  }
}

function overageClass(cost: number, limit: number | null): string {
  if (limit === null || limit <= 0) return ''
  return cost > limit ? 'text-destructive' : 'text-success-text'
}

async function saveOrgLimit() {
  savingOrg.value = true
  orgSaveError.value = null
  orgSaveSuccess.value = false
  try {
    const body: SetSpendLimitRequest = { daily_spend_limit: orgLimit.value }
    const { error: err } = await api.PUT('/api/v1/admin/costs/limits/org', { body })
    if (err) {
      orgSaveError.value = t('views.AdminSpendLimitsView.failed_to_save', { detail: formatApiError(err) })
    } else {
      orgSaveSuccess.value = true
    }
  } catch (e: unknown) {
    orgSaveError.value = t('views.AdminSpendLimitsView.failed_to_save', { detail: formatApiError(e) })
  } finally {
    savingOrg.value = false
  }
}

async function saveTeamLimit(team: TeamRow) {
  team.saving = true
  team.saveError = null
  try {
    const body: SetSpendLimitRequest = { daily_spend_limit: team.editingLimit }
    const { error: err } = await api.PUT('/api/v1/admin/costs/limits/teams/{team_id}', {
      params: { path: { team_id: team.id } },
      body,
    })
    if (err) {
      team.saveError = t('views.AdminSpendLimitsView.failed_to_save', { detail: formatApiError(err) })
    } else {
      team.daily_limit_usd = team.editingLimit
    }
  } catch (e: unknown) {
    team.saveError = t('views.AdminSpendLimitsView.failed_to_save', { detail: formatApiError(e) })
  } finally {
    team.saving = false
  }
}

planStore.fetchPlan()
void loadCurrency()
void loadCeiling()
</script>
