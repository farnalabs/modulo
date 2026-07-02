<template>
  <div class="mx-auto max-w-6xl space-y-8 p-6">
    <DashboardNotificationsPanel class="mb-4" />
    <header>
      <h1 data-testid="dashboard-title" class="text-3xl font-bold tracking-tight">Dashboard</h1>
      <p class="mt-1 text-muted-foreground">{{ $t('views.DashboardView.overview_of_your_organisations_pipelines_and_runs') }}</p>
    </header>

    <!-- Loading skeleton grid -->
    <div v-if="loading" class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
      <div v-for="n in 6" :key="n" class="card p-4">
        <div class="flex items-center gap-3">
          <div class="h-9 w-9 animate-pulse rounded-lg bg-muted" />
          <div class="min-w-0 flex-1 space-y-2">
            <div class="h-3 w-20 animate-pulse rounded bg-muted" />
            <div class="h-6 w-12 animate-pulse rounded bg-muted" />
          </div>
        </div>
      </div>
    </div>

    <!-- Full-page error -->
    <ErrorAlert v-else-if="error && !summary" :message="error" :on-retry="dashboardStore.fetchSummary" />

    <template v-else-if="summary">

      <!-- Config warnings -->
      <div v-if="summary.config_warnings && summary.config_warnings.length > 0" class="space-y-2">
        <div
          v-for="w in summary.config_warnings"
          :key="w.type"
          class="flex items-center justify-between gap-3 rounded-lg border border-warning/30 bg-warning/5 p-4 text-sm text-warning"
        >
          <div class="flex items-center gap-2">
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="shrink-0"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
            <span>{{ w.message }}</span>
          </div>
          <a
            :href="w.action_url"
            class="shrink-0 rounded-md bg-warning/15 px-3 py-1.5 text-xs font-semibold text-warning hover:bg-warning/25 transition-colors"
          >
            {{ w.action_label }}
          </a>
        </div>
      </div>

      <!-- Row 1: Summary stat cards -->
      <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard label="Total Runs" :value="summary.total_runs" color="primary">
          <template #icon><line x1="12" y1="20" x2="12" y2="10"/><line x1="18" y1="20" x2="18" y2="4"/><line x1="6" y1="20" x2="6" y2="16"/></template>
        </StatCard>
        <StatCard label="Active Pipelines" :value="summary.active_pipelines" color="primary">
          <template #icon><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></template>
        </StatCard>
        <StatCard label="Running" :value="summary.run_counts_by_status?.running ?? 0" color="success">
          <template #icon><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></template>
        </StatCard>
        <StatCard label="Awaiting Human" :value="summary.run_counts_by_status?.awaiting_human ?? 0" color="warning">
          <template #icon><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></template>
        </StatCard>
      </div>

      <div class="grid gap-4 sm:grid-cols-2">
        <StatCard label="Failed" :value="summary.run_counts_by_status?.failed ?? 0" color="destructive">
          <template #icon><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></template>
        </StatCard>
        <StatCard label="Idle" :value="summary.run_counts_by_status?.idle ?? 0" color="muted">
          <template #icon><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></template>
        </StatCard>
      </div>

      <!-- Eval pass rate + Token spend -->
      <div class="grid gap-4 sm:grid-cols-2">
        <!-- Eval pass rate card -->
        <div class="card p-4">
          <p class="text-sm font-medium text-muted-foreground mb-2">Eval Pass Rate</p>
          <div v-if="summary.eval_pass_rate">
            <p class="text-3xl font-bold">{{ summary.eval_pass_rate.overall_pass_rate }}%</p>
            <div class="flex items-center gap-2 mt-1">
              <span :class="evalTrendClass" class="inline-flex items-center text-sm font-medium">
                <svg v-if="evalTrend === 'up'" xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="18 15 12 9 6 15"/></svg>
                <svg v-else xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
                {{ evalTrendLabel }}
              </span>
              <span class="text-xs text-muted-foreground">{{ summary.eval_pass_rate.total_evals }} total evals</span>
            </div>
            <Sparkline class="mt-2 h-10 w-full" :data="evalSparklineData" color="var(--color-primary)" />
          </div>
          <div v-else class="flex items-center justify-center py-6 text-sm text-muted-foreground">No eval data yet</div>
        </div>

        <!-- Token spend card -->
        <div class="card p-4">
          <p class="text-sm font-medium text-muted-foreground mb-2">Token Spend (7d)</p>
          <p class="text-3xl font-bold">${{ totalSpend.toFixed(2) }}</p>
          <p class="text-xs text-muted-foreground mt-1">{{ summary.trend?.length ?? 0 }} days tracked</p>
          <Sparkline class="mt-2 h-10 w-full" :data="spendSparklineData" color="var(--color-warning)" />
        </div>
      </div>

      <!-- Team breakdown (Team only) -->
      <div v-if="isTeam && summary.teams && summary.teams.length > 0" class="card p-4">
        <div class="flex items-center justify-between mb-4">
          <h2 class="text-lg font-semibold">Team Breakdown</h2>
          <span class="text-xs font-medium text-muted-foreground bg-muted px-2 py-0.5 rounded">Team</span>
        </div>
        <table class="w-full text-sm">
          <thead>
            <tr class="border-b text-left text-muted-foreground">
              <th class="pb-2 font-medium">Team</th>
              <th class="pb-2 font-medium text-right">Runs</th>
              <th class="pb-2 font-medium text-right">Running</th>
              <th class="pb-2 font-medium text-right">Failed</th>
              <th class="pb-2 font-medium text-right">Eval Pass</th>
              <th class="pb-2 font-medium text-right w-8"></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="team in summary.teams" :key="team.id"
                role="button"
                tabindex="0"
                class="border-b last:border-0 cursor-pointer hover:bg-muted/50"
                @click="toggleTeam(team.id)"
                @keydown.enter="toggleTeam(team.id)"
                @keydown.space.prevent="toggleTeam(team.id)">
              <td class="py-2.5 font-medium">{{ team.name }}</td>
              <td class="py-2.5 text-right">{{ team.total_runs }}</td>
              <td class="py-2.5 text-right text-success">{{ team.run_counts_by_status.running }}</td>
              <td class="py-2.5 text-right text-destructive">{{ team.run_counts_by_status.failed }}</td>
              <td class="py-2.5 text-right">{{ team.eval_pass_rate ? team.eval_pass_rate.pass_rate + '%' : '—' }}</td>
              <td class="py-2.5 text-right">
                <svg v-if="expandedTeam === team.id" xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="18 15 12 9 6 15"/></svg>
                <svg v-else xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
              </td>
            </tr>
            <tr v-if="expandedTeam && expandedTeamData">
              <td colspan="6" class="py-3 pl-6">
                <div class="text-xs text-muted-foreground space-y-1">
                  <p>Active pipelines: <span class="font-medium text-foreground">{{ expandedTeamData.active_pipelines }}</span></p>
                  <p>Awaiting human: <span class="font-medium text-foreground">{{ expandedTeamData.run_counts_by_status.awaiting_human }}</span></p>
                  <p>Idle: <span class="font-medium text-foreground">{{ expandedTeamData.run_counts_by_status.idle }}</span></p>
                  <p v-if="expandedTeamData.eval_pass_rate">
                    Evals: <span class="font-medium text-foreground">{{ expandedTeamData.eval_pass_rate.passed_evals }} / {{ expandedTeamData.eval_pass_rate.total_evals }} passed</span>
                  </p>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- Trend chart section -->
      <div class="card p-4">
        <div class="flex items-center justify-between mb-4">
          <h2 class="text-lg font-semibold">Run Activity</h2>
          <div class="flex gap-1">
            <button v-for="d in trendDurations" :key="d.value"
                    :class="['px-3 py-1 text-xs font-medium rounded transition-colors',
                             trendDuration === d.value ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground hover:bg-muted/80']"
                    @click="switchTrendDuration(d.value)">
              {{ d.label }}
            </button>
          </div>
        </div>

        <div v-if="trendData.length > 1" class="space-y-4">
          <div>
            <p class="text-xs font-medium text-muted-foreground mb-1">Run count</p>
            <Sparkline class="h-12 w-full" :data="trendRunCounts" color="var(--color-primary)" />
          </div>
          <div>
            <p class="text-xs font-medium text-muted-foreground mb-1">Eval pass rate</p>
            <Sparkline class="h-12 w-full" :data="trendEvalRates" color="var(--color-success)" />
          </div>
          <div>
            <p class="text-xs font-medium text-muted-foreground mb-1">Token spend</p>
            <Sparkline class="h-12 w-full" :data="trendSpendData" color="var(--color-warning)" />
          </div>
        </div>
        <div v-else class="flex items-center justify-center py-8 text-sm text-muted-foreground">
          Not enough trend data to display
        </div>
      </div>

      <!-- Recent runs list -->
      <div class="card p-4">
        <h2 class="text-lg font-semibold mb-4">Recent Runs</h2>
        <div v-if="summary.recent_runs && summary.recent_runs.length > 0" class="divide-y">
          <div v-for="run in summary.recent_runs" :key="run.id" class="flex items-center justify-between py-2.5 first:pt-0 last:pb-0">
            <div class="min-w-0 flex-1">
              <Tooltip :delay-duration="300">
                <TooltipTrigger as-child>
                  <p class="text-sm font-medium truncate">{{ run.pipeline_name }}</p>
                </TooltipTrigger>
                <TooltipContent side="top">
                  <p>{{ run.pipeline_name }}</p>
                </TooltipContent>
              </Tooltip>
              <p class="text-xs text-muted-foreground">{{ formatTimestamp(run.created_at) }}</p>
            </div>
            <div class="flex items-center gap-2 ml-3">
              <span :class="statusBadgeClass(run.status)" class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium capitalize">
                {{ run.status }}
              </span>
              <span class="text-xs text-muted-foreground hidden sm:inline">{{ run.trigger_type }}</span>
            </div>
          </div>
        </div>
        <div v-else class="flex items-center justify-center py-6 text-sm text-muted-foreground">
          No runs yet
        </div>
      </div>

      <!-- Empty state CTA for fresh organisations -->
      <div v-if="summary.total_runs === 0 && summary.active_pipelines === 0" class="rounded-lg border bg-card p-8 text-center">
        <p class="text-lg font-medium">Welcome to Modulo</p>
        <p class="mt-1 text-sm text-muted-foreground">
          Get started by creating your first pipeline or exploring a template.
        </p>
        <div class="mt-4 flex items-center justify-center gap-3">
          <a
            href="/library"
            class="rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground border border-primary/30 hover:brightness-110 transition-all"
          >
            Create Pipeline
          </a>
          <a
            href="/templates"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent transition-all"
          >
            Browse Templates
          </a>
        </div>
      </div>

    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import DashboardNotificationsPanel from '../components/DashboardNotificationsPanel.vue'
import { usePlanStore } from '../stores/planStore'
import { useDashboardStore } from '../stores/dashboard'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import Sparkline from '../components/shared/Sparkline.vue'
import StatCard from '../components/StatCard.vue'
import {
  Tooltip,
  TooltipTrigger,
  TooltipContent,
} from '../components/ui/tooltip'

const planStore = usePlanStore()
const dashboardStore = useDashboardStore()

const loading = computed(() => dashboardStore.loading)
const error = computed(() => dashboardStore.error)
const summary = computed(() => dashboardStore.summary)
const totalSpend = computed(() => dashboardStore.totalSpend)

const isTeam = computed(() => planStore.isTeam)

const expandedTeam = ref<string | null>(null)

function toggleTeam(teamId: string) {
  expandedTeam.value = expandedTeam.value === teamId ? null : teamId
}

const expandedTeamData = computed(() => {
  if (!expandedTeam.value || !summary.value?.teams) return null
  return summary.value.teams.find(t => t.id === expandedTeam.value) ?? null
})

const evalSparklineData = computed(() => {
  if (!summary.value?.trend) return []
  return summary.value.trend.map(d => d.eval_pass_rate ?? 0)
})

const lastEvalRates = computed(() => {
  if (!summary.value?.trend) return []
  return summary.value.trend
    .map(d => d.eval_pass_rate)
    .filter((r): r is number => r !== null)
})

const evalTrend = computed(() => {
  const rates = lastEvalRates.value
  if (rates.length < 2) return 'flat'
  const firstHalf = rates.slice(0, Math.floor(rates.length / 2))
  const secondHalf = rates.slice(Math.floor(rates.length / 2))
  const firstAvg = firstHalf.reduce((a, b) => a + b, 0) / firstHalf.length
  const secondAvg = secondHalf.reduce((a, b) => a + b, 0) / secondHalf.length
  if (secondAvg > firstAvg) return 'up'
  if (secondAvg < firstAvg) return 'down'
  return 'flat'
})

const evalTrendClass = computed(() => {
  if (evalTrend.value === 'up') return 'text-success'
  if (evalTrend.value === 'down') return 'text-destructive'
  return 'text-muted-foreground'
})

const evalTrendLabel = computed(() => {
  if (evalTrend.value === 'up') return 'Improving'
  if (evalTrend.value === 'down') return 'Declining'
  return 'Stable'
})

const spendSparklineData = computed(() => {
  if (!summary.value?.trend) return []
  return summary.value.trend.map(d => d.token_spend_usd)
})

const trendDuration = ref(7)
const trendDurations = [
  { label: '7d', value: 7 },
  { label: '30d', value: 30 },
  { label: '90d', value: 90 },
]

const trendsRaw = computed(() => dashboardStore.trends)

const trendData = computed(() => {
  const tr = trendsRaw.value
  if (!tr) {
    // Fall back to summary trend when /trends hasn't been fetched yet
    if (!summary.value?.trend) return []
    return summary.value.trend.map(d => ({
      date: d.date,
      run_count: d.run_count,
      eval_pass_rate: d.eval_pass_rate,
      token_spend_usd: d.token_spend_usd,
    }))
  }
  const items: Array<{ date: string; run_count: number; eval_pass_rate: number | null; token_spend_usd: number }> = []
  const evalMap = new Map(tr.eval_pass_rates.map(r => [r.date, r.pass_rate]))
  const spendMap = new Map(tr.token_spend.map(r => [r.date, r.total_spend_usd]))
  // Build a combined series covering all dates
  for (const entry of tr.run_counts) {
    items.push({
      date: entry.date,
      run_count: entry.run_count,
      eval_pass_rate: evalMap.get(entry.date) ?? null,
      token_spend_usd: spendMap.get(entry.date) ?? 0,
    })
  }
  return items
})

function switchTrendDuration(days: number) {
  trendDuration.value = days
  dashboardStore.fetchTrends(days)
}

const trendRunCounts = computed(() => trendData.value.map(d => d.run_count))
const trendEvalRates = computed(() => trendData.value.map(d => d.eval_pass_rate ?? 0))
const trendSpendData = computed(() => trendData.value.map(d => d.token_spend_usd))

function statusBadgeClass(status: string): string {
  const map: Record<string, string> = {
    complete: 'bg-success/10 text-success',
    failed: 'bg-destructive/10 text-destructive',
    running: 'bg-primary/10 text-primary',
    pending: 'bg-muted text-muted-foreground',
    awaiting_human: 'bg-warning/10 text-warning',
    cancelled: 'bg-muted text-muted-foreground',
    eval_failed: 'bg-destructive/10 text-destructive',
    claimed: 'bg-warning/10 text-warning',
    waiting_for_lock: 'bg-muted text-muted-foreground',
  }
  return map[status] ?? 'bg-muted text-muted-foreground'
}

function formatTimestamp(iso: string): string {
  try {
    const d = new Date(iso)
    return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
  } catch {
    return iso
  }
}

onMounted(async () => {
  const promises: Promise<unknown>[] = [dashboardStore.fetchSummary()];
  if (!planStore.currentTier || planStore.currentTier === 'community') {
    promises.push(planStore.fetchPlan());
  }
  await Promise.all(promises);
})
</script>
