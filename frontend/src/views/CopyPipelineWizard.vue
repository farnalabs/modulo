<template>
  <div class="min-h-screen bg-background">
    <header class="bg-card border-b border-border px-6 py-4">
      <div class="max-w-3xl mx-auto">
        <BackLink to="/pipelines" :label="$t('views.CopyPipelineWizard.back_to_pipelines')" class="mb-2" />
        <PageHeader :title="$t('views.CopyPipelineWizard.copy_pipeline')" :subtitle="$t('views.CopyPipelineWizard.duplicate_an_existing_pipeline_and_adapt_it_for_a_new_purpos')" />
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
            <Check v-if="step > i + 1" :size="14" aria-hidden="true" />
            <span v-else>{{ i + 1 }}</span>
          </div>
          <span class="text-sm" :class="step === i + 1 ? 'text-foreground font-medium' : 'text-muted-foreground'">{{ s }}</span>
          <ChevronRight v-if="i < steps.length - 1" :size="16" aria-hidden="true" class="text-muted-foreground/40" />
        </div>
      </div>

      <LoadingSpinner v-if="loading" />

      <ErrorAlert v-else-if="error" :message="error" :on-retry="retry" class="mb-6" />

      <template v-else-if="step === 1">
        <div class="card p-6">
          <h2 class="text-lg font-medium text-foreground mb-1">{{ $t('views.CopyPipelineWizard.select_source_pipeline') }}</h2>
          <p class="text-sm text-muted-foreground mb-4">{{ $t('views.CopyPipelineWizard.choose_the_pipeline_you_want_to_copy_and_adapt') }}</p>

          <FilterBar class="mb-4"
            :search="{ placeholder: $t('views.CopyPipelineWizard.search_pipelines_by_name') }"
            :search-value="searchQuery"
            @update:search="searchQuery = $event"
          />

          <div class="flex gap-2 mb-4">
            <button
              v-for="f in visibilityFilters"
              :key="f.value"
              type="button"
              class="px-3 py-1.5 text-xs font-medium rounded-full border transition-colors"
              :class="visibilityFilter === f.value ? 'bg-primary text-primary-foreground border-primary' : 'border-input text-muted-foreground hover:bg-accent'"
              @click="visibilityFilter = f.value"
              data-testid="copy-wizard-visibility-filter"
            >
              {{ f.label }}
            </button>
          </div>

          <div v-if="!searchQuery && visibilityFilter === 'all' && pipelines.length === 0" class="card p-8 text-center">
            <p class="text-lg font-medium">{{ $t('views.CopyPipelineWizard.no_pipelines_available') }}</p>
            <p class="mt-1 text-sm text-muted-foreground">
              {{ $t('views.CopyPipelineWizard.create_one_from_the_library_first') }}
            </p>
          </div>

          <div v-else-if="filteredPipelines.length === 0" class="py-12 text-center text-sm text-muted-foreground">
            {{ $t('views.CopyPipelineWizard.no_pipelines_match_your_search') }}
          </div>

          <div v-else class="space-y-2 max-h-96 overflow-y-auto">
            <button
              v-for="p in filteredPipelines"
              :key="p.id"
              type="button"
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
                  {{ p.visibility === 'org' ? $t('views.CopyPipelineWizard.org') : $t('views.CopyPipelineWizard.team') }}
                </span>
              </div>
              <p class="text-xs text-muted-foreground mt-1.5">
                {{ $t('views.CopyPipelineWizard.created') }} {{ formatDate(p.created_at) }}
              </p>
            </button>
          </div>
        </div>

<div class="flex justify-end mt-6">
              <Button :disabled="!selectedPipeline" class="px-6 py-2.5" @click="step = 2" data-testid="copy-wizard-next-step1">
                {{ $t('views.CopyPipelineWizard.next_configure_copy') }}
              </Button>
            </div>
      </template>

      <template v-else-if="step === 2">
        <div class="card p-6 mb-6">
          <h2 class="text-lg font-medium text-foreground mb-4">{{ $t('views.CopyPipelineWizard.copy_configuration') }}</h2>

          <div class="space-y-4">
            <div>
              <label for="copypipelinewizard-field-6" class="block text-sm font-medium text-foreground mb-1">{{ $t('views.CopyPipelineWizard.new_pipeline_name') }}</label>
              <input id="copypipelinewizard-field-6"
                v-model="pipelineName"
                type="text"
                class="w-full px-3 py-2 border border-input bg-background rounded-lg text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                :placeholder="defaultPipelineName"
                data-testid="copy-wizard-pipeline-name"
              />
            </div>

            <div>
              <span class="block text-sm font-medium text-foreground mb-1">{{ $t('views.CopyPipelineWizard.target_ownership') }}</span>
              <p class="text-xs text-muted-foreground mb-2">{{ $t('views.CopyPipelineWizard.choose_who_the_copied_pipeline_belongs_to') }}</p>
              <OwnershipPicker v-model="ownership" :label="$t('views.LibraryPipelineWizard.owner')" />
            </div>

            <div class="border-t border-border pt-4">
              <h3 class="text-sm font-medium text-foreground mb-3">{{ $t('views.CopyPipelineWizard.what_to_copy') }}</h3>

              <div class="space-y-3">
                <label for="copypipelinewizard-field-5" class="flex items-start gap-3 p-3 rounded-lg border border-input hover:bg-accent/50 cursor-pointer">
                  <input id="copypipelinewizard-field-5"
                    v-model="copyScope"
                    type="radio"
                    value="all"
                    class="mt-0.5"
                    data-testid="copy-wizard-scope-all"
                  />
                  <div>
                    <p class="text-sm font-medium text-foreground">{{ $t('views.CopyPipelineWizard.all_nodes') }}</p>
                    <p class="text-xs text-muted-foreground">{{ $t('views.CopyPipelineWizard.copy_the_entire_pipeline_graph_including_all_agents_manual_nodes_and_edges') }}</p>
                  </div>
                </label>

                <label for="copypipelinewizard-field-4" class="flex items-start gap-3 p-3 rounded-lg border border-input hover:bg-accent/50 cursor-pointer">
                  <input id="copypipelinewizard-field-4"
                    v-model="copyScope"
                    type="radio"
                    value="selected"
                    class="mt-0.5"
                    data-testid="copy-wizard-scope-selected"
                  />
                  <div>
                    <p class="text-sm font-medium text-foreground">{{ $t('views.CopyPipelineWizard.selected_nodes_only') }}</p>
                    <p class="text-xs text-muted-foreground">{{ $t('views.CopyPipelineWizard.only_copy_specific_nodes_and_their_edges_opens_in_editor_for_further_adaptation') }}</p>
                  </div>
                </label>
              </div>
            </div>

            <div class="border-t border-border pt-4 space-y-3">
              <h3 class="text-sm font-medium text-foreground mb-3">{{ $t('views.CopyPipelineWizard.additional_options') }}</h3>

              <label for="copypipelinewizard-field-3" class="flex items-center gap-3 p-3 rounded-lg border border-input hover:bg-accent/50 cursor-pointer">
                <input id="copypipelinewizard-field-3" v-model="keepEvalConfigs" type="checkbox" class="h-4 w-4" data-testid="copy-wizard-keep-evals" />
                <div>
                  <p class="text-sm font-medium text-foreground">{{ $t('views.CopyPipelineWizard.keep_eval_configurations') }}</p>
                  <p class="text-xs text-muted-foreground">{{ $t('views.CopyPipelineWizard.preserve_eval_configs_scoring_criteria_and_threshold_settings_from_the_source_pipeline') }}</p>
                </div>
              </label>

              <label for="copypipelinewizard-field-2" class="flex items-center gap-3 p-3 rounded-lg border border-input hover:bg-accent/50 cursor-pointer">
                <input id="copypipelinewizard-field-2" v-model="keepTriggers" type="checkbox" class="h-4 w-4" data-testid="copy-wizard-keep-triggers" />
                <div>
                  <p class="text-sm font-medium text-foreground">{{ $t('views.CopyPipelineWizard.keep_triggers') }}</p>
                  <p class="text-xs text-muted-foreground">{{ $t('views.CopyPipelineWizard.copy_trigger_configurations_schedules_webhooks_events_to_the_new_pipeline') }}</p>
                </div>
              </label>

              <label for="copypipelinewizard-field-1" class="flex items-center gap-3 p-3 rounded-lg border border-input hover:bg-accent/50 cursor-pointer">
                <input id="copypipelinewizard-field-1" v-model="shareConnectors" type="checkbox" class="h-4 w-4" data-testid="copy-wizard-share-connectors" />
                <div>
                  <p class="text-sm font-medium text-foreground">{{ $t('views.CopyPipelineWizard.share_connector_bindings') }}</p>
                  <p class="text-xs text-muted-foreground">{{ $t('views.CopyPipelineWizard.keep_connector_bindings_pointing_to_the_same_instances_uncheck_to_create_unbound_copies') }}</p>
                </div>
              </label>
            </div>
          </div>
        </div>

        <div class="flex items-center justify-between">
          <button
            type="button"
            class="px-6 py-2.5 border border-input bg-background text-foreground text-sm font-medium rounded-lg hover:bg-accent transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            @click="step = 1"
            data-testid="copy-wizard-back-step2"
          >
            {{ $t('views.CopyPipelineWizard.back') }}
          </button>
          <Button class="px-6 py-2.5" @click="step = 3" data-testid="copy-wizard-next-step2">
            {{ $t('views.CopyPipelineWizard.next_review') }}
          </Button>
        </div>
      </template>

      <template v-else-if="step === 3">
        <div class="card p-6 mb-6">
          <h2 class="text-lg font-medium text-foreground mb-4">{{ $t('views.CopyPipelineWizard.review_copy') }}</h2>

          <div class="space-y-4">
            <div class="bg-muted rounded-lg p-4">
              <h3 class="text-sm font-medium text-foreground mb-2">{{ $t('views.CopyPipelineWizard.source_pipeline') }}</h3>
              <p class="text-sm text-foreground">{{ selectedPipeline?.name || '—' }}</p>
              <p v-if="selectedPipeline?.description" class="text-xs text-muted-foreground mt-0.5">{{ selectedPipeline?.description }}</p>
            </div>

            <div class="grid grid-cols-2 gap-4">
              <div class="bg-muted rounded-lg p-4">
                <p class="text-xs text-muted-foreground mb-1">{{ $t('views.CopyPipelineWizard.new_name') }}</p>
                <p class="text-sm font-medium text-foreground">{{ displayName }}</p>
              </div>
              <div class="bg-muted rounded-lg p-4">
                <p class="text-xs text-muted-foreground mb-1">{{ $t('views.CopyPipelineWizard.visibility') }}</p>
                <p class="text-sm font-medium text-foreground">{{ ownership.visibility === 'org' ? $t('views.CopyPipelineWizard.org_wide') : $t('views.CopyPipelineWizard.team') }}</p>
              </div>
            </div>

            <div class="bg-muted rounded-lg p-4">
              <h3 class="text-sm font-medium text-foreground mb-2">{{ $t('views.CopyPipelineWizard.copy_options') }}</h3>
              <ul class="space-y-1.5 text-sm">
                <li class="flex items-center gap-2">
                  <Check v-if="copyScope === 'all'" :size="14" aria-hidden="true" class="text-success" />
                  <X v-else :size="14" aria-hidden="true" class="text-muted-foreground" />
                  <span :class="copyScope === 'all' ? 'text-foreground' : 'text-muted-foreground'">{{ copyScope === 'all' ? $t('views.CopyPipelineWizard.all_nodes_will_be_copied') : $t('views.CopyPipelineWizard.selected_nodes_only') }}</span>
                </li>
                <li class="flex items-center gap-2">
                  <Check v-if="keepEvalConfigs" :size="14" aria-hidden="true" class="text-success" />
                  <X v-else :size="14" aria-hidden="true" class="text-muted-foreground" />
                  <span :class="keepEvalConfigs ? 'text-foreground' : 'text-muted-foreground'">{{ keepEvalConfigs ? $t('views.CopyPipelineWizard.eval_configurations_preserved') : $t('views.CopyPipelineWizard.eval_configurations_excluded') }}</span>
                </li>
                <li class="flex items-center gap-2">
                  <Check v-if="keepTriggers" :size="14" aria-hidden="true" class="text-success" />
                  <X v-else :size="14" aria-hidden="true" class="text-muted-foreground" />
                  <span :class="keepTriggers ? 'text-foreground' : 'text-muted-foreground'">{{ keepTriggers ? $t('views.CopyPipelineWizard.triggers_preserved') : $t('views.CopyPipelineWizard.triggers_excluded') }}</span>
                </li>
                <li class="flex items-center gap-2">
                  <Check v-if="shareConnectors" :size="14" aria-hidden="true" class="text-success" />
                  <X v-else :size="14" aria-hidden="true" class="text-muted-foreground" />
                  <span :class="shareConnectors ? 'text-foreground' : 'text-muted-foreground'">{{ shareConnectors ? $t('views.CopyPipelineWizard.connector_bindings_shared') : $t('views.CopyPipelineWizard.connector_bindings_unbound') }}</span>
                </li>
              </ul>
            </div>

            <div v-if="ownership.owner_team_id" class="bg-muted rounded-lg p-4">
              <p class="text-xs text-muted-foreground mb-1">{{ $t('views.CopyPipelineWizard.target_team') }}</p>
              <p class="text-sm font-medium text-foreground" :title="ownership.owner_team_id">
                <span class="select-all font-mono">{{ shortId(ownership.owner_team_id) }}</span>
              </p>
            </div>
          </div>
        </div>

        <div class="flex items-center justify-between">
          <button
            type="button"
            class="px-6 py-2.5 border border-input bg-background text-foreground text-sm font-medium rounded-lg hover:bg-accent transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            @click="step = 2"
            data-testid="copy-wizard-back-step3"
          >
            {{ $t('views.CopyPipelineWizard.back') }}
          </button>
          <Button :disabled="executing" class="px-6 py-2.5" @click="executeCopy" data-testid="copy-wizard-execute">
            {{ executing ? $t('views.CopyPipelineWizard.copying') : $t('views.CopyPipelineWizard.copy_pipeline') }}
          </Button>
        </div>
      </template>

      <template v-else-if="step === 4">
        <div class="card p-6">
          <div v-if="progressStep === 'preparing'" class="text-center py-8">
            <div class="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent mx-auto mb-4" />
            <p class="text-sm text-muted-foreground">{{ $t('views.CopyPipelineWizard.preparing_copy') }}</p>
          </div>

          <div v-else-if="progressStep === 'cloning'" class="text-center py-8">
            <div class="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent mx-auto mb-4" />
            <p class="text-sm text-foreground font-medium mb-1">{{ $t('views.CopyPipelineWizard.cloning_pipeline') }}</p>
            <p class="text-sm text-muted-foreground">{{ $t('views.CopyPipelineWizard.creating_copy_of', { name: selectedPipeline?.name || '—' }) }}</p>
            <div class="w-full bg-muted rounded-full h-2 mt-4 max-w-xs mx-auto">
              <div class="bg-primary h-2 rounded-full transition-all duration-500" style="width: 60%" />
            </div>
          </div>

          <div v-else-if="progressStep === 'configuring'" class="text-center py-8">
            <div class="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent mx-auto mb-4" />
            <p class="text-sm text-foreground font-medium mb-1">{{ $t('views.CopyPipelineWizard.applying_configuration') }}</p>
            <p class="text-sm text-muted-foreground">{{ $t('views.CopyPipelineWizard.setting_up_ownership_and_options') }}</p>
            <div class="w-full bg-muted rounded-full h-2 mt-4 max-w-xs mx-auto">
              <div class="bg-primary h-2 rounded-full transition-all duration-500" style="width: 85%" />
            </div>
          </div>

          <div v-else-if="progressStep === 'complete'" class="text-center py-8">
            <div class="w-12 h-12 rounded-full bg-success/20 flex items-center justify-center mx-auto mb-4">
              <Check :size="24" aria-hidden="true" class="text-success" />
            </div>
            <p class="text-lg font-medium text-foreground mb-1">{{ $t('views.CopyPipelineWizard.pipeline_copied') }}</p>
            <p class="text-sm text-muted-foreground mb-6">{{ $t('views.CopyPipelineWizard.is_ready_for_adaptation', { name: result?.name || '—' }) }}</p>
            <div class="flex items-center justify-center gap-3">
              <Button class="px-6 py-2.5" @click="openInEditor" data-testid="copy-wizard-open-editor">
                {{ $t('views.CopyPipelineWizard.open_in_editor') }}
              </Button>
              <button
                type="button"
                class="px-6 py-2.5 border border-input bg-background text-foreground text-sm font-medium rounded-lg hover:bg-accent transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                @click="reset"
                data-testid="copy-wizard-copy-another"
              >
                {{ $t('views.CopyPipelineWizard.copy_another') }}
              </button>
            </div>
          </div>

          <div v-else-if="progressStep === 'error'" class="text-center py-8">
            <div class="w-12 h-12 rounded-full bg-destructive/20 flex items-center justify-center mx-auto mb-4">
              <XCircle :size="24" aria-hidden="true" class="text-destructive" />
            </div>
            <p class="text-lg font-medium text-destructive mb-1">{{ $t('views.CopyPipelineWizard.copy_failed') }}</p>
            <p class="text-sm text-muted-foreground mb-6">{{ executeError }}</p>
            <div class="flex items-center justify-center gap-3">
              <Button class="px-6 py-2.5" @click="executeCopy" data-testid="copy-wizard-retry">
                {{ $t('views.CopyPipelineWizard.retry') }}
              </Button>
              <button type="button"
                class="px-6 py-2.5 border border-input bg-background text-foreground text-sm font-medium rounded-lg hover:bg-accent transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                @click="step = 3"
                data-testid="copy-wizard-back-error"
              >
                {{ $t('views.CopyPipelineWizard.back_to_review') }}
              </button>
            </div>
          </div>
        </div>
      </template>
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { Check, ChevronRight, X, XCircle } from '@lucide/vue'
import PageHeader from '../components/shared/PageHeader.vue'
import FilterBar from '../components/shared/FilterBar.vue'
import Button from 'primevue/button'
import { useDataFetch } from '../composables/useDataFetch'
import BackLink from '../components/BackLink.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import OwnershipPicker from '../components/OwnershipPicker.vue'
import type { OwnershipValue } from '../components/OwnershipPicker.vue'
import { api } from '../lib/api/client'
import type { components } from '../lib/api/schema'
import { formatDateShort } from '../lib/formatDate'
import { shortId } from '../utils/format'

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

type PipelineResponse = components['schemas']['PipelineResponse']

const { t } = useI18n()
const steps = computed(() => [
  t('views.CopyPipelineWizard.step_select_pipeline'),
  t('views.CopyPipelineWizard.step_configure'),
  t('views.CopyPipelineWizard.step_review'),
  t('views.CopyPipelineWizard.step_execute'),
])
const router = useRouter()

const { loading, error, data: pipelinesResp, load: fetchPipelines } = useDataFetch<PipelineListResponse>(
  () => api.GET('/api/v1/pipelines', { params: { query: { page_size: 100 } } }),
  { initialValue: { items: [] as PipelineItem[], total: 0, page: 1, page_size: 100 } },
)

const pipelines = computed(() => pipelinesResp.value?.items ?? [])

const step = ref(1)
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
const result = ref<PipelineResponse | null>(null)

const visibilityFilters = computed(() => [
  { label: t('views.CopyPipelineWizard.all'), value: 'all' as const },
  { label: t('views.CopyPipelineWizard.org'), value: 'org' as const },
  { label: t('views.CopyPipelineWizard.team'), value: 'team' as const },
])

const defaultPipelineName = computed(() => t('views.CopyPipelineWizard.copy_of_named_pipeline', {
  name: selectedPipeline.value?.name || t('views.CopyPipelineWizard.pipeline'),
}))
const displayName = computed(() => pipelineName.value || defaultPipelineName.value)

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
    return formatDateShort(d)
  } catch {
    return dateStr
  }
}

function retry() {
  fetchPipelines()
}

async function executeCopy() {
  if (!selectedPipeline.value) return
  executing.value = true
  executeError.value = null
  progressStep.value = 'preparing'

  try {
    await new Promise(r => setTimeout(r, 300))
    progressStep.value = 'cloning'

    const { data } = await api.POST('/api/v1/pipelines/{pipeline_id}/clone', {
      params: { path: { pipeline_id: selectedPipeline.value.id } },
      body: { name: displayName.value || undefined },
    })
    if (!data) throw new Error(t('views.CopyPipelineWizard.failed_to_copy_pipeline'))
    result.value = data

    progressStep.value = 'configuring'
    await new Promise(r => setTimeout(r, 400))
    progressStep.value = 'complete'
    step.value = 4
  } catch (e) {
    progressStep.value = 'error'
    executeError.value = e instanceof Error ? e.message : t('views.CopyPipelineWizard.failed_to_copy_pipeline')
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

</script>
