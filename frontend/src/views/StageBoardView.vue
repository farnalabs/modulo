<template>
  <FeatureGate feature-name="team_rbac" required-tier="team" show-disabled>

    <div class="mx-auto space-y-6 p-6">
    <header class="flex flex-wrap items-center justify-between gap-4">
      <div>
        <h1 class="text-3xl font-bold tracking-tight">Stage Board</h1>
        <p class="mt-1 text-muted-foreground">Organise pipelines into stages — track progress as pipelines move through development, testing, and production phases. Drag pipelines between stages to update their lifecycle status.</p>
      </div>
      <button
        data-testid="stage-board-create-btn"
        class="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
        @click="showCreateDialog = true"
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
        New Stage
      </button>
    </header>

    <div v-if="loading" class="flex items-center justify-center py-20">
      <div class="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
    </div>

    <div v-else-if="pageError" class="flex items-center justify-center py-20">
      <div class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive">{{ pageError }}</div>
    </div>

    <template v-else>
      <div class="flex flex-wrap items-center gap-4">
        <div class="flex items-center gap-2">
          <label class="text-sm font-medium text-muted-foreground">Team</label>
          <select
            v-model="teamFilter"
            data-testid="stage-board-team-filter"
            class="rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            @change="applyFilters"
          >
            <option value="">All Teams</option>
            <option v-for="t in teams" :key="t.id" :value="t.id">{{ t.name }}</option>
          </select>
        </div>

        <div class="flex items-center gap-2">
          <label class="text-sm font-medium text-muted-foreground">Status</label>
          <select
            v-model="statusFilter"
            data-testid="stage-board-status-filter"
            class="rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            @change="applyFilters"
          >
            <option value="">All Statuses</option>
            <option value="running">Running</option>
            <option value="idle">Idle</option>
            <option value="failed">Failed</option>
            <option value="complete">Complete</option>
            <option value="awaiting_human">Awaiting Human</option>
          </select>
        </div>

        <div class="flex items-center gap-2">
          <label class="text-sm font-medium text-muted-foreground">From</label>
          <input
            v-model="dateFrom"
            type="date"
            placeholder="YYYY-MM-DD"
            data-testid="stage-board-date-from"
            class="rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            @change="applyFilters"
          />
        </div>

        <div class="flex items-center gap-2">
          <label class="text-sm font-medium text-muted-foreground">To</label>
          <input
            v-model="dateTo"
            type="date"
            placeholder="YYYY-MM-DD"
            data-testid="stage-board-date-to"
            class="rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            @change="applyFilters"
          />
        </div>
      </div>

      <div class="overflow-x-auto pb-4">
        <div class="flex gap-4" style="min-width: max-content">
          <div
            v-for="stage in filteredStages"
            :key="stage.id"
            class="w-72 shrink-0"
          >
            <div
              class="stage-column-header mb-3 cursor-pointer rounded-lg border bg-card p-3 transition-shadow hover:shadow-md"
              :data-testid="'stage-board-column-' + stage.id"
              role="button"
              tabindex="0"
              @click="selectedStageId = stage.id"
              @keydown.enter="selectedStageId = stage.id"
              @keydown.space.prevent="selectedStageId = stage.id"
            >
              <div class="flex items-center justify-between">
                <div class="flex items-center gap-2">
                  <span
                    class="inline-block h-2.5 w-2.5 rounded-full"
                    :class="stageStatusClass(stage)"
                  />
                  <h3 class="font-semibold">{{ stage.name }}</h3>
                </div>
                <span class="rounded-full bg-muted px-2 py-0.5 text-xs tabular-nums text-muted-foreground">
                  {{ pipelinesByStage(stage.id).length }}
                </span>
              </div>
              <p v-if="stage.description" class="mt-1 truncate text-xs text-muted-foreground">{{ stage.description }}</p>
            </div>

            <div class="space-y-2">
              <div
                v-for="pipeline in pipelinesByStage(stage.id)"
                :key="pipeline.id"
                :data-testid="'stage-board-card-' + pipeline.id"
                class="card card-hover cursor-pointer px-3 py-2.5"
                role="button"
                tabindex="0"
                @click="selectedPipeline = pipeline"
                @keydown.enter="selectedPipeline = pipeline"
                @keydown.space.prevent="selectedPipeline = pipeline"
              >
                <div class="flex items-center justify-between">
                  <span class="text-sm font-medium truncate flex-1">{{ pipeline.name }}</span>
                  <span
                    class="ml-2 inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium capitalize"
                    :class="statusBadgeClass(pipeline.status || 'idle')"
                  >
                    {{ pipeline.status || 'idle' }}
                  </span>
                </div>
                <div class="mt-1 flex items-center gap-3 text-xs text-muted-foreground">
                  <span>{{ pipeline.team_name || 'No team' }}</span>
                  <span v-if="pipeline.created_at">{{ formatDate(pipeline.created_at) }}</span>
                </div>
                <div class="mt-1.5 flex justify-end gap-1">
                  <button
                    v-if="stage.position > 0"
                    data-testid="stage-board-move-left"
                    class="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-30"
                    title="Move to previous stage"
                    :disabled="movingPipelines[pipeline.id]"
                    @click.stop="movePipeline(pipeline, -1)"
                  >
                    <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 18 9 12 15 6"/></svg>
                  </button>
                  <button
                    v-if="stage.position < maxStagePosition"
                    data-testid="stage-board-move-right"
                    class="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-30"
                    title="Move to next stage"
                    :disabled="movingPipelines[pipeline.id]"
                    @click.stop="movePipeline(pipeline, 1)"
                  >
                    <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg>
                  </button>
                </div>
              </div>

              <div v-if="pipelinesByStage(stage.id).length === 0" class="rounded-lg border border-dashed bg-muted/30 px-3 py-6 text-center text-xs text-muted-foreground">
                No pipelines
              </div>
            </div>
          </div>
        </div>
      </div>
    </template>

    <div v-if="selectedStageId" class="fixed inset-0 z-50 flex items-start justify-end" @click.self="selectedStageId = null">
      <div class="h-full w-full max-w-md overflow-y-auto border-l bg-card p-6 shadow-lg">
        <div class="mb-6 flex items-center justify-between">
          <h2 class="text-lg font-semibold">Stage Details</h2>
          <button
            data-testid="stage-board-detail-close"
            class="rounded-lg p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground"
            @click="selectedStageId = null"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
          </button>
        </div>

        <template v-if="selectedStageDetail">
          <dl class="space-y-4 text-sm">
            <div>
              <dt class="text-muted-foreground">Name</dt>
              <dd class="font-medium">{{ selectedStageDetail.name }}</dd>
            </div>
            <div v-if="selectedStageDetail.description">
              <dt class="text-muted-foreground">Description</dt>
              <dd class="text-sm">{{ selectedStageDetail.description }}</dd>
            </div>
            <div>
              <dt class="text-muted-foreground">Position</dt>
              <dd>{{ selectedStageDetail.position }}</dd>
            </div>
            <div>
              <dt class="text-muted-foreground">Visibility</dt>
              <dd>
                <span
                  class="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium"
                  :class="selectedStageDetail.visibility === 'org' ? 'bg-primary/10 text-primary' : 'bg-warning/10 text-warning'"
                >
                  {{ selectedStageDetail.visibility === 'org' ? 'Org' : 'Team' }}
                </span>
              </dd>
            </div>
            <div>
              <dt class="text-muted-foreground">Connected Pipelines</dt>
              <dd>{{ stagePipelineCount(selectedStageDetail.id) }}</dd>
            </div>
            <div>
              <dt class="text-muted-foreground">Created</dt>
              <dd>{{ formatDate(selectedStageDetail.created_at) }}</dd>
            </div>
          </dl>

          <div class="mt-6 rounded-lg border bg-muted/30 p-4">
            <h4 class="mb-2 text-sm font-medium">Connected Pipelines</h4>
            <ul class="space-y-1.5">
              <li
                v-for="p in connectedPipelines"
                :key="p.id"
                class="truncate text-sm text-muted-foreground"
              >
                {{ p.name }}
              </li>
              <li v-if="connectedPipelines.length === 0" class="text-xs text-muted-foreground">No pipelines assigned</li>
            </ul>
          </div>
        </template>
      </div>
    </div>

    <div v-if="showCreateDialog" class="fixed inset-0 z-50 flex items-center justify-center bg-black/50" @click.self="showCreateDialog = false">
      <div class="w-full max-w-md rounded-lg border bg-card p-6 shadow-lg">
        <h3 class="mb-4 text-lg font-semibold">Create New Stage</h3>
        <div class="space-y-4">
          <div>
            <label class="mb-1 block text-sm font-medium">Name</label>
            <input
              v-model="createName"
              data-testid="stage-board-create-name"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              placeholder="e.g. Testing"
            />
          </div>
          <div>
            <label class="mb-1 block text-sm font-medium">Description</label>
            <textarea
              v-model="createDescription"
              data-testid="stage-board-create-description"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              rows="3"
              placeholder="Optional description"
            />
          </div>
          <div>
            <label class="mb-1 block text-sm font-medium">Position</label>
            <input
              v-model.number="createPosition"
              type="number"
              data-testid="stage-board-create-position"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              :min="0"
            />
          </div>
          <div>
            <label class="mb-1 block text-sm font-medium">Visibility</label>
            <select
              v-model="createVisibility"
              data-testid="stage-board-create-visibility"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <option value="org">Org</option>
              <option value="team">Team</option>
            </select>
          </div>

          <div v-if="createError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
            {{ createError }}
          </div>

          <div class="flex justify-end gap-2">
            <button
              data-testid="stage-board-create-cancel"
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm hover:bg-accent"
              @click="showCreateDialog = false"
            >
              Cancel
            </button>
            <button
              :disabled="!createName.trim()"
              data-testid="stage-board-create-submit"
              class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              @click="createStage"
            >
              Create
            </button>
          </div>
        </div>
      </div>
    </div>

    <div v-if="selectedPipeline" class="fixed inset-0 z-50 flex items-start justify-end" @click.self="selectedPipeline = null">
      <div class="h-full w-full max-w-md overflow-y-auto border-l bg-card p-6 shadow-lg">
        <div class="mb-6 flex items-center justify-between">
          <h2 class="text-lg font-semibold">Pipeline Details</h2>
          <button
            data-testid="stage-board-pipeline-close"
            class="rounded-lg p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground"
            @click="selectedPipeline = null"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
          </button>
        </div>
        <dl class="space-y-4 text-sm">
          <div>
            <dt class="text-muted-foreground">Name</dt>
            <dd class="font-medium">{{ selectedPipeline.name }}</dd>
          </div>
          <div v-if="selectedPipeline.description">
            <dt class="text-muted-foreground">Description</dt>
            <dd>{{ selectedPipeline.description }}</dd>
          </div>
          <div>
            <dt class="text-muted-foreground">Status</dt>
            <dd>
              <span
                class="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium capitalize"
                :class="statusBadgeClass(selectedPipeline.status || 'idle')"
              >
                {{ selectedPipeline.status || 'idle' }}
              </span>
            </dd>
          </div>
          <div>
            <dt class="text-muted-foreground">Stage</dt>
            <dd>{{ stageName(selectedPipeline.stage_id) || 'Unassigned' }}</dd>
          </div>
          <div>
            <dt class="text-muted-foreground">Created</dt>
            <dd>{{ formatDate(selectedPipeline.created_at) }}</dd>
          </div>
        </dl>
      </div>
    </div>
  </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useApi } from '../composables/useApi'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'

const planStore = usePlanStore()
const { get, post, patch } = useApi()

const loading = ref(true)
const pageError = ref<string | null>(null)

const stages = ref<any[]>([])
const allPipelines = ref<any[]>([])
const teams = ref<any[]>([])
const selectedStageId = ref<string | null>(null)
const selectedPipeline = ref<any | null>(null)

const teamFilter = ref('')
const statusFilter = ref('')
const dateFrom = ref('')
const dateTo = ref('')

const showCreateDialog = ref(false)
const createName = ref('')
const createDescription = ref('')
const createPosition = ref(0)
const createVisibility = ref('org')
const createError = ref<string | null>(null)

const maxStagePosition = computed(() => {
  if (stages.value.length === 0) return 0
  return Math.max(...stages.value.map(s => s.position))
})

const selectedStageDetail = computed(() => {
  if (!selectedStageId.value) return null
  return stages.value.find(s => s.id === selectedStageId.value) || null
})

const connectedPipelines = computed(() => {
  if (!selectedStageId.value) return []
  return allPipelines.value.filter(p => p.stage_id === selectedStageId.value)
})

function stageStatusClass(stage: any): string {
  const count = pipelinesByStage(stage.id).length
  if (count === 0) return 'bg-muted-foreground/30'
  const hasRunning = pipelinesByStage(stage.id).some(p => p.status === 'running' || p.status === 'awaiting_human')
  if (hasRunning) return 'bg-green-500'
  const hasFailed = pipelinesByStage(stage.id).some(p => p.status === 'failed')
  if (hasFailed) return 'bg-destructive'
  return 'bg-primary'
}

function statusBadgeClass(status: string): string {
  const map: Record<string, string> = {
    running: 'bg-blue-500/10 text-blue-600 dark:text-blue-400',
    idle: 'bg-muted text-muted-foreground',
    failed: 'bg-destructive/10 text-destructive',
    complete: 'bg-green-500/10 text-green-600 dark:text-green-400',
    awaiting_human: 'bg-warning/10 text-warning',
  }
  return map[status] || map.idle
}

function formatDate(dateStr: string | null | undefined): string {
  if (!dateStr) return '-'
  return new Date(dateStr).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
}

function stageName(stageId: string | null | undefined): string {
  if (!stageId) return ''
  const s = stages.value.find(st => st.id === stageId)
  return s ? s.name : ''
}

const filteredStages = computed(() => {
  if (!teamFilter.value) return stages.value
  return stages.value.filter(s => s.owner_team_id === teamFilter.value)
})

const filteredPipelines = computed(() => {
  return allPipelines.value.filter(p => {
    if (teamFilter.value && p.owner_team_id !== teamFilter.value) return false
    if (statusFilter.value && p.status !== statusFilter.value) return false
    if (dateFrom.value && p.created_at && new Date(p.created_at) < new Date(dateFrom.value)) return false
    if (dateTo.value && p.created_at && new Date(p.created_at) > new Date(dateTo.value + 'T23:59:59')) return false
    return true
  })
})

function pipelinesByStage(stageId: string): any[] {
  return filteredPipelines.value.filter(p => p.stage_id === stageId)
}

function stagePipelineCount(stageId: string): number {
  return allPipelines.value.filter(p => p.stage_id === stageId).length
}

function applyFilters() {
  // reactivity handles this via computed
}

async function loadStages() {
  try {
    const result = await get<any>('/api/v1/stages')
    stages.value = (result.items || []).sort((a: any, b: any) => a.position - b.position)
  } catch {
    // Will show empty state
  }
}

async function loadPipelines() {
  try {
    const result = await get<any>('/api/v1/pipelines')
    allPipelines.value = result.items || []
  } catch {
    // Will show empty state
  }
}

async function loadTeams() {
  try {
    const result = await get<any>('/api/v1/teams')
    teams.value = result.items || []
  } catch {
    // Will show empty state
  }
}

const movingPipelines = ref<Record<string, boolean>>({})

async function movePipeline(pipeline: any, direction: number) {
  const currentStage = stages.value.find(s => s.id === pipeline.stage_id)
  if (!currentStage) return
  const sortedStages = [...stages.value].sort((a, b) => a.position - b.position)
  const currentIdx = sortedStages.findIndex(s => s.id === currentStage.id)
  const targetIdx = currentIdx + direction
  if (targetIdx < 0 || targetIdx >= sortedStages.length) return
  const targetStage = sortedStages[targetIdx]
  const prevStageId = pipeline.stage_id
  movingPipelines.value[pipeline.id] = true
  try {
    await patch(`/api/v1/pipelines/${pipeline.id}`, { stage_id: targetStage.id })
    pipeline.stage_id = targetStage.id
  } catch {
    pipeline.stage_id = prevStageId
  } finally {
    movingPipelines.value[pipeline.id] = false
  }
}

async function createStage() {
  if (!createName.value.trim()) return
  createError.value = null
  try {
    await post('/api/v1/stages', {
      name: createName.value.trim(),
      description: createDescription.value.trim() || null,
      position: createPosition.value,
      visibility: createVisibility.value,
    })
    showCreateDialog.value = false
    createName.value = ''
    createDescription.value = ''
    createPosition.value = 0
    createVisibility.value = 'org'
    await loadStages()
  } catch (e: unknown) {
    createError.value = e instanceof Error ? e.message : String(e)
  }
}

onMounted(async () => {
  planStore.fetchPlan()
  try {
    await Promise.all([loadStages(), loadPipelines(), loadTeams()])
  } catch {
    pageError.value = 'Failed to load stage board data'
  } finally {
    loading.value = false
  }
})
</script>
