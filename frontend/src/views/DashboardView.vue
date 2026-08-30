<template>
  <div class="page-wide">
    <DashboardNotificationsPanel class="-mt-3 mb-4" />
    <PageHeader :title="$t('views.DashboardView.dashboard')" :subtitle="$t('views.DashboardView.overview_of_your_organisations_pipelines_and_runs')" data-testid="dashboard-title">
      <template #right>
        <div class="flex flex-wrap justify-end gap-1">
          <button type="button"
            v-for="w in trendWindows"
            :key="w.value ?? 'all'"
            :data-testid="'trend-toggle-' + (w.value ?? 'all')"
            :aria-pressed="selectedWindow === w.value"
            :class="[
              'px-3 py-1 text-xs font-medium rounded transition-colors',
              selectedWindow === w.value ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground hover:bg-muted/80',
            ]"
            @click="selectWindow(w.value)"
          >
            {{ $t(w.labelKey) }}
          </button>
        </div>
      </template>
    </PageHeader>
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
      <!-- Row 1: Summary stat cards -->
      <div :class="['grid gap-4 sm:grid-cols-2 lg:grid-cols-4 transition-opacity duration-200', periodRefreshing ? 'opacity-60' : 'opacity-100']">
        <StatCard :label="$t('views.DashboardView.pipelines')" :value="cardValue(summary.period?.metrics?.active_pipelines?.current, summary.active_pipelines)" color="primary" to="/pipelines" :delta="periodMetrics?.active_pipelines ?? null" :no-baseline-label="selectedWindow != null ? $t('views.DashboardView.no_prior_data') : undefined">
          <template #icon><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></template>
        </StatCard>
        <StatCard :label="$t('views.DashboardView.total_runs')" :value="cardValue(summary.period?.metrics?.total_runs?.current, summary.total_runs)" color="primary" to="/runs" :delta="periodMetrics?.total_runs ?? null" :no-baseline-label="selectedWindow != null ? $t('views.DashboardView.no_prior_data') : undefined">
          <template #icon><line x1="12" y1="20" x2="12" y2="10"/><line x1="18" y1="20" x2="18" y2="4"/><line x1="6" y1="20" x2="6" y2="16"/></template>
        </StatCard>
        <StatCard :label="$t('views.DashboardView.running')" :value="cardValue(summary.period?.metrics?.run_counts_by_status?.running?.current, summary.run_counts_by_status?.running ?? 0)" color="success" :to="`/runs?status=${RUN_STATUS.RUNNING}`" :delta="periodMetrics?.run_counts_by_status?.running ?? null" :no-baseline-label="selectedWindow != null ? $t('views.DashboardView.no_prior_data') : undefined">
          <template #icon><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></template>
        </StatCard>
        <StatCard :label="$t('views.DashboardView.awaiting_human')" :value="cardValue(summary.period?.metrics?.run_counts_by_status?.awaiting_human?.current, summary.run_counts_by_status?.awaiting_human ?? 0)" color="warning" :to="`/runs?status=${RUN_STATUS.AWAITING_HUMAN}`" :delta="periodMetrics?.run_counts_by_status?.awaiting_human ?? null" :no-baseline-label="selectedWindow != null ? $t('views.DashboardView.no_prior_data') : undefined" :inverted="true">
          <template #icon><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></template>
        </StatCard>
      </div>
      <div class="grid gap-4 sm:grid-cols-2">
        <StatCard :label="$t('views.DashboardView.failed')" :value="cardValue(summary.period?.metrics?.run_counts_by_status?.failed?.current, summary.run_counts_by_status?.failed ?? 0)" color="destructive" :to="`/runs?status=${RUN_STATUS.FAILED}`" :delta="periodMetrics?.run_counts_by_status?.failed ?? null" :no-baseline-label="selectedWindow != null ? $t('views.DashboardView.no_prior_data') : undefined" :inverted="true">
          <template #icon><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></template>
        </StatCard>
        <StatCard :label="$t('views.DashboardView.idle')" :value="cardValue(summary.period?.metrics?.run_counts_by_status?.idle?.current, summary.run_counts_by_status?.idle ?? 0)" color="muted" to="/pipelines" :delta="periodMetrics?.run_counts_by_status?.idle ?? null" :no-baseline-label="selectedWindow != null ? $t('views.DashboardView.no_prior_data') : undefined" :inverted="true">
          <template #icon><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></template>
        </StatCard>
      </div>
      <!-- Eval pass rate + Token spend -->
      <div class="grid gap-4 sm:grid-cols-2">
        <!-- Eval pass rate card -->
        <router-link to="/eval-editor" class="card card-hover p-4 block" data-testid="dashboard-eval-rate">
          <p class="text-sm font-medium text-muted-foreground mb-2">{{ $t('views.DashboardView.eval_pass_rate') }}</p>
          <div v-if="summary.eval_pass_rate != null">
            <p class="text-2xl font-semibold tabular-nums">{{ evalRateDisplay }}</p>
            <div class="flex items-center gap-2 mt-1">
              <span v-if="evalPeriodDelta?.delta_pct != null" :class="evalDeltaClass" class="inline-flex items-center text-sm font-medium">
                <span aria-hidden="true">{{ evalDeltaArrow }}</span>{{ evalDeltaPctText }}
              </span>
              <span v-else :class="evalTrendClass" class="inline-flex items-center text-sm font-medium">
                <ChevronUp v-if="evalTrend === 'up'" class="h-3.5 w-3.5" aria-hidden="true" />
                <ChevronDown v-else class="h-3.5 w-3.5" aria-hidden="true" />
                {{ evalTrendLabel }}
              </span>
              <span class="text-xs text-muted-foreground">{{ summary.eval_pass_rate.total_evals }} {{ $t('views.DashboardView.total_evals') }}</span>
            </div>
            <Sparkline class="mt-2 h-12 w-full" :data="evalSparklineData" :labels="summaryTrendLabels" unit="%" color="hsl(var(--primary))" :show-y-axis="true" />
          </div>
          <div v-else class="flex items-center justify-center py-6 text-sm text-muted-foreground">{{ $t('views.DashboardView.no_eval_data_yet') }}</div>
        </router-link>
        <!-- Token spend card -->
        <router-link to="/admin/costs" class="card card-hover p-4 block" data-testid="dashboard-token-spend">
          <p class="text-sm font-medium text-muted-foreground mb-2">{{ $t('views.DashboardView.token_spend_7d') }}</p>
          <div class="flex items-baseline gap-2">
            <p class="text-2xl font-semibold tabular-nums">{{ formatMoney(totalSpend, currencyCode, 2) }}</p>
            <span v-if="spendDeltaPct != null" :class="spendDeltaClass" class="text-xs font-medium">
              <span aria-hidden="true">{{ spendDeltaArrow }}</span>{{ spendDeltaPctText }}
            </span>
          </div>
          <p class="text-xs text-muted-foreground mt-1">{{ spendTrackedDays }} {{ $t('views.DashboardView.days_tracked') }}</p>
          <Sparkline class="mt-2 h-12 w-full" :data="spendSparklineData" :labels="summaryTrendLabels" unit="$" color="hsl(var(--warning))" :show-y-axis="true" />
        </router-link>
      </div>
      <!-- Team breakdown (Team only) -->
      <div v-if="isTeam && summary.teams && summary.teams.length > 0" class="card p-4">
        <div class="flex items-center justify-between mb-4">
          <h2 class="text-base font-semibold">{{ $t('views.DashboardView.team_breakdown') }}</h2>
          <span class="text-xs font-medium text-muted-foreground bg-muted px-2 py-0.5 rounded">{{ $t('views.DashboardView.team') }}</span>
        </div>
        <table class="w-full text-sm">
          <thead>
            <tr class="border-b text-left text-muted-foreground">
              <th class="pb-2 font-medium">{{ $t('views.DashboardView.team') }}</th>
              <th class="pb-2 font-medium text-right">{{ $t('views.DashboardView.runs') }}</th>
              <th class="pb-2 font-medium text-right">{{ $t('views.DashboardView.running') }}</th>
              <th class="pb-2 font-medium text-right">{{ $t('views.DashboardView.failed') }}</th>
              <th class="pb-2 font-medium text-right">{{ $t('views.DashboardView.eval_pass') }}</th>
              <th class="pb-2 font-medium text-right w-8"></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="team in summary.teams" :key="team.id"
                role="button"
                tabindex="0"
                :data-testid="'dashboard-team-row-' + team.id"
                class="border-b last:border-0 cursor-pointer hover:bg-muted/50"
                @click="toggleTeam(team.id)"
                @keydown.enter="toggleTeam(team.id)"
                @keydown.space.prevent="toggleTeam(team.id)">
              <td class="py-2.5 font-medium">{{ team.name }}</td>
              <td class="py-2.5 text-right">{{ team.total_runs }}</td>
              <td class="py-2.5 text-right text-success">{{ team.run_counts_by_status?.running ?? 0 }}</td>
              <td class="py-2.5 text-right text-destructive">{{ team.run_counts_by_status?.failed ?? 0 }}</td>
              <td class="py-2.5 text-right">{{ team.eval_pass_rate ? team.eval_pass_rate.pass_rate + '%' : '—' }}</td>
              <td class="py-2.5 text-right">
                <ChevronUp v-if="expandedTeam === team.id" class="h-3.5 w-3.5" aria-hidden="true" />
                <ChevronDown v-else class="h-3.5 w-3.5" aria-hidden="true" />
              </td>
            </tr>
            <tr v-if="expandedTeam && expandedTeamData">
              <td colspan="6" class="py-3 pl-6">
                <div class="text-xs text-muted-foreground space-y-1">
                  <p>{{ $t('views.DashboardView.pipelines') }}: <span class="font-medium text-foreground">{{ expandedTeamData.active_pipelines }}</span></p>
                  <p>{{ $t('views.DashboardView.awaiting_human') }}: <span class="font-medium text-foreground">{{ expandedTeamData.run_counts_by_status.awaiting_human }}</span></p>
                  <p>{{ $t('views.DashboardView.idle') }}: <span class="font-medium text-foreground">{{ expandedTeamData.run_counts_by_status.idle }}</span></p>
                  <p v-if="expandedTeamData.eval_pass_rate">
                    {{ $t('views.DashboardView.evals') }}: <span class="font-medium text-foreground">{{ expandedTeamData.eval_pass_rate.passed_evals }} / {{ expandedTeamData.eval_pass_rate.total_evals }} {{ $t('views.DashboardView.passed') }}</span>
                  </p>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <!-- Trend chart section -->
      <div v-if="planStore.featureEnabled('dashboard_charts')" class="card p-4">
        <div class="flex items-center justify-between mb-4">
          <h2 class="text-base font-semibold">{{ $t('views.DashboardView.run_activity') }}</h2>
        </div>
        <div v-if="trendData.length > 1" class="space-y-4">
          <div>
            <p class="text-xs font-medium text-muted-foreground mb-1">{{ $t('views.DashboardView.run_count') }}</p>
            <Sparkline class="h-16 w-full" :data="trendRunCounts" :labels="trendLabels" :unit="$t('views.DashboardView.runs')" color="hsl(var(--primary))" :show-y-axis="true" :show-x-ticks="true" />
          </div>
          <div>
            <p class="text-xs font-medium text-muted-foreground mb-1">{{ $t('views.DashboardView.eval_pass_rate_label') }}</p>
            <Sparkline class="h-16 w-full" :data="trendEvalRates" :labels="trendLabels" unit="%" color="hsl(var(--success))" :show-y-axis="true" :show-x-ticks="true" />
          </div>
          <div>
            <p class="text-xs font-medium text-muted-foreground mb-1">{{ $t('views.DashboardView.token_spend') }}</p>
            <Sparkline class="h-16 w-full" :data="trendSpendData" :labels="trendLabels" unit="$" color="hsl(var(--warning))" :show-y-axis="true" :show-x-ticks="true" />
          </div>
        </div>
        <div v-else class="flex items-center justify-center py-12">
          <p class="text-sm italic text-muted-foreground">{{ $t('views.DashboardView.no_data_trends') }}</p>
        </div>
      </div>
      <!-- Recent runs list -->
      <div class="card p-4">
        <h2 class="text-base font-semibold mb-4">{{ $t('views.DashboardView.recent_runs') }}</h2>
        <div v-if="summary.recent_runs && summary.recent_runs.length > 0" class="divide-y">
          <router-link v-for="run in summary.recent_runs" :key="run.id" :to="'/runs/' + run.id" class="flex items-center justify-between py-2.5 first:pt-0 last:pb-0" :data-testid="'dashboard-recent-run-' + run.id">
            <div class="min-w-0 flex-1">
              <p class="text-sm font-medium truncate" v-tooltip.top="run.pipeline_name">{{ run.pipeline_name }}</p>
              <p class="text-xs text-muted-foreground">{{ formatRunDate(run.created_at) }}</p>
            </div>
            <div class="flex items-center gap-2 ml-3">
              <span :class="runStatusBadgeClass(run.status)" class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium capitalize">
                {{ runStatusLabel(run.status) }}
              </span>
              <span class="text-xs text-muted-foreground hidden sm:inline">{{ run.trigger_type }}</span>
            </div>
          </router-link>
        </div>
        <div v-else class="flex items-center justify-center py-6 text-sm text-muted-foreground">
          {{ $t('views.DashboardView.no_runs_yet') }}
        </div>
      </div>
    </template>
  </div>
</template>
<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import PageHeader from '../components/shared/PageHeader.vue'
import DashboardNotificationsPanel from '../components/DashboardNotificationsPanel.vue'
import { usePlanStore } from '../stores/planStore'
import { useDashboardStore } from '../stores/dashboard'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import Sparkline from '../components/shared/Sparkline.vue'
import StatCard from '../components/StatCard.vue'
import { ChevronUp, ChevronDown } from '@lucide/vue'
import { RUN_STATUS } from '../constants/filters'
import { formatMoney } from '../lib/money'
import { runStatusBadgeClass, runStatusLabel, formatRunDate } from '../utils/runUtils'
import { useOrgCurrency } from '../composables/useOrgCurrency'

const { t } = useI18n()

const { currencyCode, loadCurrency } = useOrgCurrency()

const planStore = usePlanStore()
const dashboardStore = useDashboardStore()

const loading = computed(() => dashboardStore.loading)
const error = computed(() => dashboardStore.error)
const summary = computed(() => dashboardStore.summary)
const totalSpend = computed(() => dashboardStore.totalSpend)
const periodRefreshing = computed(() => dashboardStore.periodRefreshing)

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
  if (evalTrend.value === 'up') return t('views.DashboardView.improving')
  if (evalTrend.value === 'down') return t('views.DashboardView.declining')
  return t('views.DashboardView.stable')
})

const spendSparklineData = computed(() => {
  if (!summary.value?.trend) return []
  return summary.value.trend.map(d => d.token_spend_usd)
})

const summaryTrendLabels = computed(() => summary.value?.trend?.map(d => d.date) ?? [])

// --- Rolling-window toggle (FAR-92 / FAR-115) ---
const trendWindows: Array<{ labelKey: string; value: number | null }> = [
  { labelKey: 'views.DashboardView.trend_24h', value: 1 },
  { labelKey: 'views.DashboardView.trend_3d', value: 3 },
  { labelKey: 'views.DashboardView.trend_7d', value: 7 },
  { labelKey: 'views.DashboardView.trend_30d', value: 30 },
  { labelKey: 'views.DashboardView.trend_90d', value: 90 },
  { labelKey: 'views.DashboardView.trend_all_time', value: null },
]

const TREND_WINDOW_STORAGE_KEY = 'modulo.dashboard.trendWindow'
const TREND_WINDOW_ALLOWED = new Set([1, 3, 7, 30, 90])

function loadTrendWindow(): number | null {
  try {
    const raw = localStorage.getItem(TREND_WINDOW_STORAGE_KEY)
    if (raw === 'all') return null
    const parsed = Number(raw)
    if (TREND_WINDOW_ALLOWED.has(parsed)) return parsed
  } catch {
    // localStorage unavailable (private mode / SSR) — fall through to default.
  }
  return 3
}

function persistTrendWindow(days: number | null) {
  try {
    localStorage.setItem(TREND_WINDOW_STORAGE_KEY, days == null ? 'all' : String(days))
  } catch {
    // localStorage unavailable — persistence is best-effort.
  }
}

const selectedWindow = ref<number | null>(null)

function selectWindow(days: number | null) {
  selectedWindow.value = days
  persistTrendWindow(days)
  if (days == null) {
    // All-time: full reload is fine here — the all-time scalars aren't part
    // of the period block.
    void dashboardStore.fetchSummary()
  } else {
    // Period windows refresh ONLY the period block — no full-page skeleton.
    void dashboardStore.fetchPeriodMetrics(days)
  }
}

// Bug A: `??` only falls back on null/undefined, so a period window with zero
// runs shows 0 instead of the all-time figure. Show the period current only
// when it's a meaningful number (non-zero, or the all-time figure is also
// zero/absent); otherwise fall back to the all-time value.
function cardValue(periodCurrent: number | null | undefined, allTime: number | undefined): number {
  if (typeof periodCurrent === 'number') {
    const allTimeVal = allTime ?? 0
    if (periodCurrent !== 0 || allTimeVal === 0) return periodCurrent
  }
  return allTime ?? 0
}

// --- Period-scoped metrics (same-source/same-window value AND arrow) ---
const periodMetrics = computed(() => summary.value?.period?.metrics ?? null)

const evalPeriodDelta = computed(() => periodMetrics.value?.eval_pass_rate ?? null)
const evalRateDisplay = computed(() => {
  const period = evalPeriodDelta.value
  if (selectedWindow.value != null && period) {
    const current = period.current
    return current == null ? '—' : `${current}%`
  }
  const allTime = summary.value?.eval_pass_rate?.overall_pass_rate
  return allTime == null ? '—' : `${allTime}%`
})
const evalDeltaDirection = computed(() => deltaDirection(evalPeriodDelta.value?.delta_pct ?? null))
const evalDeltaArrow = computed(() => {
  if (evalDeltaDirection.value === 'up') return '▲'
  if (evalDeltaDirection.value === 'down') return '▼'
  return '→'
})
const evalDeltaClass = computed(() => {
  if (evalDeltaDirection.value === 'up') return 'text-success'
  if (evalDeltaDirection.value === 'down') return 'text-destructive'
  return 'text-muted-foreground'
})
const evalDeltaPctText = computed(() => {
  const d = evalPeriodDelta.value?.delta_pct
  return typeof d === 'number' && Number.isFinite(d) ? `${Math.abs(d).toFixed(1)}%` : ''
})

const spendDeltaPct = computed(() => {
  const d = summary.value?.period?.metrics?.spend?.delta_pct
  return typeof d === 'number' && Number.isFinite(d) ? d : null
})
const spendDeltaDirection = computed(() => deltaDirection(spendDeltaPct.value))
const spendDeltaArrow = computed(() => {
  if (spendDeltaDirection.value === 'up') return '▲'
  if (spendDeltaDirection.value === 'down') return '▼'
  return '→'
})
const spendDeltaClass = computed(() => {
  if (spendDeltaDirection.value === 'up') return 'text-success'
  if (spendDeltaDirection.value === 'down') return 'text-destructive'
  return 'text-muted-foreground'
})
const spendDeltaPctText = computed(() => {
  const d = spendDeltaPct.value
  return d == null ? '' : `${Math.abs(d).toFixed(1)}%`
})
const spendTrackedDays = computed(() => summary.value?.period?.days ?? summary.value?.trend?.length ?? 0)

function deltaDirection(deltaPct: number | null): 'up' | 'down' | 'flat' | null {
  if (deltaPct == null) return null
  if (deltaPct > 0) return 'up'
  if (deltaPct < 0) return 'down'
  return 'flat'
}

const trendsRaw = computed(() => dashboardStore.trends)

const trendData = computed(() => {
  const tr = trendsRaw.value
  if (!tr) return []
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

const trendRunCounts = computed(() => trendData.value.map(d => d.run_count))
const trendEvalRates = computed(() => trendData.value.map(d => d.eval_pass_rate ?? 0))
const trendSpendData = computed(() => trendData.value.map(d => d.token_spend_usd))
const trendLabels = computed(() => trendData.value.map(d => d.date))

onMounted(async () => {
  // Restore the persisted window (FAR-115); default to 3d when nothing is stored.
  selectedWindow.value = loadTrendWindow();
  const promises: Promise<unknown>[] = [
    dashboardStore.fetchSummary(selectedWindow.value ?? undefined),
    dashboardStore.fetchTrends(7),
    loadCurrency(),
  ];
  if (!planStore.currentTier || planStore.currentTier === 'community') {
    promises.push(planStore.fetchPlan());
  }
  await Promise.all(promises);
})
</script>
