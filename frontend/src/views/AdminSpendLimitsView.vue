<template>
  <PageTabs :tabs="[
    { label: 'Overview', to: '/admin/costs' },
    { label: 'Spend Limits', to: '/admin/costs/limits' },
    { label: 'Cost Controls', to: '/admin/costs/controls' },
  ]" />
  <div data-theme="agent" class="mx-auto max-w-6xl space-y-6 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">Spend Limits</h1>
      <p class="mt-1 text-muted-foreground">Configure daily spend limits at the org and team level</p>
    </header>

    <FeatureGate feature-name="admin_spend_limits" required-tier="team">
      <template #locked="{ tooltip }">
        <div class="mb-4 flex items-center gap-2 rounded-lg border border-warning/30 bg-warning/5 p-4 text-sm text-warning">
          <LockIcon :locked="true" :tooltip="tooltip" />
          <span>Spend limits are not available on your current plan.</span>
        </div>
      </template>

      <LoadingSpinner v-if="loading" />

      <ErrorAlert v-else-if="loadError" :message="loadError" :on-retry="loadData" />

      <template v-else>
        <Card>
          <CardHeader>
            <CardTitle>Org-Level Daily Spend Limit</CardTitle>
            <CardDescription>Maximum daily spend across all teams in USD</CardDescription>
          </CardHeader>
          <CardContent>
            <div class="flex items-end gap-3">
              <div class="flex-1">
                <label class="mb-1.5 block text-xs font-medium text-muted-foreground">Daily limit (USD)</label>
                <Input :model-value="orgLimit ?? undefined" @update:model-value="(v: any) => orgLimit = v === '' ? null : Number(v)" type="number" min="0" step="0.01" placeholder="No limit" data-testid="admin-spend-limits-org-limit" />
              </div>
              <Button :disabled="savingOrg" data-testid="admin-spend-limits-org-save" @click="saveOrgLimit">
                {{ savingOrg ? 'Saving...' : 'Save' }}
              </Button>
            </div>
            <p v-if="orgSaveError" class="mt-2 text-xs text-destructive">{{ orgSaveError }}</p>
            <p v-if="orgSaveSuccess" class="mt-2 text-xs text-success">Org limit updated.</p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Per-Team Spend Limits</CardTitle>
            <CardDescription>Override the org-level limit for specific teams</CardDescription>
          </CardHeader>
          <CardContent>
            <div v-if="teams.length === 0" class="py-4 text-center text-sm text-muted-foreground">
              No teams found.
            </div>
            <table v-else class="w-full text-sm">
              <thead>
                <tr class="border-b text-left text-muted-foreground">
                  <th class="pb-2 font-medium">Team</th>
                  <th class="pb-2 font-medium">Daily Limit (USD)</th>
                  <th class="pb-2 font-medium" />
                </tr>
              </thead>
              <tbody>
                <tr v-for="team in teams" :key="team.id" class="border-b last:border-b-0">
                  <td class="py-3 font-medium">{{ team.name }}</td>
                  <td class="py-3">
                    <Input
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
                  <td class="py-3 text-right">
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
            <CardTitle>Current Spend</CardTitle>
            <CardDescription>Today's accrued costs across all teams</CardDescription>
          </CardHeader>
          <CardContent>
            <LoadingSpinner v-if="costsLoading" />
            <div v-else-if="costsError" class="text-sm text-destructive">{{ costsError }}</div>
            <div v-else class="space-y-4">
              <div class="flex items-center justify-between rounded-lg border bg-muted p-4">
                <span class="text-sm font-medium">Org Total</span>
                <span class="text-lg font-semibold" :class="overageClass(orgTotalCost, orgLimitValue)">
                  ${{ orgTotalCost.toFixed(2) }}
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
                    ${{ tc.cost_usd.toFixed(2) }}
                  </span>
                </div>
              </div>
              <p v-else class="text-sm text-muted-foreground">No team cost data available.</p>
            </div>
          </CardContent>
        </Card>
      </template>
    </FeatureGate>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { api } from '../lib/api/client'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import LockIcon from '../components/LockIcon.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from '../components/ui/card'
import { Input } from '../components/ui/input'
import { Button } from '../components/ui/button'
import PageTabs from "../components/PageTabs.vue"

const planStore = usePlanStore()

interface SpendLimitData {
  org_daily_limit_usd: number | null
  teams: Array<{
    id: string
    name: string
    daily_limit_usd: number | null
  }>
}

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

interface TeamRow {
  id: string
  name: string
  daily_limit_usd: number | null
  editingLimit: number | null
  saving: boolean
  saveError: string | null
}

const loading = ref(true)
const loadError = ref<string | null>(null)

const orgLimit = ref<number | null>(null)
const savingOrg = ref(false)
const orgSaveError = ref<string | null>(null)
const orgSaveSuccess = ref(false)

const teams = ref<TeamRow[]>([])

const costsLoading = ref(true)
const costsError = ref<string | null>(null)
const orgTotalCost = ref(0)
const teamCosts = ref<TeamCostItem[]>([])

function overageClass(cost: number, limit: number | null): string {
  if (limit === null || limit <= 0) return ''
  return cost > limit ? 'text-destructive' : 'text-success'
}

async function loadData() {
  loading.value = true
  loadError.value = null
  try {
    const { data, error: err } = await (api as any).GET('/api/v1/admin/costs/limits')
    if (err) {
      loadError.value = `Failed to load spend limits: ${err}`
    } else if (data) {
      const resp = data as SpendLimitData
      orgLimit.value = resp.org_daily_limit_usd
      teams.value = (resp.teams ?? []).map((t) => ({
        ...t,
        editingLimit: t.daily_limit_usd,
        saving: false,
        saveError: null,
      }))
    }
  } catch (e: unknown) {
    loadError.value = `Failed to load spend limits: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
}

async function loadCosts() {
  costsLoading.value = true
  costsError.value = null
  try {
    const { data, error: err } = await (api as any).GET('/api/v1/admin/costs')
    if (err) {
      costsError.value = `Failed to load costs: ${err}`
    } else if (data) {
      const resp = data as CostReportData
      orgTotalCost.value = resp.org_total_usd ?? 0
      teamCosts.value = resp.teams ?? []
    }
  } catch (e: unknown) {
    costsError.value = `Failed to load costs: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    costsLoading.value = false
  }
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
      orgSaveError.value = `Failed to save: ${err}`
    } else {
      orgSaveSuccess.value = true
    }
  } catch (e: unknown) {
    orgSaveError.value = `Failed to save: ${e instanceof Error ? e.message : String(e)}`
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
      team.saveError = `Failed to save: ${err}`
    } else {
      team.daily_limit_usd = team.editingLimit
    }
  } catch (e: unknown) {
    team.saveError = `Failed to save: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    team.saving = false
  }
}

onMounted(() => {
  planStore.fetchPlan()
  loadData()
  loadCosts()
})
</script>
