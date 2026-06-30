<template>
  <div class="mx-auto max-w-6xl space-y-6 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">Team Comparison</h1>
      <p class="mt-1 text-muted-foreground">Side-by-side eval pass rates and pipeline metrics across teams</p>
    </header>

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadData" />

    <template v-else-if="data">
      <!-- Org-wide summary cards -->
      <div class="grid gap-4 sm:grid-cols-4">
        <div class="rounded-lg border bg-card p-4 text-card-foreground shadow-sm">
          <p class="text-sm font-medium text-muted-foreground">Total Runs</p>
          <p class="mt-1 text-3xl font-bold">{{ data.summary.total_runs }}</p>
        </div>
        <div class="rounded-lg border bg-card p-4 text-card-foreground shadow-sm">
          <p class="text-sm font-medium text-muted-foreground">Active Pipelines</p>
          <p class="mt-1 text-3xl font-bold">{{ data.summary.active_pipelines }}</p>
        </div>
        <div class="rounded-lg border bg-card p-4 text-card-foreground shadow-sm">
          <p class="text-sm font-medium text-muted-foreground">Eval Pass Rate</p>
          <p class="mt-1 text-3xl font-bold" :class="passRateClass(data.orgEvalPassRate)">
            {{ data.orgEvalPassRate != null ? `${data.orgEvalPassRate}%` : '—' }}
          </p>
        </div>
        <div class="rounded-lg border bg-card p-4 text-card-foreground shadow-sm">
          <p class="text-sm font-medium text-muted-foreground">Teams</p>
          <p class="mt-1 text-3xl font-bold">{{ data.teams.length }}</p>
        </div>
      </div>

      <!-- Team comparison table -->
      <div class="overflow-hidden rounded-lg border bg-card shadow-sm">
        <table class="w-full">
          <thead>
            <tr class="border-b bg-muted/50 text-left text-xs font-medium uppercase text-muted-foreground">
              <th class="px-4 py-3">Team</th>
              <th class="px-4 py-3">Members</th>
              <th class="px-4 py-3">Total Runs</th>
              <th class="px-4 py-3">Active Pipelines</th>
              <th class="px-4 py-3">Eval Pass Rate</th>
              <th class="px-4 py-3">Run Status</th>
              <th class="w-8 px-4 py-3" />
            </tr>
          </thead>
          <tbody class="divide-y">
            <tr
              v-for="team in data.teams"
              :key="team.id"
              :data-testid="`team-comparison-team-row-${team.id}`"
              class="cursor-pointer transition-colors hover:bg-muted/30"
              @click="toggleExpand(team.id)"
            >
              <td class="px-4 py-3 text-sm font-medium">{{ team.name }}</td>
              <td class="px-4 py-3 text-sm text-muted-foreground">{{ team.memberCount }}</td>
              <td class="px-4 py-3 text-sm">{{ team.totalRuns }}</td>
              <td class="px-4 py-3 text-sm">{{ team.activePipelines }}</td>
              <td class="px-4 py-3">
                <div v-if="team.avgPassRate != null" class="flex items-center gap-2">
                  <div class="h-2 w-24 overflow-hidden rounded-full bg-muted">
                    <div
                      class="h-full rounded-full transition-all"
                      :class="passRateBarClass(team.avgPassRate)"
                      :style="{ width: `${Math.min(team.avgPassRate, 100)}%` }"
                    />
                  </div>
                  <span class="text-xs font-medium tabular-nums" :class="passRateClass(team.avgPassRate)">
                    {{ team.avgPassRate }}%
                  </span>
                </div>
                <span v-else class="text-xs text-muted-foreground">—</span>
              </td>
              <td class="px-4 py-3">
                <div class="flex gap-1.5 text-xs">
                  <span class="badge badge-status-primary" title="Running">{{ team.runCounts.running }}</span>
                  <span class="badge badge-status-warning" title="Awaiting">{{ team.runCounts.awaiting_human }}</span>
                  <span class="badge badge-status-destructive" title="Failed">{{ team.runCounts.failed }}</span>
                  <span class="badge badge-status-muted" title="Idle">{{ team.runCounts.idle }}</span>
                </div>
              </td>
              <td class="px-4 py-3 text-xs text-muted-foreground">
                <svg
                  class="h-4 w-4 transition-transform"
                  :class="{ 'rotate-180': expandedTeamId === team.id }"
                  xmlns="http://www.w3.org/2000/svg"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2"
                >
                  <path d="m6 9 6 6 6-6" />
                </svg>
              </td>
            </tr>
            <!-- Expanded drill-down row -->
            <tr v-if="expandedTeamId">
              <td colspan="7" class="border-t bg-muted p-4">
                <div class="space-y-4">
                  <div class="flex items-center justify-between">
                    <h3 class="text-lg font-semibold">{{ expandedTeam?.name }} — Pipeline Eval Breakdown</h3>
                    <span class="text-sm text-muted-foreground">
                      {{ pipelineEvals.length }} pipeline{{ pipelineEvals.length === 1 ? '' : 's' }}
                    </span>
                  </div>

                  <div v-if="pipelineEvals.length === 0" class="rounded-lg border bg-background p-6 text-center text-sm text-muted-foreground">
                    No eval data available for this team's pipelines.
                  </div>

                  <div v-else class="space-y-2">
                    <div
                      v-for="pe in pipelineEvals"
                      :key="pe.pipelineId"
                      class="rounded-lg border bg-background p-4"
                    >
                      <div class="flex items-center justify-between">
                        <div class="min-w-0 flex-1">
                          <p class="text-sm font-medium truncate">{{ pe.pipelineName }}</p>
                          <p class="text-xs text-muted-foreground">
                            {{ pe.totalEvals }} eval{{ pe.totalEvals === 1 ? '' : 's' }}
                            · {{ pe.passedEvals }} passed
                          </p>
                        </div>
                        <div class="ml-4 flex items-center gap-3">
                          <div class="h-2 w-20 overflow-hidden rounded-full bg-muted">
                            <div
                              class="h-full rounded-full transition-all"
                              :class="passRateBarClass(pe.passRate)"
                              :style="{ width: `${Math.min(pe.passRate, 100)}%` }"
                            />
                          </div>
                          <span class="text-sm font-medium tabular-nums" :class="passRateClass(pe.passRate)">
                            {{ pe.passRate }}%
                          </span>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <p v-if="data.teams.length === 0" class="rounded-lg border bg-card p-8 text-center text-sm text-muted-foreground">
        No teams found. Create teams in Settings to see comparison data.
      </p>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { api } from '../lib/api/client'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { formatError } from '../lib/utils'

interface TeamRunStatus {
  running: number
  awaiting_human: number
  failed: number
  idle: number
}

interface TeamInfo {
  id: string
  name: string
  totalRuns: number
  activePipelines: number
  runCounts: TeamRunStatus
  memberCount: number
  avgPassRate: number | null
}

interface PipelineEval {
  pipelineId: string
  pipelineName: string
  totalEvals: number
  passedEvals: number
  passRate: number
}

interface ViewData {
  summary: {
    total_runs: number
    active_pipelines: number
  }
  teams: TeamInfo[]
  orgEvalPassRate: number | null
}

const loading = ref(true)
const error = ref<string | null>(null)
const data = ref<ViewData | null>(null)
const expandedTeamId = ref<string | null>(null)

const pipelineEvals = ref<PipelineEval[]>([])
const expandedTeam = ref<TeamInfo | null>(null)

function passRateClass(rate: number | null): string {
  if (rate == null) return ''
  if (rate >= 80) return 'text-success'
  if (rate >= 50) return 'text-warning'
  return 'text-destructive'
}

function passRateBarClass(rate: number | null): string {
  if (rate == null) return 'bg-muted-foreground/30'
  if (rate >= 80) return 'bg-success'
  if (rate >= 50) return 'bg-warning'
  return 'bg-destructive'
}

function buildPipelineEvals(
  perPipeline: Record<string, { total_evals: number; passed_evals: number; pass_rate: number }> | undefined,
  pipelineNames: Map<string, string>,
): PipelineEval[] {
  if (!perPipeline) return []
  return Object.entries(perPipeline).map(([pipelineId, evalData]) => ({
    pipelineId,
    pipelineName: pipelineNames.get(pipelineId) ?? pipelineId.slice(0, 8),
    totalEvals: evalData.total_evals,
    passedEvals: evalData.passed_evals,
    passRate: evalData.pass_rate,
  })).sort((a, b) => b.passRate - a.passRate)
}

async function loadData() {
  loading.value = true
  error.value = null
  expandedTeamId.value = null

  try {
    const [{ data: summaryResult, error: summaryErr }, { data: teamsResult, error: teamsErr }] = await Promise.all([
      api.GET('/api/v1/dashboard/summary'),
      api.GET('/api/v1/admin/teams', { params: { query: { page_size: 100 } as any } }),
    ])

    if (summaryErr) {
      error.value = `Failed to load dashboard: ${formatError(summaryErr)}`
      return
    }
    if (teamsErr) {
      error.value = `Failed to load teams: ${formatError(teamsErr)}`
      return
    }

    const s = summaryResult as unknown as {
      total_runs: number
      active_pipelines: number
      run_counts_by_status: TeamRunStatus
      teams: Array<{
        id: string
        name: string
        total_runs: number
        active_pipelines: number
        run_counts_by_status: TeamRunStatus
        eval_pass_rate?: {
          total_evals: number
          passed_evals: number
          pass_rate: number
        }
      }>
      eval_pass_rate: {
        overall_pass_rate: number | null
        total_evals: number
        passed_evals: number
        per_pipeline: Record<string, { total_evals: number; passed_evals: number; pass_rate: number }> | null
        per_team_pipeline?: Record<string, Record<string, { total_evals: number; passed_evals: number; pass_rate: number }>>
      } | null
    }

    const t = teamsResult as unknown as {
      items: Array<{ id: string; member_count: number }>
    }
    const memberCountMap = new Map(t.items.map(item => [item.id, item.member_count]))

    const teams: TeamInfo[] = (s.teams ?? []).map(team => ({
      id: team.id,
      name: team.name,
      totalRuns: team.total_runs,
      activePipelines: team.active_pipelines,
      runCounts: team.run_counts_by_status,
      memberCount: memberCountMap.get(team.id) ?? 0,
      avgPassRate: team.eval_pass_rate?.pass_rate ?? null,
    }))

    data.value = {
      summary: { total_runs: s.total_runs, active_pipelines: s.active_pipelines },
      teams,
      orgEvalPassRate: s.eval_pass_rate?.overall_pass_rate ?? null,
    }

    // Cache per-pipeline eval data for drill-down
    pipelineEvalCache.value = s.eval_pass_rate?.per_team_pipeline ?? {}
  } catch (e: unknown) {
    error.value = `Failed to load data: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
}

// Cache for per-pipeline eval data and pipeline names
const pipelineEvalCache = ref<Record<string, Record<string, { total_evals: number; passed_evals: number; pass_rate: number }>>>({})
const pipelineNames = ref<Map<string, string>>(new Map())

async function toggleExpand(teamId: string) {
  if (expandedTeamId.value === teamId) {
    expandedTeamId.value = null
    expandedTeam.value = null
    pipelineEvals.value = []
    return
  }

  expandedTeamId.value = teamId
  expandedTeam.value = data.value?.teams.find(t => t.id === teamId) ?? null

  // Build pipeline-level breakdown from cached eval data
  const teamPipelineData = pipelineEvalCache.value[teamId] ?? {}
  pipelineEvals.value = buildPipelineEvals(teamPipelineData, pipelineNames.value)

  // Lazy-fetch pipeline names if we have eval data but no names yet
  if (Object.keys(pipelineEvalCache.value).length > 0 && pipelineNames.value.size === 0) {
    try {
      const { data: pipelinesResult } = await api.GET('/api/v1/pipelines', {
        params: { query: { page_size: 100 } as any },
      })
      const pr = pipelinesResult as unknown as { items: Array<{ id: string; name: string }> } | undefined
      if (pr?.items) {
        const map = new Map(pr.items.map(p => [p.id, p.name]))
        pipelineNames.value = map
        const teamPipelineData = pipelineEvalCache.value[teamId] ?? {}
        pipelineEvals.value = buildPipelineEvals(teamPipelineData, map)
      }
    } catch {
      // Silently fail — pipeline IDs shown as fallback
    }
  }
}

onMounted(() => loadData())
</script>
