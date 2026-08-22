<template>
  <PageTabs :tabs="[
    { label: 'Overview', to: '/admin/costs' },
    { label: 'Spend Limits', to: '/admin/costs/limits' },
    { label: 'Cost Components', to: '/admin/costs/components' },
    { label: 'Cost Controls', to: '/admin/costs/controls' },
  ]" />
  <div data-theme="agent" class="page-wide">
    <PageHeader :title="$t('views.AdminCostBreakdownView.cost_controls')" :subtitle="$t('views.AdminCostControlsView.budget_overview_team_budgets_alert_thresholds_and_billing_se')" />

    <FeatureGate feature-name="admin_cost_controls" required-tier="team" show-disabled>
      <div class="space-y-6">
      <LoadingSpinner v-if="loading" />
      <template v-else>
        <Card>
          <template #title>{{ $t('views.AdminCostControlsView.budget_overview') }}</template>
<template #subtitle>{{ $t('views.AdminCostControlsView.current_billing_period_spend_vs_budget') }}</template>

          <template #content>
            <LoadingSpinner v-if="costsLoading" />
            <div v-else-if="costsError" class="text-sm text-destructive">{{ costsError }}</div>
            <div v-else class="space-y-4">
              <div class="grid grid-cols-1 gap-4 sm:grid-cols-3">
                <div class="rounded-lg border bg-muted p-4">
                  <p class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminCostBreakdownView.total_spend') }}</p>
                  <p class="mt-1 text-2xl font-semibold tabular-nums" data-testid="cc-total-spend">{{ formatMoney(totalSpend, settings.currency) }}</p>
                </div>
                <div class="rounded-lg border bg-muted p-4">
                  <p class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminCostControlsView.budget') }}</p>
                  <p class="mt-1 text-2xl font-semibold tabular-nums" data-testid="cc-budget">{{ formatMoney(settings.budget, settings.currency) }}</p>
                </div>
                <div class="rounded-lg border bg-muted p-4">
                  <p class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminCostControlsView.remaining') }}</p>
                  <p class="mt-1 text-2xl font-semibold tabular-nums" :class="remainingClass" data-testid="cc-remaining">{{ formatMoney(remainingBudget, settings.currency) }}</p>
                </div>
              </div>
              <div>
                <div class="mb-1 flex items-center justify-between text-xs text-muted-foreground">
                  <span>{{ $t('views.AdminCostControlsView.percent_used', { percent: percentUsed.toFixed(1) }) }}</span>
                  <span>{{ formatMoney(totalSpend, settings.currency) }} / {{ formatMoney(settings.budget, settings.currency) }}</span>
                </div>
                <div class="h-2.5 w-full overflow-hidden rounded-full bg-muted">
                  <div
                    class="h-full rounded-full transition-all duration-500"
                    :class="progressBarClass"
                    :style="{ width: Math.min(percentUsed, 100) + '%' }"
                    data-testid="cc-progress-bar"
                  />
                </div>
              </div>
            </div>
          </template>
        </Card>

        <Card>
          <template #title>{{ $t('views.AdminCostControlsView.team_budgets') }}</template>
<template #subtitle>{{ $t('views.AdminCostControlsView.set_per_team_budget_caps') }}</template>

          <template #content>
            <EmptyState v-if="teams.length === 0" :title="$t('views.AdminCostControlsView.no_teams_found')" />
            <DataTable
              v-else
              :columns="[
                { key: 'name', label: $t('views.AdminCostBreakdownView.team') },
                { key: 'budget', label: $t('views.AdminCostControlsView.budget') + ' (' + settings.currency + ')', numeric: true },
                { key: 'spend', label: $t('views.AdminCostBreakdownView.total_spend'), numeric: true },
                { key: 'actions', label: '' },
              ]"
              :rows="tableRows"
              :row-clickable="false"
            >
              <template #cell-budget="{ row }">
                <InputText :aria-label="$t('views.AdminCostControlsView.budget')"
                  :model-value="(row as any).editingBudget == null ? '' : String((row as any).editingBudget)" @update:model-value="(v: any) => (row as any).editingBudget = v === '' ? null : Number(v)"
                  type="number"
                  min="0"
                  step="0.01"
                  :placeholder="$t('views.AdminCostControlsView.budget_placeholder')"
                  class="max-w-40"
                  :data-testid="'cc-team-budget-' + (row as any).id"
                />
                <p v-if="(row as any).saveError" class="mt-1 text-xs text-destructive">{{ (row as any).saveError }}</p>
              </template>
              <template #cell-spend="{ row }">
                <span class="text-muted-foreground">{{ formatMoney(teamCostMap[(row as any).id] ?? 0, settings.currency) }}</span>
              </template>
              <template #cell-actions="{ row }">
                <div class="text-right">
                  <Button size="small" :disabled="(row as any).saving" :data-testid="'cc-team-save-' + (row as any).id" @click="saveTeamBudget(row as any)">
                    {{ (row as any).saving ? $t('views.AdminCostControlsView.saving') : $t('views.AdminCostControlsView.save') }}
                  </Button>
                </div>
              </template>
            </DataTable>
          </template>
        </Card>

        <Card>
          <template #title>{{ $t('views.AdminCostControlsView.alert_thresholds') }}</template>
<template #subtitle>{{ $t('views.AdminCostControlsView.receive_notifications_when_spend_reaches_thresholds') }}</template>

          <template #content>
            <div class="space-y-3">
              <label for="admincostcontrolsview-field-7" class="flex items-center gap-3 rounded-lg border p-3" data-testid="cc-threshold-50">
                <input id="admincostcontrolsview-field-7" type="checkbox" :checked="settings.alertThresholds.includes(50)" @change="toggleThreshold(50)" class="h-4 w-4 rounded border-muted-foreground" />
                <div>
                  <p class="text-sm font-medium">{{ $t('views.AdminCostControlsView.caution_50') }}</p>
                  <p class="text-xs text-muted-foreground">{{ $t('views.AdminCostControlsView.notify_when_half_budget_consumed') }}</p>
                </div>
              </label>
              <label for="admincostcontrolsview-field-6" class="flex items-center gap-3 rounded-lg border p-3" data-testid="cc-threshold-75">
                <input id="admincostcontrolsview-field-6" type="checkbox" :checked="settings.alertThresholds.includes(75)" @change="toggleThreshold(75)" class="h-4 w-4 rounded border-muted-foreground" />
                <div>
                  <p class="text-sm font-medium">{{ $t('views.AdminCostControlsView.warning_75') }}</p>
                  <p class="text-xs text-muted-foreground">{{ $t('views.AdminCostControlsView.notify_when_three_quarters_consumed') }}</p>
                </div>
              </label>
              <label for="admincostcontrolsview-field-5" class="flex items-center gap-3 rounded-lg border p-3" data-testid="cc-threshold-90">
                <input id="admincostcontrolsview-field-5" type="checkbox" :checked="settings.alertThresholds.includes(90)" @change="toggleThreshold(90)" class="h-4 w-4 rounded border-muted-foreground" />
                <div>
                  <p class="text-sm font-medium">{{ $t('views.AdminCostControlsView.critical_90') }}</p>
                  <p class="text-xs text-muted-foreground">{{ $t('views.AdminCostControlsView.notify_when_budget_nearly_exhausted') }}</p>
                </div>
              </label>
              <label for="admincostcontrolsview-field-4" class="flex items-center gap-3 rounded-lg border p-3" data-testid="cc-threshold-100">
                <input id="admincostcontrolsview-field-4" type="checkbox" :checked="settings.alertThresholds.includes(100)" @change="toggleThreshold(100)" class="h-4 w-4 rounded border-muted-foreground" />
                <div>
                  <p class="text-sm font-medium">{{ $t('views.AdminCostControlsView.exceeded_100') }}</p>
                  <p class="text-xs text-muted-foreground">{{ $t('views.AdminCostControlsView.notify_when_budget_exceeded') }}</p>
                </div>
              </label>
              <p v-if="thresholdSaveError" class="text-xs text-destructive">{{ thresholdSaveError }}</p>
            </div>
          </template>
        </Card>

        <Card>
          <template #title>{{ $t('views.AdminCostControlsView.circuit_breaker') }}</template>
<template #subtitle>{{ $t('views.AdminCostControlsView.automatically_stop_agent_runs_when_budget_exceeded') }}</template>

          <template #content>
            <div class="flex items-center justify-between rounded-lg border p-4">
              <div>
                <p class="text-sm font-medium">{{ $t('views.AdminCostControlsView.auto_stop_on_budget_exceeded') }}</p>
                <p class="text-xs text-muted-foreground">
                  {{ $t('views.AdminCostControlsView.when_enabled_all_agent_runs_paused') }}
                </p>
              </div>
              <label for="admincostcontrolsview-field-3" class="relative inline-flex cursor-pointer items-center" data-testid="cc-circuit-breaker">
                <input id="admincostcontrolsview-field-3" type="checkbox" class="peer sr-only" :aria-label="$t('views.AdminCostControlsView.auto_stop_on_budget_exceeded')" :checked="settings.circuitBreakerEnabled" @change="toggleCircuitBreaker" />
                <div class="peer h-6 w-11 rounded-full bg-muted-foreground/30 after:absolute after:start-[2px] after:top-[2px] after:h-5 after:w-5 after:rounded-full after:border after:border-muted after:bg-background after:transition-all peer-checked:bg-primary peer-checked:after:translate-x-full" />
              </label>
            </div>
            <p v-if="circuitBreakerSaveError" class="mt-2 text-xs text-destructive">{{ circuitBreakerSaveError }}</p>
          </template>
        </Card>

        <Card>
          <template #title>{{ $t('views.AdminCostControlsView.billing_settings') }}</template>
<template #subtitle>{{ $t('views.AdminCostControlsView.configure_currency_and_billing_period') }}</template>

          <template #content>
            <div class="grid grid-cols-1 gap-6 sm:grid-cols-2">
              <div>
                <label for="admincostcontrolsview-field-2" class="mb-1.5 block text-xs font-medium text-muted-foreground">{{ $t('views.AdminCostControlsView.currency') }}</label>
                <Select
  :aria-label="$t('views.AdminCostControlsView.currency')"
  :model-value="settings.currency"
  @update:model-value="onCurrencyChange"
  :placeholder="$t('views.AdminCostControlsView.currency_usd')"
  data-testid="cc-currency"
  class="w-full"
  :options="[{ value: 'USD', label: $t('views.AdminCostControlsView.currency_usd') }, { value: 'EUR', label: $t('views.AdminCostControlsView.currency_eur') }, { value: 'GBP', label: $t('views.AdminCostControlsView.currency_gbp') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
                <p v-if="currencySaveError" class="mt-1 text-xs text-destructive">{{ currencySaveError }}</p>
              </div>
              <div>
                <label for="admincostcontrolsview-field-1" class="mb-1.5 block text-xs font-medium text-muted-foreground">{{ $t('views.AdminCostControlsView.billing_period') }}</label>
                <Select
  :aria-label="$t('views.AdminCostControlsView.billing_period')"
  :model-value="settings.billingPeriod"
  @update:model-value="onBillingPeriodChange"
  :placeholder="$t('views.AdminCostControlsView.monthly')"
  data-testid="cc-billing-period"
  class="w-full"
  :options="[{ value: 'monthly', label: $t('views.AdminCostControlsView.monthly') }, { value: 'quarterly', label: $t('views.AdminCostControlsView.quarterly') }, { value: 'annual', label: $t('views.AdminCostControlsView.annual') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
                <p v-if="periodSaveError" class="mt-1 text-xs text-destructive">{{ periodSaveError }}</p>
              </div>
            </div>
            <div class="mt-6">
              <span class="mb-1.5 block text-xs font-medium text-muted-foreground">{{ $t('views.AdminCostControlsView.monthly_budget', { currency: settings.currency }) }}</span>
              <div class="flex items-end gap-3">
                <div class="flex-1">
                  <InputText :aria-label="$t('views.AdminCostControlsView.monthly_budget', { currency: settings.currency })"
                    :model-value="settings.budget == null ? '' : String(settings.budget)"
                    @update:model-value="(v: any) => settings.budget = v === '' ? 0 : Number(v)"
                    type="number"
                    min="0"
                    step="0.01"
                    placeholder="0.00"
                    data-testid="cc-budget-input"
                  />
                </div>
                <Button :disabled="savingBudget" data-testid="cc-budget-save" @click="saveBudget">
                  {{ savingBudget ? $t('views.AdminCostControlsView.saving') : $t('views.AdminCostControlsView.save') }}
                </Button>
              </div>
              <p v-if="budgetSaveError" class="mt-2 text-xs text-destructive">{{ budgetSaveError }}</p>
              <p v-if="budgetSaveSuccess" class="mt-2 text-xs text-success">{{ $t('views.AdminCostControlsView.budget_updated') }}</p>
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
import { ref, computed, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import { formatApiError } from '../lib/api/formatError'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import Card from 'primevue/card'
import InputText from 'primevue/inputtext'
import Button from 'primevue/button'
import { DataTable } from '../components/ui/data-table'
import Select from 'primevue/select'
import PageTabs from "../components/PageTabs.vue"
import { formatMoney } from '../lib/money'

const planStore = usePlanStore()
const { t } = useI18n()

interface CostReportComponent {
  name: string
  amount_usd: string
}

interface CostReportAnnotations {
  refused_total_usd?: number | null
  clamped_total_usd?: number | null
}

interface CostReportRow {
  entity_id: string
  entity_name: string
  total_spend_usd: number
  total_runs: number
  components: CostReportComponent[]
  annotations: CostReportAnnotations | null
}

interface CostReportData {
  period: string
  group_by: string
  items: CostReportRow[]
  org_unassigned_components?: string | null
  legacy_total?: string | null
  org_total?: string | null
  has_more?: boolean
}

function parseDecimalString(value: string | null | undefined): number | null {
  if (value == null) return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

interface ControlsSettings {
  budget: number
  currency: 'USD' | 'EUR' | 'GBP'
  billingPeriod: 'monthly' | 'quarterly' | 'annual'
  alertThresholds: number[]
  circuitBreakerEnabled: boolean
}

interface TeamBudgetRow {
  id: string
  name: string
  editingBudget: number | null
  saving: boolean
  saveError: string | null
}

const { data: costsData, loading: costsLoading, error: costsError, load: loadCosts } = useDataFetch(
  () => api.GET('/api/v1/admin/costs'),
  { initialValue: { period: 'month', group_by: 'team', items: [], org_total: '0', legacy_total: '0', org_unassigned_components: '0', has_more: false } }
)

const totalSpend = ref(0)
const teamCostMap = ref<Record<string, number>>({})

watch(() => costsData.value, (data) => {
  if (data) {
    const resp = data as CostReportData
    totalSpend.value = parseDecimalString(resp.org_total) ?? 0
    const map: Record<string, number> = {}
    for (const item of resp.items ?? []) {
      map[item.entity_id] = item.total_spend_usd
    }
    teamCostMap.value = map
  }
})

const { data: limitsData, loading: limitsLoading, load: loadLimits } = useDataFetch(
  () => api.GET('/api/v1/admin/costs/limits'),
  { immediate: false }
)

const teams = ref<TeamBudgetRow[]>([])

const tableRows = computed(() => teams.value)

watch(() => limitsData.value, (data) => {
  if (data) {
    teams.value = (data.team_limits ?? []).map((t: Record<string, unknown>) => ({
      id: t.team_id as string,
      name: t.team_name as string,
      editingBudget: t.daily_spend_limit as number | null,
      saving: false,
      saveError: null,
    }))
  }
})

const settings = ref<ControlsSettings>({
  budget: 0,
  currency: 'USD',
  billingPeriod: 'monthly',
  alertThresholds: [50, 75, 90],
  circuitBreakerEnabled: false,
})

const settingsLoading = ref(false)

async function loadAll() {
  await Promise.all([loadCosts(), loadLimits(), loadSettings()])
}

const loading = computed(() => costsLoading.value || limitsLoading.value || settingsLoading.value)

const savingBudget = ref(false)
const budgetSaveError = ref<string | null>(null)
const budgetSaveSuccess = ref(false)

const thresholdSaveError = ref<string | null>(null)
const circuitBreakerSaveError = ref<string | null>(null)
const currencySaveError = ref<string | null>(null)
const periodSaveError = ref<string | null>(null)

const remainingBudget = computed(() => Math.max(0, settings.value.budget - totalSpend.value))
const percentUsed = computed(() => {
  if (settings.value.budget <= 0) return 0
  return (totalSpend.value / settings.value.budget) * 100
})

const remainingClass = computed(() => {
  if (settings.value.budget <= 0) return ''
  const pct = percentUsed.value
  if (pct >= 100) return 'text-destructive'
  if (pct >= 90) return 'text-warning'
  return 'text-success'
})

const progressBarClass = computed(() => {
  const pct = percentUsed.value
  if (pct >= 100) return 'bg-destructive'
  if (pct >= 90) return 'bg-warning'
  if (pct >= 75) return 'bg-amber-500'
  return 'bg-primary'
})

async function loadSettings() {
  settingsLoading.value = true
  try {
    const { data, error: err } = await api.GET('/api/v1/admin/costs/controls')
    if (err) {
      return
    } else if (data) {
      const resp = data as {
        budget?: number | null
        currency?: string | null
        billing_period?: string | null
        alert_thresholds?: number[] | null
        circuit_breaker_enabled?: boolean | null
      }
      settings.value = {
        ...settings.value,
        ...(typeof resp.budget === 'number' ? { budget: resp.budget } : {}),
        ...(typeof resp.currency === 'string' && resp.currency ? { currency: resp.currency as ControlsSettings['currency'] } : {}),
        ...(typeof resp.billing_period === 'string' && resp.billing_period ? { billingPeriod: resp.billing_period as ControlsSettings['billingPeriod'] } : {}),
        ...(Array.isArray(resp.alert_thresholds) ? { alertThresholds: resp.alert_thresholds } : {}),
        ...(typeof resp.circuit_breaker_enabled === 'boolean' ? { circuitBreakerEnabled: resp.circuit_breaker_enabled } : {}),
      }
    }
  } catch (e) {
    console.warn('Failed to load cost control settings', e)
  } finally {
    settingsLoading.value = false
  }
}

async function saveTeamBudget(team: TeamBudgetRow) {
  team.saving = true
  team.saveError = null
  try {
    const { error: err } = await api.PUT('/api/v1/admin/costs/limits/teams/{team_id}', {
      params: { path: { team_id: team.id } },
      body: { daily_spend_limit: team.editingBudget },
    })
    if (err) {
      team.saveError = t('views.AdminCostControlsView.failed_to_save_team_budget', { detail: formatApiError(err) })
    } else {
      budgetSaveSuccess.value = true
      setTimeout(() => { budgetSaveSuccess.value = false }, 3000)
    }
  } catch (e: unknown) {
    team.saveError = t('views.AdminCostControlsView.failed_to_save_team_budget', { detail: formatApiError(e) })
  } finally {
    team.saving = false
  }
}

async function saveBudget() {
  savingBudget.value = true
  budgetSaveError.value = null
  budgetSaveSuccess.value = false
  try {
    const { error: err } = await api.PUT('/api/v1/admin/costs/controls', {
      body: { budget: settings.value.budget },
    })
    if (err) {
      budgetSaveError.value = t('views.AdminCostControlsView.failed_to_save_budget', { detail: formatApiError(err) })
    } else {
      budgetSaveSuccess.value = true
    }
  } catch (e: unknown) {
    budgetSaveError.value = t('views.AdminCostControlsView.failed_to_save_budget', { detail: formatApiError(e) })
  } finally {
    savingBudget.value = false
  }
}

async function toggleThreshold(threshold: number) {
  const prev = [...settings.value.alertThresholds]
  const idx = settings.value.alertThresholds.indexOf(threshold)
  const next = [...prev]
  if (idx >= 0) {
    next.splice(idx, 1)
  } else {
    next.push(threshold)
    next.sort((a, b) => a - b)
  }
  if (next.length === 0) {
    thresholdSaveError.value = t('views.AdminCostControlsView.at_least_one_threshold')
    return
  }
  settings.value.alertThresholds = next
  thresholdSaveError.value = null
  const { error: err } = await api.PUT('/api/v1/admin/costs/controls', { body: { alert_thresholds: next } })
  if (err) {
    settings.value.alertThresholds = prev
    thresholdSaveError.value = t('views.AdminCostControlsView.failed_to_save_thresholds', { detail: formatApiError(err) })
  }
}

async function toggleCircuitBreaker() {
  const prev = settings.value.circuitBreakerEnabled
  const next = !prev
  settings.value.circuitBreakerEnabled = next
  circuitBreakerSaveError.value = null
  const { error: err } = await api.PUT('/api/v1/admin/costs/controls', { body: { circuit_breaker_enabled: next } })
  if (err) {
    settings.value.circuitBreakerEnabled = prev
    circuitBreakerSaveError.value = t('views.AdminCostControlsView.failed_to_save_circuit_breaker', { detail: formatApiError(err) })
  }
}

async function onCurrencyChange(value: unknown) {
  const prev = settings.value.currency
  settings.value.currency = String(value) as 'USD' | 'EUR' | 'GBP'
  currencySaveError.value = null
  try {
    await api.PUT('/api/v1/admin/costs/controls', { body: { currency: settings.value.currency } })
  } catch {
    settings.value.currency = prev
    currencySaveError.value = t('views.AdminCostControlsView.failed_to_save_currency')
  }
}

async function onBillingPeriodChange(value: unknown) {
  const prev = settings.value.billingPeriod
  const next = String(value) as 'monthly' | 'quarterly' | 'annual'
  settings.value.billingPeriod = next
  periodSaveError.value = null
  const { error: err } = await api.PUT('/api/v1/admin/costs/controls', { body: { billing_period: next } })
  if (err) {
    settings.value.billingPeriod = prev
    periodSaveError.value = t('views.AdminCostControlsView.failed_to_save_billing_period', { detail: formatApiError(err) })
  }
}

planStore.fetchPlan()
loadAll()
</script>
