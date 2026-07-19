<template>
  <div class="mx-auto space-y-6 p-6">
  <header class="flex flex-wrap items-center justify-between gap-4">
    <PageHeader title="Stage Board" subtitle="Organise pipelines into stages — track progress as pipelines move through development, testing, and production phases. Drag pipelines between stages to update their lifecycle status." />
    <div class="flex items-center gap-2">
      <Button
        variant="default"
        data-testid="stage-board-create-btn"
        @click="showCreateDialog = true"
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
        New Stage
      </Button>
      <button
        data-testid="stage-board-reorder-btn"
        class="inline-flex items-center gap-2 rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
        @click="reorderMode = !reorderMode"
      >
        <svg v-if="!reorderMode" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>
        <svg v-else xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>
        {{ reorderMode ? 'Done' : 'Reorder' }}
      </button>
    </div>
  </header>
  <div v-if="loading" class="flex items-center justify-center py-20">
    <div class="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
  </div>
  <div v-else-if="pageError" class="flex items-center justify-center py-20">
    <div class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive">{{ pageError }}</div>
  </div>
  <template v-else>
    <div v-if="stages.length === 0" class="flex items-center justify-center py-20">
      <EmptyState title="No stages yet" description="Create stages to organise pipelines into development, testing, and production phases.">
        <Button variant="default" data-testid="stage-board-create-btn" @click="showCreateDialog = true">
          <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          New Stage
        </Button>
      </EmptyState>
    </div>
    <template v-else>
      <div class="flex flex-wrap items-center gap-4">
        <div class="flex items-center gap-2">
          <label for="stageboardview-field-8" class="text-sm font-medium text-muted-foreground">Team</label>
          <Select :model-value="teamFilter" @update:model-value="teamFilter = String($event); applyFilters()">
            <SelectTrigger class="w-auto min-w-[140px]" aria-label="Team" data-testid="stage-board-team-filter">
              <SelectValue placeholder="All Teams" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="__all__">All Teams</SelectItem>
              <SelectItem v-for="t in teams" :key="t.id" :value="t.id">{{ t.name }}</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div class="flex items-center gap-2">
          <label for="stageboardview-field-7" class="text-sm font-medium text-muted-foreground">Status</label>
          <Select :model-value="statusFilter" @update:model-value="statusFilter = String($event); applyFilters()">
            <SelectTrigger class="w-auto min-w-[140px]" aria-label="Status" data-testid="stage-board-status-filter">
              <SelectValue placeholder="All Statuses" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="__all__">All Statuses</SelectItem>
              <SelectItem :value="RUN_STATUS.RUNNING">Running</SelectItem>
              <SelectItem value="idle">Idle</SelectItem>
              <SelectItem :value="RUN_STATUS.FAILED">Failed</SelectItem>
              <SelectItem :value="RUN_STATUS.COMPLETE">Complete</SelectItem>
              <SelectItem :value="RUN_STATUS.AWAITING_HUMAN">Awaiting Human</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div class="flex items-center gap-2">
          <label for="stageboardview-field-6" class="text-sm font-medium text-muted-foreground">From</label>
          <input id="stageboardview-field-6"
            v-model="dateFrom"
            type="date"
            placeholder="YYYY-MM-DD"
            data-testid="stage-board-date-from"
            class="rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            @change="applyFilters"
          />
        </div>
        <div class="flex items-center gap-2">
          <label for="stageboardview-field-5" class="text-sm font-medium text-muted-foreground">To</label>
          <input id="stageboardview-field-5"
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
                <div class="flex items-center gap-2 min-w-0">
                  <span
                    class="inline-block h-2.5 w-2.5 shrink-0 rounded-full"
                    :class="stageStatusClass(stage)"
                  />
                  <template v-if="editingNameStageId === stage.id">
                    <input
                      v-model="editingNameValue"
                      class="w-full rounded border border-input bg-background px-1 py-0.5 text-sm font-semibold"
                      placeholder="Stage name"

                      @click.stop
                      @keydown.enter.prevent="saveEditingName"
                      @keydown.escape.prevent="cancelEditingName"
                      @blur="saveEditingName"
                    />
                  </template>
                  <h3
                    v-else
                    role="button"
                    tabindex="0"
                    class="truncate cursor-pointer font-semibold hover:text-primary"
                    @click.stop="startEditingName(stage)"
                    @keydown.enter.prevent="startEditingName(stage)"
                    @keydown.space.prevent="startEditingName(stage)"
                    :title="'Click to rename'"
                  >{{ stage.name }}</h3>
                </div>
                <div class="flex items-center gap-1 shrink-0">
                  <div v-if="reorderMode" class="flex flex-col gap-0.5">
                    <button
                      :data-testid="'stage-board-move-up-' + stage.id"
                      class="rounded p-0.5 text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-30"
                      :disabled="updatingStages[stage.id] || isFirstStage(stage)"
                      @click.stop="moveStage(stage, -1)"
                    >
                      <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="18 15 12 9 6 15"/></svg>
                    </button>
                    <button
                      :data-testid="'stage-board-move-down-' + stage.id"
                      class="rounded p-0.5 text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-30"
                      :disabled="updatingStages[stage.id] || isLastStage(stage)"
                      @click.stop="moveStage(stage, 1)"
                    >
                      <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
                    </button>
                  </div>
                  <span class="rounded-full bg-muted px-2 py-0.5 text-xs tabular-nums text-muted-foreground">
                    {{ pipelinesByStage(stage.id).length }}
                  </span>
                </div>
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
  </template>
  <div role="button" tabindex="0" @keydown.enter="($event.currentTarget as HTMLElement).click()" @keydown.space.prevent="($event.currentTarget as HTMLElement).click()" v-if="selectedStageId" class="fixed inset-0 z-50 flex items-start justify-end" @click.self="selectedStageId = null">
    <div class="h-full w-full max-w-md overflow-y-auto border-l bg-card p-6 shadow-lg">
      <div class="mb-6 flex items-center justify-between">
        <h2 class="text-base font-semibold">Stage Details</h2>
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
            <dd>
              <template v-if="editingNameStageId === selectedStageDetail.id">
                <input
                  v-model="editingNameValue"
                  class="w-full rounded border border-input bg-background px-2 py-1 text-sm font-medium"
                  placeholder="Stage name"

                  @keydown.enter.prevent="saveEditingName"
                  @keydown.escape.prevent="cancelEditingName"
                  @blur="saveEditingName"
                />
              </template>
              <span role="button" tabindex="0" @keydown.enter="($event.currentTarget as HTMLElement).click()" @keydown.space.prevent="($event.currentTarget as HTMLElement).click()"
                v-else
                class="cursor-pointer font-medium hover:text-primary"
                @click="startEditingName(selectedStageDetail)"
              >{{ selectedStageDetail.name }}</span>
            </dd>
          </div>
          <div v-if="selectedStageDetail.description">
            <dt class="text-muted-foreground">Description</dt>
            <dd class="text-sm">{{ selectedStageDetail.description }}</dd>
          </div>
          <div>
            <dt class="text-muted-foreground">Position</dt>
            <dd>
              <template v-if="editingPositionStageId === selectedStageDetail.id">
                <div class="flex items-center gap-2">
                  <input aria-label="number"
                    v-model.number="editingPositionValue"
                    type="number"
                    min="0"

                    class="w-20 rounded border border-input bg-background px-2 py-1 text-sm"
                    @keydown.enter.prevent="saveEditingPosition"
                    @keydown.escape.prevent="cancelEditingPosition"
                    @blur="saveEditingPosition"
                  />
                </div>
              </template>
              <span role="button" tabindex="0" @keydown.enter="($event.currentTarget as HTMLElement).click()" @keydown.space.prevent="($event.currentTarget as HTMLElement).click()"
                v-else
                class="cursor-pointer hover:text-primary"
                @click="startEditingPosition(selectedStageDetail)"
                :title="'Click to edit position'"
              >{{ selectedStageDetail.position }}</span>
            </dd>
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
  <div role="button" tabindex="0" @keydown.enter="($event.currentTarget as HTMLElement).click()" @keydown.space.prevent="($event.currentTarget as HTMLElement).click()" v-if="showCreateDialog" class="fixed inset-0 z-50 flex items-center justify-center bg-black/50" @click.self="showCreateDialog = false">
    <div class="w-full max-w-md rounded-lg border bg-card p-6 shadow-lg">
      <h3 class="mb-4 text-base font-semibold">Create New Stage</h3>
      <div class="space-y-4">
        <div>
          <label for="stageboardview-field-4" class="mb-1 block text-sm font-medium">Name</label>
          <input id="stageboardview-field-4"
            v-model="createName"
            data-testid="stage-board-create-name"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            placeholder="e.g. Testing"
          />
        </div>
        <div>
          <label for="stageboardview-field-3" class="mb-1 block text-sm font-medium">Description</label>
          <textarea id="stageboardview-field-3"
            v-model="createDescription"
            data-testid="stage-board-create-description"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            rows="3"
            placeholder="Optional description"
          />
        </div>
        <div>
          <label for="stageboardview-field-2" class="mb-1 block text-sm font-medium">Position</label>
          <input id="stageboardview-field-2"
            v-model.number="createPosition"
            type="number"
            data-testid="stage-board-create-position"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            :min="0"
          />
        </div>
        <div>
          <label for="stageboardview-field-1" class="mb-1 block text-sm font-medium">Visibility</label>
          <Select :model-value="createVisibility" @update:model-value="createVisibility = String($event)">
            <SelectTrigger class="w-full" aria-label="Visibility" data-testid="stage-board-create-visibility">
              <SelectValue placeholder="Select visibility" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="org">Org</SelectItem>
              <SelectItem value="team">Team</SelectItem>
            </SelectContent>
          </Select>
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
          <Button
            :disabled="!createName.trim()"
            variant="default"
            data-testid="stage-board-create-submit"
            @click="createStage"
          >
            Create
          </Button>
        </div>
      </div>
    </div>
  </div>
  <div role="button" tabindex="0" @keydown.enter="($event.currentTarget as HTMLElement).click()" @keydown.space.prevent="($event.currentTarget as HTMLElement).click()" v-if="selectedPipeline" class="fixed inset-0 z-50 flex items-start justify-end" @click.self="selectedPipeline = null">
    <div class="h-full w-full max-w-md overflow-y-auto border-l bg-card p-6 shadow-lg">
      <div class="mb-6 flex items-center justify-between">
        <h2 class="text-base font-semibold">Pipeline Details</h2>
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
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import PageHeader from '../components/shared/PageHeader.vue'
import { useDataFetch } from '../composables/useDataFetch'
import { formatApiError } from '../lib/api/formatError'
import { api } from '../lib/api/client'
import { formatDateShort } from '../lib/formatDate'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Button } from '@/components/ui/button'
import EmptyState from '../components/shared/EmptyState.vue'
import { RUN_STATUS } from '../constants/filters'

async function fetchWithTimeout<T>(factory: (signal: AbortSignal) => Promise<T>, ms = 15000): Promise<T> {
  const ctrl = new AbortController()
  const timeout = setTimeout(() => ctrl.abort(), ms)
  try {
    return await factory(ctrl.signal)
  } finally {
    clearTimeout(timeout)
  }
}

const selectedStageId = ref<string | null>(null)
const selectedPipeline = ref<any | null>(null)

const teamFilter = ref('__all__')
const statusFilter = ref('__all__')
const dateFrom = ref('')
const dateTo = ref('')

const showCreateDialog = ref(false)
const createName = ref('')
const createDescription = ref('')
const createPosition = ref(0)
const createVisibility = ref('org')
const createError = ref<string | null>(null)

const editingNameStageId = ref<string | null>(null)
const editingNameValue = ref('')
const reorderMode = ref(false)
const editingPositionStageId = ref<string | null>(null)
const editingPositionValue = ref(0)
const updatingStages = ref<Record<string, boolean>>({})

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
  const d = new Date(dateStr)
  if (isNaN(d.getTime())) return '-'
  return formatDateShort(d)
}

function stageName(stageId: string | null | undefined): string {
  if (!stageId) return ''
  const s = stages.value.find(st => st.id === stageId)
  return s ? s.name : ''
}

const filteredStages = computed(() => {
  if (teamFilter.value === '__all__') return stages.value
  return stages.value.filter(s => s.owner_team_id === teamFilter.value)
})

const filteredPipelines = computed(() => {
  return allPipelines.value.filter(p => {
    if (teamFilter.value !== '__all__' && p.owner_team_id !== teamFilter.value) return false
    if (statusFilter.value !== '__all__' && p.status !== statusFilter.value) return false
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

const stages = ref<any[]>([])
const allPipelines = ref<any[]>([])
const teams = ref<any[]>([])

const { loading, error: pageError } = useDataFetch(
  async () => {
    const [stagesResp, pipelinesResp, teamsResp] = await Promise.all([
      fetchWithTimeout((signal) => api.GET('/api/v1/stages', { signal })).catch(() => ({ data: null })),
      fetchWithTimeout((signal) => api.GET('/api/v1/pipelines', { signal })).catch(() => ({ data: null })),
      fetchWithTimeout((signal) => api.GET('/api/v1/teams', { signal })).catch(() => ({ data: null })),
    ])
    const stagesData = (stagesResp?.data as any)?.items ?? []
    const pipelinesData = (pipelinesResp?.data as any)?.items ?? []
    const teamsData = (teamsResp?.data as any)?.items ?? []
    stages.value = (stagesData as any[]).sort((a: any, b: any) => a.position - b.position)
    allPipelines.value = pipelinesData as any[]
    teams.value = teamsData as any[]
    if (stagesResp?.data === null && pipelinesResp?.data === null) {
      return { error: { detail: 'Failed to load board data. The server may be unavailable.' } }
    }
    return { data: {} }
  },
  { initialValue: {} },
)


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
    await fetchWithTimeout((signal) => api.PATCH('/api/v1/pipelines/{pipeline_id}', {
      params: { path: { pipeline_id: pipeline.id } },
      body: { stage_id: targetStage.id },
      signal,
    }))
    pipeline.stage_id = targetStage.id
  } catch (e) {
    pipeline.stage_id = prevStageId
    pageError.value = 'Failed to move pipeline: ' + formatApiError(e)
  } finally {
    movingPipelines.value[pipeline.id] = false
  }
}

async function createStage() {
  if (!createName.value.trim()) return
  createError.value = null
  try {
    await fetchWithTimeout((signal) => api.POST('/api/v1/stages', {
      body: {
        name: createName.value.trim(),
        description: createDescription.value.trim() || null,
        position: createPosition.value,
        visibility: createVisibility.value,
      },
      signal,
    }))
    showCreateDialog.value = false
    createName.value = ''
    createDescription.value = ''
    createPosition.value = 0
    createVisibility.value = 'org'
    await (async () => {
      const { data } = await api.GET('/api/v1/stages')
      stages.value = ((data as any)?.items ?? []).sort((a: any, b: any) => a.position - b.position)
    })()
  } catch (e: unknown) {
    createError.value = formatApiError(e)
  }
}

function isFirstStage(stage: any): boolean {
  const sorted = [...stages.value].sort((a, b) => a.position - b.position)
  return sorted.length > 0 && sorted[0].id === stage.id
}

function isLastStage(stage: any): boolean {
  const sorted = [...stages.value].sort((a, b) => a.position - b.position)
  return sorted.length > 0 && sorted[sorted.length - 1].id === stage.id
}

function startEditingName(stage: any) {
  editingNameStageId.value = stage.id
  editingNameValue.value = stage.name
}

async function saveEditingName() {
  if (!editingNameStageId.value || !editingNameValue.value.trim()) {
    editingNameStageId.value = null
    return
  }
  const stageId = editingNameStageId.value
  const name = editingNameValue.value.trim()
  editingNameStageId.value = null
  const existing = stages.value.find(s => s.id === stageId)
  if (existing && existing.name === name) return
  updatingStages.value[stageId] = true
  try {
    const { data } = await fetchWithTimeout((signal) => api.PATCH('/api/v1/stages/{stage_id}', {
      params: { path: { stage_id: stageId } },
      body: { name },
      signal,
    }))
    if (data) {
      const idx = stages.value.findIndex(s => s.id === stageId)
      if (idx !== -1) stages.value[idx] = data
    }
  } catch (e) {
    pageError.value = 'Failed to rename stage: ' + formatApiError(e)
  } finally {
    updatingStages.value[stageId] = false
  }
}

function cancelEditingName() {
  editingNameStageId.value = null
  editingNameValue.value = ''
}

function startEditingPosition(stage: any) {
  editingPositionStageId.value = stage.id
  editingPositionValue.value = stage.position
}

async function saveEditingPosition() {
  if (!editingPositionStageId.value) return
  const stageId = editingPositionStageId.value
  const position = editingPositionValue.value
  editingPositionStageId.value = null
  const existing = stages.value.find(s => s.id === stageId)
  if (existing && existing.position === position) return
  updatingStages.value[stageId] = true
  try {
    const { data } = await fetchWithTimeout((signal) => api.PATCH('/api/v1/stages/{stage_id}', {
      params: { path: { stage_id: stageId } },
      body: { position },
      signal,
    }))
    if (data) {
      const idx = stages.value.findIndex(s => s.id === stageId)
      if (idx !== -1) stages.value[idx] = data
    }
    stages.value.sort((a, b) => a.position - b.position)
  } catch (e) {
    pageError.value = 'Failed to update stage position: ' + formatApiError(e)
  } finally {
    updatingStages.value[stageId] = false
  }
}

function cancelEditingPosition() {
  editingPositionStageId.value = null
}

async function moveStage(stage: any, direction: number) {
  const sorted = [...stages.value].sort((a, b) => a.position - b.position)
  const idx = sorted.findIndex(s => s.id === stage.id)
  const targetIdx = idx + direction
  if (targetIdx < 0 || targetIdx >= sorted.length) return
  const target = sorted[targetIdx]
  const myPos = stage.position
  const targetPos = target.position

  stage.position = targetPos
  target.position = myPos
  stages.value.sort((a, b) => a.position - b.position)

  updatingStages.value[stage.id] = true
  try {
    await fetchWithTimeout((signal) => api.PATCH('/api/v1/stages/{stage_id}', {
      params: { path: { stage_id: stage.id } },
      body: { position: targetPos },
      signal,
    }))
    await fetchWithTimeout((signal) => api.PATCH('/api/v1/stages/{stage_id}', {
      params: { path: { stage_id: target.id } },
      body: { position: myPos },
      signal,
    }))
  } catch (e) {
    stage.position = myPos
    target.position = targetPos
    stages.value.sort((a, b) => a.position - b.position)
    pageError.value = 'Failed to reorder stages: ' + formatApiError(e)
  } finally {
    updatingStages.value[stage.id] = false
  }
}
</script>
