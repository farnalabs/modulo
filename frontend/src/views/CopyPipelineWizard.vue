<template>
  <div class="min-h-screen bg-background">
    <header class="bg-card border-b border-border px-6 py-4">
      <div class="max-w-3xl mx-auto">
        <button
          class="text-sm text-muted-foreground hover:text-foreground mb-2 inline-flex items-center gap-1"
          @click="onBack"
          data-testid="copy-wizard-back"
        >
          &larr; {{ step === 1 ? 'Back to Pipelines' : 'Back' }}
        </button>
        <h1 class="text-xl font-semibold text-foreground">Copy Pipeline</h1>
        <p class="text-sm text-muted-foreground mt-1">Duplicate an existing pipeline and adapt it for a new purpose</p>
      </div>
    </header>

    <main class="max-w-3xl mx-auto px-6 py-8">
      <div class="flex items-center gap-2 mb-8">
        <div
          v-for="(s, i) in steps"
          :key="i"
          class="flex items-center gap-2"
        >
          <div
            class="flex items-center justify-center w-8 h-8 rounded-full text-sm font-medium transition-colors"
            :class="step === i + 1 ? 'bg-primary text-primary-foreground' : step > i + 1 ? 'bg-success/20 text-success' : 'bg-muted text-muted-foreground'"
            data-testid="copy-wizard-step-indicator"
          >
            <svg v-if="step > i + 1" xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polyline points="20 6 9 17 4 12"/></svg>
            <span v-else>{{ i + 1 }}</span>
          </div>
          <span class="text-sm" :class="step === i + 1 ? 'text-foreground font-medium' : 'text-muted-foreground'">{{ s }}</span>
          <svg v-if="i < steps.length - 1" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1" class="text-muted-foreground/40"><polyline points="9 18 15 12 9 6"/></svg>
        </div>
      </div>

      <LoadingSpinner v-if="loading" />

      <ErrorAlert v-else-if="error" :message="error" :on-retry="retry" class="mb-6" />

      <template v-else-if="step === 1">
        <div class="card p-6">
          <h2 class="text-lg font-medium text-foreground mb-1">Select Source Pipeline</h2>
          <p class="text-sm text-muted-foreground mb-4">Choose the pipeline you want to copy and adapt.</p>

          <div class="relative mb-4">
            <input
              v-model="searchQuery"
              type="text"
              placeholder="Search pipelines by name..."
              class="w-full pl-9 pr-3 py-2 border border-input bg-background rounded-lg text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              data-testid="copy-wizard-search"
            />
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
          </div>

          <div class="flex gap-2 mb-4">
            <button
              v-for="f in visibilityFilters"
              :key="f.value"
              class="px-3 py-1.5 text-xs font-medium rounded-full border transition-colors"
              :class="visibilityFilter === f.value ? 'bg-primary text-primary-foreground border-primary' : 'border-input text-muted-foreground hover:bg-accent'"
              @click="visibilityFilter = f.value"
              data-testid="copy-wizard-visibility-filter"
            >
              {{ f.label }}
            </button>
          </div>

          <div v-if="filteredPipelines.length === 0" class="py-12 text-center text-sm text-muted-foreground">
            No pipelines match your search.
          </div>

          <div v-else class="space-y-2 max-h-96 overflow-y-auto">
            <button
              v-for="p in filteredPipelines"
              :key="p.id"
              class="w-full text-left p-3 rounded-lg border transition-colors"
              :class="selectedPipeline?.id === p.id ? 'border-primary bg-primary/5' : 'border-input hover:bg-accent'"
              @click="selectedPipeline = p"
              data-testid="copy-wizard-pipeline-option"
            >
              <div class="flex items-start justify-between gap-2">
                <div class="min-w-0">
                  <p class="text-sm font-medium text-foreground truncate">{{ p.name }}</p>
                  <p v-if="p.description" class="text-xs text-muted-foreground mt-0.5 line-clamp-2">{{ p.description }}</p>
                </div>
                <span
                  class="shrink-0 badge text-xs"
                  :class="p.visibility === 'org' ? 'badge-context-blue' : 'badge-context-purple'"
                >
                  {{ p.visibility === 'org' ? 'Org' : 'Team' }}
                </span>
              </div>
              <p class="text-xs text-muted-foreground mt-1.5">
                Created {{ formatDate(p.created_at) }}
              </p>
            </button>
          </div>
        </div>

        <div class="flex justify-end mt-6">
          <button
            :disabled="!selectedPipeline"
            class="px-6 py-2.5 bg-primary text-primary-foreground text-sm font-medium rounded-lg hover:brightness-110 disabled:opacity-50 transition-all"
            @click="step = 2"
            data-testid="copy-wizard-next-step1"
          >
            Next: Configure Copy
          </button>
        </div>
      </template>

      <template v-else-if="step === 2">
        <div class="card p-6 mb-6">
          <h2 class="text-lg font-medium text-foreground mb-4">Copy Configuration</h2>

          <div class="space-y-4">
            <div>
              <label class="block text-sm font-medium text-foreground mb-1">New Pipeline Name</label>
              <input
                v-model="pipelineName"
                type="text"
                class="w-full px-3 py-2 border border-input bg-background rounded-lg text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                :placeholder="`Copy of ${selectedPipeline?.name ?? 'Pipeline'}`"
                data-testid="copy-wizard-pipeline-name"
              />
            </div>

            <div>
              <label class="block text-sm font-medium text-foreground mb-1">Target Ownership</label>
              <p class="text-xs text-muted-foreground mb-2">Choose who the copied pipeline belongs to.</p>
              <OwnershipPicker v-model="ownership" label="Owner" />
            </div>

            <div class="border-t border-border pt-4">
              <h3 class="text-sm font-medium text-foreground mb-3">What to Copy</h3>

              <div class="space-y-3">
                <label class="flex items-start gap-3 p-3 rounded-lg border border-input hover:bg-accent/50 cursor-pointer">
                  <input
                    v-model="copyScope"
                    type="radio"
                    value="all"
                    class="mt-0.5"
                    data-testid="copy-wizard-scope-all"
                  />
                  <div>
                    <p class="text-sm font-medium text-foreground">All nodes</p>
                    <p class="text-xs text-muted-foreground">Copy the entire pipeline graph including all agents, manual nodes, and edges.</p>
                  </div>
                </label>

                <label class="flex items-start gap-3 p-3 rounded-lg border border-input hover:bg-accent/50 cursor-pointer">
                  <input
                    v-model="copyScope"
                    type="radio"
                    value="selected"
                    class="mt-0.5"
                    data-testid="copy-wizard-scope-selected"
                  />
                  <div>
                    <p class="text-sm font-medium text-foreground">Selected nodes only</p>
                    <p class="text-xs text-muted-foreground">Only copy specific nodes and their edges. Opens in editor for further adaptation.</p>
                  </div>
                </label>
              </div>
            </div>

            <div class="border-t border-border pt-4 space-y-3">
              <h3 class="text-sm font-medium text-foreground mb-3">Additional Options</h3>

              <label class="flex items-center gap-3 p-3 rounded-lg border border-input hover:bg-accent/50 cursor-pointer">
                <input v-model="keepEvalConfigs" type="checkbox" class="h-4 w-4" data-testid="copy-wizard-keep-evals" />
                <div>
                  <p class="text-sm font-medium text-foreground">Keep eval configurations</p>
                  <p class="text-xs text-muted-foreground">Preserve eval configs, scoring criteria, and threshold settings from the source pipeline.</p>
                </div>
              </label>

              <label class="flex items-center gap-3 p-3 rounded-lg border border-input hover:bg-accent/50 cursor-pointer">
                <input v-model="keepTriggers" type="checkbox" class="h-4 w-4" data-testid="copy-wizard-keep-triggers" />
                <div>
                  <p class="text-sm font-medium text-foreground">Keep triggers</p>
                  <p class="text-xs text-muted-foreground">Copy trigger configurations (schedules, webhooks, events) to the new pipeline.</p>
                </div>
              </label>

              <label class="flex items-center gap-3 p-3 rounded-lg border border-input hover:bg-accent/50 cursor-pointer">
                <input v-model="shareConnectors" type="checkbox" class="h-4 w-4" data-testid="copy-wizard-share-connectors" />
                <div>
                  <p class="text-sm font-medium text-foreground">Share connector bindings</p>
                  <p class="text-xs text-muted-foreground">Keep connector bindings pointing to the same instances. Uncheck to create unbound copies.</p>
                </div>
              </label>
            </div>
          </div>
        </div>

        <div class="flex items-center justify-between">
          <button
            class="px-6 py-2.5 border border-input bg-background text-foreground text-sm font-medium rounded-lg hover:bg-accent transition-colors"
            @click="step = 1"
            data-testid="copy-wizard-back-step2"
          >
            Back
          </button>
          <button
            class="px-6 py-2.5 bg-primary text-primary-foreground text-sm font-medium rounded-lg hover:brightness-110 transition-all"
            @click="step = 3"
            data-testid="copy-wizard-next-step2"
          >
            Next: Review
          </button>
        </div>
      </template>

      <template v-else-if="step === 3">
        <div class="card p-6 mb-6">
          <h2 class="text-lg font-medium text-foreground mb-4">Review Copy</h2>

          <div class="space-y-4">
            <div class="bg-muted rounded-lg p-4">
              <h3 class="text-sm font-medium text-foreground mb-2">Source Pipeline</h3>
              <p class="text-sm text-foreground">{{ selectedPipeline?.name }}</p>
              <p v-if="selectedPipeline?.description" class="text-xs text-muted-foreground mt-0.5">{{ selectedPipeline?.description }}</p>
            </div>

            <div class="grid grid-cols-2 gap-4">
              <div class="bg-muted rounded-lg p-4">
                <p class="text-xs text-muted-foreground mb-1">New Name</p>
                <p class="text-sm font-medium text-foreground">{{ displayName }}</p>
              </div>
              <div class="bg-muted rounded-lg p-4">
                <p class="text-xs text-muted-foreground mb-1">Visibility</p>
                <p class="text-sm font-medium text-foreground">{{ ownership.visibility === 'org' ? 'Org-wide' : 'Team' }}</p>
              </div>
            </div>

            <div class="bg-muted rounded-lg p-4">
              <h3 class="text-sm font-medium text-foreground mb-2">Copy Options</h3>
              <ul class="space-y-1.5 text-sm">
                <li class="flex items-center gap-2">
                  <svg v-if="copyScope === 'all'" xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="text-success"><polyline points="20 6 9 17 4 12"/></svg>
                  <svg v-else xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="text-muted-foreground"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                  <span :class="copyScope === 'all' ? 'text-foreground' : 'text-muted-foreground'">{{ copyScope === 'all' ? 'All nodes will be copied' : 'Selected nodes only' }}</span>
                </li>
                <li class="flex items-center gap-2">
                  <svg v-if="keepEvalConfigs" xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="text-success"><polyline points="20 6 9 17 4 12"/></svg>
                  <svg v-else xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="text-muted-foreground"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                  <span :class="keepEvalConfigs ? 'text-foreground' : 'text-muted-foreground'">{{ keepEvalConfigs ? 'Eval configurations preserved' : 'Eval configurations excluded' }}</span>
                </li>
                <li class="flex items-center gap-2">
                  <svg v-if="keepTriggers" xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="text-success"><polyline points="20 6 9 17 4 12"/></svg>
                  <svg v-else xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="text-muted-foreground"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                  <span :class="keepTriggers ? 'text-foreground' : 'text-muted-foreground'">{{ keepTriggers ? 'Triggers preserved' : 'Triggers excluded' }}</span>
                </li>
                <li class="flex items-center gap-2">
                  <svg v-if="shareConnectors" xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="text-success"><polyline points="20 6 9 17 4 12"/></svg>
                  <svg v-else xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="text-muted-foreground"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                  <span :class="shareConnectors ? 'text-foreground' : 'text-muted-foreground'">{{ shareConnectors ? 'Connector bindings shared' : 'Connector bindings unbound' }}</span>
                </li>
              </ul>
            </div>

            <div v-if="ownership.owner_team_id" class="bg-muted rounded-lg p-4">
              <p class="text-xs text-muted-foreground mb-1">Target Team</p>
              <p class="text-sm font-medium text-foreground">{{ ownership.owner_team_id }}</p>
            </div>
          </div>
        </div>

        <div class="flex items-center justify-between">
          <button
            class="px-6 py-2.5 border border-input bg-background text-foreground text-sm font-medium rounded-lg hover:bg-accent transition-colors"
            @click="step = 2"
            data-testid="copy-wizard-back-step3"
          >
            Back
          </button>
          <button
            :disabled="executing"
            class="px-6 py-2.5 bg-primary text-primary-foreground text-sm font-medium rounded-lg hover:brightness-110 disabled:opacity-50 transition-all"
            @click="executeCopy"
            data-testid="copy-wizard-execute"
          >
            {{ executing ? 'Copying...' : 'Copy Pipeline' }}
          </button>
        </div>
      </template>

      <template v-else-if="step === 4">
        <div class="card p-6">
          <div v-if="progressStep === 'preparing'" class="text-center py-8">
            <div class="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent mx-auto mb-4" />
            <p class="text-sm text-muted-foreground">Preparing copy...</p>
          </div>

          <div v-else-if="progressStep === 'cloning'" class="text-center py-8">
            <div class="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent mx-auto mb-4" />
            <p class="text-sm text-foreground font-medium mb-1">Cloning pipeline...</p>
            <p class="text-sm text-muted-foreground">Creating copy of {{ selectedPipeline?.name }}</p>
            <div class="w-full bg-muted rounded-full h-2 mt-4 max-w-xs mx-auto">
              <div class="bg-primary h-2 rounded-full transition-all duration-500" style="width: 60%" />
            </div>
          </div>

          <div v-else-if="progressStep === 'configuring'" class="text-center py-8">
            <div class="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent mx-auto mb-4" />
            <p class="text-sm text-foreground font-medium mb-1">Applying configuration...</p>
            <p class="text-sm text-muted-foreground">Setting up ownership and options</p>
            <div class="w-full bg-muted rounded-full h-2 mt-4 max-w-xs mx-auto">
              <div class="bg-primary h-2 rounded-full transition-all duration-500" style="width: 85%" />
            </div>
          </div>

          <div v-else-if="progressStep === 'complete'" class="text-center py-8">
            <div class="w-12 h-12 rounded-full bg-success/20 flex items-center justify-center mx-auto mb-4">
              <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="text-success"><polyline points="20 6 9 17 4 12"/></svg>
            </div>
            <p class="text-lg font-medium text-foreground mb-1">Pipeline Copied!</p>
            <p class="text-sm text-muted-foreground mb-6">{{ result?.name }} is ready for adaptation.</p>
            <div class="flex items-center justify-center gap-3">
              <button
                class="px-6 py-2.5 bg-primary text-primary-foreground text-sm font-medium rounded-lg hover:brightness-110 transition-all"
                @click="openInEditor"
                data-testid="copy-wizard-open-editor"
              >
                Open in Editor
              </button>
              <button
                class="px-6 py-2.5 border border-input bg-background text-foreground text-sm font-medium rounded-lg hover:bg-accent transition-colors"
                @click="reset"
                data-testid="copy-wizard-copy-another"
              >
                Copy Another
              </button>
            </div>
          </div>

          <div v-else-if="progressStep === 'error'" class="text-center py-8">
            <div class="w-12 h-12 rounded-full bg-destructive/20 flex items-center justify-center mx-auto mb-4">
              <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="text-destructive"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
            </div>
            <p class="text-lg font-medium text-destructive mb-1">Copy Failed</p>
            <p class="text-sm text-muted-foreground mb-6">{{ executeError }}</p>
            <div class="flex items-center justify-center gap-3">
              <button
                class="px-6 py-2.5 bg-primary text-primary-foreground text-sm font-medium rounded-lg hover:brightness-110 transition-all"
                @click="executeCopy"
                data-testid="copy-wizard-retry"
              >
                Retry
              </button>
              <button
                class="px-6 py-2.5 border border-input bg-background text-foreground text-sm font-medium rounded-lg hover:bg-accent transition-colors"
                @click="step = 3"
                data-testid="copy-wizard-back-error"
              >
                Back to Review
              </button>
            </div>
          </div>
        </div>
      </template>
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useApi } from '../composables/useApi'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import OwnershipPicker from '../components/OwnershipPicker.vue'
import type { OwnershipValue } from '../components/OwnershipPicker.vue'

interface PipelineItem {
  id: string
  name: string
  description: string | null
  visibility: string
  created_at: string
}

interface PipelineListResponse {
  items: PipelineItem[]
  total: number
  page: number
  page_size: number
}

interface CloneResponse {
  id: string
  name: string
  description: string | null
  visibility: string
  created_at: string
  updated_at: string
}

const steps = ['Select Pipeline', 'Configure', 'Review', 'Execute']
const { get, post } = useApi()
const router = useRouter()

const step = ref(1)
const loading = ref(true)
const error = ref<string | null>(null)
const pipelines = ref<PipelineItem[]>([])
const selectedPipeline = ref<PipelineItem | null>(null)
const searchQuery = ref('')
const visibilityFilter = ref<'all' | 'org' | 'team'>('all')

const pipelineName = ref('')
const ownership = ref<OwnershipValue>({ owner_team_id: null, visibility: 'org' })
const copyScope = ref<'all' | 'selected'>('all')
const keepEvalConfigs = ref(true)
const keepTriggers = ref(true)
const shareConnectors = ref(true)

const executing = ref(false)
const executeError = ref<string | null>(null)
const progressStep = ref<'preparing' | 'cloning' | 'configuring' | 'complete' | 'error'>('preparing')
const result = ref<CloneResponse | null>(null)

const visibilityFilters = [
  { label: 'All', value: 'all' as const },
  { label: 'Org', value: 'org' as const },
  { label: 'Team', value: 'team' as const },
]

const displayName = computed(() => pipelineName.value || `Copy of ${selectedPipeline.value?.name ?? 'Pipeline'}`)

const filteredPipelines = computed(() => {
  let list = pipelines.value
  if (visibilityFilter.value !== 'all') {
    list = list.filter(p => p.visibility === visibilityFilter.value)
  }
  if (searchQuery.value) {
    const q = searchQuery.value.toLowerCase()
    list = list.filter(p => p.name.toLowerCase().includes(q) || (p.description?.toLowerCase() ?? '').includes(q))
  }
  return list
})

function formatDate(dateStr: string): string {
  try {
    const d = new Date(dateStr)
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
  } catch {
    return dateStr
  }
}

function retry() {
  error.value = null
  loading.value = true
  fetchPipelines()
}

async function fetchPipelines() {
  try {
    const data = await get<PipelineListResponse>('/api/v1/pipelines?page_size=100')
    pipelines.value = data.items || []
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to load pipelines'
  } finally {
    loading.value = false
  }
}

function onBack() {
  if (step.value === 1) {
    router.push({ name: 'library' })
  } else {
    step.value--
  }
}

async function executeCopy() {
  if (!selectedPipeline.value) return
  executing.value = true
  executeError.value = null
  progressStep.value = 'preparing'

  try {
    await new Promise(r => setTimeout(r, 300))
    progressStep.value = 'cloning'

    const data = await post<CloneResponse>(
      `/api/v1/pipelines/${selectedPipeline.value.id}/clone`,
      { name: displayName.value || undefined },
    )
    result.value = data

    progressStep.value = 'configuring'
    await new Promise(r => setTimeout(r, 400))
    progressStep.value = 'complete'
    step.value = 4
  } catch (e) {
    progressStep.value = 'error'
    executeError.value = e instanceof Error ? e.message : 'Failed to copy pipeline'
    step.value = 4
  } finally {
    executing.value = false
  }
}

function openInEditor() {
  if (!result.value) return
  router.push({ name: 'pipeline-editor', params: { id: result.value.id } })
}

function reset() {
  step.value = 1
  selectedPipeline.value = null
  pipelineName.value = ''
  ownership.value = { owner_team_id: null, visibility: 'org' }
  copyScope.value = 'all'
  keepEvalConfigs.value = true
  keepTriggers.value = true
  shareConnectors.value = true
  executing.value = false
  executeError.value = null
  progressStep.value = 'preparing'
  result.value = null
  searchQuery.value = ''
  visibilityFilter.value = 'all'
  fetchPipelines()
}

onMounted(fetchPipelines)
</script>
