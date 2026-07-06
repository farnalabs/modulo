<template>
  <PageTabs :tabs="[
    { label: 'Overview', to: '/admin/costs' },
    { label: 'Spend Limits', to: '/admin/costs/limits' },
    { label: 'Cost Controls', to: '/admin/costs/controls' },
  ]" />
  <div data-theme="agent" class="mx-auto max-w-6xl space-y-6 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">{{ $t('views.AdminCostBreakdownView.cost_controls') }}</h1>
      <p class="mt-1 text-muted-foreground">{{ $t('views.AdminCostControlsView.budget_overview_team_budgets_alert_thresholds_and_billing_se') }}</p>
    </header>

    <FeatureGate feature-name="admin_cost_controls" required-tier="team" show-disabled>

      <LoadingSpinner v-if="loading" />

      <ErrorAlert v-else-if="loadError" :message="loadError" :on-retry="loadAll" />

      <template v-else>
        <Card>
          <CardHeader>
            <CardTitle>{{ $t('views.AdminCostControlsView.budget_overview') }}</CardTitle>
            <CardDescription>{{ $t('views.AdminCostControlsView.current_billing_period_spend_vs_budget') }}</CardDescription>
          </CardHeader>
          <CardContent>
            <LoadingSpinner v-if="costsLoading" />
            <div v-else-if="costsError" class="text-sm text-destructive">{{ costsError }}</div>
            <div v-else class="space-y-4">
              <div class="grid grid-cols-1 gap-4 sm:grid-cols-3">
                <div class="rounded-lg border bg-muted p-4">
                  <p class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminCostBreakdownView.total_spend') }}</p>
                  <p class="mt-1 text-2xl font-bold" data-testid="cc-total-spend">{{ currencySymbol }}{{ totalSpend.toFixed(2) }}</p>
                </div>
                <div class="rounded-lg border bg-muted p-4">
                  <p class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminCostControlsView.budget') }}</p>
                  <p class="mt-1 text-2xl font-bold" data-testid="cc-budget">{{ currencySymbol }}{{ settings.budget.toFixed(2) }}</p>
                </div>
                <div class="rounded-lg border bg-muted p-4">
                  <p class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminCostControlsView.remaining') }}</p>
                  <p class="mt-1 text-2xl font-bold" :class="remainingClass" data-testid="cc-remaining">{{ currencySymbol }}{{ remainingBudget.toFixed(2) }}</p>
                </div>
              </div>
              <div>
                <div class="mb-1 flex items-center justify-between text-xs text-muted-foreground">
                  <span>{{ percentUsed.toFixed(1) }}% used</span>
                  <span>{{ currencySymbol }}{{ totalSpend.toFixed(2) }} / {{ currencySymbol }}{{ settings.budget.toFixed(2) }}</span>
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
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>{{ $t('views.AdminCostControlsView.team_budgets') }}</CardTitle>
            <CardDescription>{{ $t('views.AdminCostControlsView.set_per_team_budget_caps') }}</CardDescription>
          </CardHeader>
          <CardContent>
            <div v-if="teams.length === 0" class="py-4 text-center text-sm text-muted-foreground">
              {{ $t('views.AdminCostControlsView.no_teams_found') }}
            </div>
            <table v-else class="w-full text-sm">
              <thead>
                <tr class="border-b text-left text-muted-foreground">
                  <th class="pb-2 font-medium">{{ $t('views.AdminCostBreakdownView.team') }}</th>
                  <th class="pb-2 font-medium">{{ $t('views.AdminCostControlsView.budget') }} ({{ settings.currency }})</th>
                  <th class="pb-2 font-medium">{{ $t('views.AdminCostBreakdownView.total_spend') }}</th>
                  <th class="pb-2 font-medium" />
                </tr>
              </thead>
              <tbody>
                <tr v-for="team in teams" :key="team.id" class="border-b last:border-b-0">
                  <td class="py-3 font-medium">{{ team.name }}</td>
                  <td class="py-3">
                    <Input
                      :model-value="team.editingBudget ?? undefined" @update:model-value="(v: any) => team.editingBudget = v === '' ? null : Number(v)"
                      type="number"
                      min="0"
                      step="0.01"
                      :placeholder="$t('views.AdminCostControlsView.budget_placeholder')"
                      class="max-w-40"
                      :data-testid="'cc-team-budget-' + team.id"
                    />
                    <p v-if="team.saveError" class="mt-1 text-xs text-destructive">{{ team.saveError }}</p>
                  </td>
                  <td class="py-3 text-sm text-muted-foreground">
                    {{ currencySymbol }}{{ teamCostMap[team.id]?.toFixed(2) ?? '0.00' }}
                  </td>
                  <td class="py-3 text-right">
                    <Button size="sm" :disabled="team.saving" :data-testid="'cc-team-save-' + team.id" @click="saveTeamBudget(team)">
                      {{ team.saving ? $t('views.AdminCostControlsView.saving') : $t('views.AdminCostControlsView.save') }}
                    </Button>
                  </td>
                </tr>
              </tbody>
            </table>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>{{ $t('views.AdminCostControlsView.alert_thresholds') }}</CardTitle>
            <CardDescription>{{ $t('views.AdminCostControlsView.receive_notifications_when_spend_reaches_thresholds') }}</CardDescription>
          </CardHeader>
          <CardContent>
            <div class="space-y-3">
              <label class="flex items-center gap-3 rounded-lg border p-3" data-testid="cc-threshold-50">
                <input type="checkbox" :checked="settings.alertThresholds.includes(50)" @change="toggleThreshold(50)" class="h-4 w-4 rounded border-muted-foreground" />
                <div>
                  <p class="text-sm font-medium">{{ $t('views.AdminCostControlsView.caution_50') }}</p>
                  <p class="text-xs text-muted-foreground">{{ $t('views.AdminCostControlsView.notify_when_half_budget_consumed') }}</p>
                </div>
              </label>
              <label class="flex items-center gap-3 rounded-lg border p-3" data-testid="cc-threshold-75">
                <input type="checkbox" :checked="settings.alertThresholds.includes(75)" @change="toggleThreshold(75)" class="h-4 w-4 rounded border-muted-foreground" />
                <div>
                  <p class="text-sm font-medium">{{ $t('views.AdminCostControlsView.warning_75') }}</p>
                  <p class="text-xs text-muted-foreground">{{ $t('views.AdminCostControlsView.notify_when_three_quarters_consumed') }}</p>
                </div>
              </label>
              <label class="flex items-center gap-3 rounded-lg border p-3" data-testid="cc-threshold-90">
                <input type="checkbox" :checked="settings.alertThresholds.includes(90)" @change="toggleThreshold(90)" class="h-4 w-4 rounded border-muted-foreground" />
                <div>
                  <p class="text-sm font-medium">{{ $t('views.AdminCostControlsView.critical_90') }}</p>
                  <p class="text-xs text-muted-foreground">{{ $t('views.AdminCostControlsView.notify_when_budget_nearly_exhausted') }}</p>
                </div>
              </label>
              <label class="flex items-center gap-3 rounded-lg border p-3" data-testid="cc-threshold-100">
                <input type="checkbox" :checked="settings.alertThresholds.includes(100)" @change="toggleThreshold(100)" class="h-4 w-4 rounded border-muted-foreground" />
                <div>
                  <p class="text-sm font-medium">{{ $t('views.AdminCostControlsView.exceeded_100') }}</p>
                  <p class="text-xs text-muted-foreground">{{ $t('views.AdminCostControlsView.notify_when_budget_exceeded') }}</p>
                </div>
              </label>
              <p v-if="thresholdSaveError" class="text-xs text-destructive">{{ thresholdSaveError }}</p>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>{{ $t('views.AdminCostControlsView.circuit_breaker') }}</CardTitle>
            <CardDescription>{{ $t('views.AdminCostControlsView.automatically_stop_agent_runs_when_budget_exceeded') }}</CardDescription>
          </CardHeader>
          <CardContent>
            <div class="flex items-center justify-between rounded-lg border p-4">
              <div>
                <p class="text-sm font-medium">{{ $t('views.AdminCostControlsView.auto_stop_on_budget_exceeded') }}</p>
                <p class="text-xs text-muted-foreground">
                  {{ $t('views.AdminCostControlsView.when_enabled_all_agent_runs_paused') }}
                </p>
              </div>
              <label class="relative inline-flex cursor-pointer items-center" data-testid="cc-circuit-breaker">
                <input type="checkbox" class="peer sr-only" :checked="settings.circuitBreakerEnabled" @change="toggleCircuitBreaker" />
                <div class="peer h-6 w-11 rounded-full bg-muted-foreground/30 after:absolute after:start-[2px] after:top-[2px] after:h-5 after:w-5 after:rounded-full after:border after:border-muted after:bg-background after:transition-all peer-checked:bg-primary peer-checked:after:translate-x-full" />
              </label>
            </div>
            <p v-if="circuitBreakerSaveError" class="mt-2 text-xs text-destructive">{{ circuitBreakerSaveError }}</p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>{{ $t('views.AdminCostControlsView.billing_settings') }}</CardTitle>
            <CardDescription>{{ $t('views.AdminCostControlsView.configure_currency_and_billing_period') }}</CardDescription>
          </CardHeader>
          <CardContent>
            <div class="grid grid-cols-1 gap-6 sm:grid-cols-2">
              <div>
                <label class="mb-1.5 block text-xs font-medium text-muted-foreground">{{ $t('views.AdminCostControlsView.currency') }}</label>
                <select
                  :value="settings.currency"
                  @change="onCurrencyChange"
                  class="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm"
                  data-testid="cc-currency"
                >
                  <option value="USD">USD ($)</option>
                  <option value="EUR">EUR (€)</option>
                  <option value="GBP">GBP (£)</option>
                </select>
                <p v-if="currencySaveError" class="mt-1 text-xs text-destructive">{{ currencySaveError }}</p>
              </div>
              <div>
                <label class="mb-1.5 block text-xs font-medium text-muted-foreground">{{ $t('views.AdminCostControlsView.billing_period') }}</label>
                <select
                  :value="settings.billingPeriod"
                  @change="onBillingPeriodChange"
                  class="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm"
                  data-testid="cc-billing-period"
                >
                  <option value="monthly">Monthly</option>
                  <option value="quarterly">Quarterly</option>
                  <option value="annual">Annual</option>
                </select>
                <p v-if="periodSaveError" class="mt-1 text-xs text-destructive">{{ periodSaveError }}</p>
              </div>
            </div>
            <div class="mt-6">
              <label class="mb-1.5 block text-xs font-medium text-muted-foreground">{{ $t('views.AdminCostControlsView.monthly_budget', { currency: settings.currency }) }}</label>
              <div class="flex items-end gap-3">
                <div class="flex-1">
                  <Input
                    :model-value="settings.budget"
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
          </CardContent>
        </Card>
      </template>
    </FeatureGate>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from '../components/ui/card'
import { Input } from '../components/ui/input'
import { Button } from '../components/ui/button'
import PageTabs from "../components/PageTabs.vue"

const planStore = usePlanStore()

interface TeamCostItem {
  team_id: string
  team_name: string
  cost_usd: number
  limit_usd: number | null
}

interface CostReportData {
  org_total_usd: number
  teams: TeamCostItem[]
}

interface TeamLimitData {
  id: string
  name: string
  daily_limit_usd: number | null
}

interface SpendLimitResponse {
  org_daily_spend_limit: number | null
  team_limits: TeamLimitData[]
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

const loading = ref(true)
const loadError = ref<string | null>(null)

const costsLoading = ref(true)
const costsError = ref<string | null>(null)
const totalSpend = ref(0)
const teamCostMap = ref<Record<string, number>>({})

const teams = ref<TeamBudgetRow[]>([])

const settings = ref<ControlsSettings>({
  budget: 0,
  currency: 'USD',
  billingPeriod: 'monthly',
  alertThresholds: [50, 75, 90],
  circuitBreakerEnabled: false,
})

const savingBudget = ref(false)
const budgetSaveError = ref<string | null>(null)
const budgetSaveSuccess = ref(false)

const thresholdSaveError = ref<string | null>(null)
const circuitBreakerSaveError = ref<string | null>(null)
const currencySaveError = ref<string | null>(null)
const periodSaveError = ref<string | null>(null)

const currencyMap: Record<string, string> = {
  USD: '$',
  EUR: '€',
  GBP: '£',
}

const currencySymbol = computed(() => currencyMap[settings.value.currency] ?? '$')

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

async function loadAll() {
  loading.value = true
  loadError.value = null
  await Promise.all([loadCosts(), loadLimits(), loadSettings()])
  loading.value = false
}

async function loadCosts() {
  costsLoading.value = true
  costsError.value = null
  try {
    const { data, error: err } = await (api as any).GET('/api/v1/admin/costs')
    if (err) {
      costsError.value = `Failed to load costs: ${formatApiError(err)}`
    } else if (data) {
      const resp = data as CostReportData
      totalSpend.value = resp.org_total_usd ?? 0
      const map: Record<string, number> = {}
      for (const tc of resp.teams ?? []) {
        map[tc.team_id] = tc.cost_usd
      }
      teamCostMap.value = map
    }
  } catch (e: unknown) {
    costsError.value = `Failed to load costs: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    costsLoading.value = false
  }
}

async function loadLimits() {
  try {
    const { data, error: err } = await (api as any).GET('/api/v1/admin/costs/limits')
    if (err) {
      return
    } else if (data) {
      const resp = data as SpendLimitResponse
      teams.value = (resp.team_limits ?? []).map((t) => ({
        id: t.team_id as string,
        name: t.team_name as string,
        editingBudget: t.daily_spend_limit,
        saving: false,
        saveError: null,
      }))
    }
  } catch (err) {
    console.warn('Failed to load team limits:', err)
  }
}

async function loadSettings() {
  try {
    const { data, error: err } = await (api as any).GET('/api/v1/admin/costs/controls')
    if (err) {
      // endpoint may not exist yet — use defaults
      return
    } else if (data) {
      settings.value = { ...settings.value, ...data }
    }
  } catch (err) {
    console.warn('Failed to load settings, using defaults:', err)
  }
}

async function saveTeamBudget(team: TeamBudgetRow) {
  team.saving = true
  team.saveError = null
  try {
    const { error: err } = await (api as any).PUT(`/api/v1/admin/costs/limits/teams/${team.id}`, {
      body: { daily_spend_limit: team.editingBudget },
    })
    if (err) {
      team.saveError = `Failed to save: ${formatApiError(err)}`
    } else {
      budgetSaveSuccess.value = true
      setTimeout(() => { budgetSaveSuccess.value = false }, 3000)
    }
  } catch (e: unknown) {
    team.saveError = `Failed to save: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    team.saving = false
  }
}

async function saveBudget() {
  savingBudget.value = true
  budgetSaveError.value = null
  budgetSaveSuccess.value = false
  try {
    const { error: err } = await (api as any).PUT('/api/v1/admin/costs/controls', {
      body: { budget: settings.value.budget },
    })
    if (err) {
      budgetSaveError.value = `Failed to save: ${formatApiError(err)}`
    } else {
      budgetSaveSuccess.value = true
    }
  } catch (e: unknown) {
    budgetSaveError.value = `Failed to save: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    savingBudget.value = false
  }
}

async function toggleThreshold(threshold: number) {
  const prev = [...settings.value.alertThresholds]
  const idx = settings.value.alertThresholds.indexOf(threshold)
  if (idx >= 0) {
    settings.value.alertThresholds.splice(idx, 1)
  } else {
    settings.value.alertThresholds.push(threshold)
    settings.value.alertThresholds.sort((a, b) => a - b)
  }
  thresholdSaveError.value = null
  try {
    await (api as any).PUT('/api/v1/admin/costs/controls', { body: { alertThresholds: settings.value.alertThresholds } })
  } catch {
    settings.value.alertThresholds = prev
    thresholdSaveError.value = 'Failed to save thresholds'
  }
}

async function toggleCircuitBreaker() {
  const prev = settings.value.circuitBreakerEnabled
  settings.value.circuitBreakerEnabled = !prev
  circuitBreakerSaveError.value = null
  try {
    await (api as any).PUT('/api/v1/admin/costs/controls', { body: { circuitBreakerEnabled: settings.value.circuitBreakerEnabled } })
  } catch {
    settings.value.circuitBreakerEnabled = prev
    circuitBreakerSaveError.value = 'Failed to save circuit breaker'
  }
}

async function onCurrencyChange(e: Event) {
  const target = e.target as HTMLSelectElement
  const prev = settings.value.currency
  settings.value.currency = target.value as 'USD' | 'EUR' | 'GBP'
  currencySaveError.value = null
  try {
    await (api as any).PUT('/api/v1/admin/costs/controls', { body: { currency: settings.value.currency } })
  } catch {
    settings.value.currency = prev
    currencySaveError.value = 'Failed to save currency'
  }
}

async function onBillingPeriodChange(e: Event) {
  const target = e.target as HTMLSelectElement
  const prev = settings.value.billingPeriod
  settings.value.billingPeriod = target.value as 'monthly' | 'quarterly' | 'annual'
  periodSaveError.value = null
  try {
    await (api as any).PUT('/api/v1/admin/costs/controls', { body: { billingPeriod: settings.value.billingPeriod } })
  } catch {
    settings.value.billingPeriod = prev
    periodSaveError.value = 'Failed to save billing period'
  }
}

onMounted(() => {
  planStore.fetchPlan()
  loadAll()
})
</script>
