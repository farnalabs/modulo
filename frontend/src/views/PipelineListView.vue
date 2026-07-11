<template>
  <div class="min-h-screen bg-background">
    <header class="bg-card border-b border-border px-6 py-4">
      <div class="mx-auto flex items-center justify-between gap-3 max-w-6xl">
        <PageHeader :title="$t('views.PipelineListView.title')" />
        <FilterBar
          :search="{ placeholder: $t('views.PipelineListView.search_pipelines') }"
          :search-value="search"
          @update:search="search = $event; page = 1"
        />
          <Button
            v-if="allPipelines.length > 0 && !loading"
            variant="default"
            as="router-link"
            to="/library"
            data-testid="pipeline-list-new-pipeline"
          >
            {{ $t('views.PipelineListView.new_pipeline') }}
          </Button>
      </div>
    </header>

    <main class="page-wide" @click.self="showActionMenu = null">
      <div v-if="loading" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <div v-for="i in 6" :key="i" class="card p-5 animate-pulse">
          <div class="h-5 w-3/4 bg-muted rounded mb-2" />
          <div class="h-3 w-full bg-muted rounded mb-1" />
          <div class="h-3 w-2/3 bg-muted rounded mb-4" />
          <div class="h-4 w-16 bg-muted rounded mb-3" />
          <div class="h-9 w-full bg-muted rounded" />
        </div>
      </div>

      <ErrorAlert v-else-if="error" :message="error" :on-retry="loadPipelines" class="mb-6" />

      <div v-else-if="filteredPipelines.length === 0 && search" class="text-center py-16">
        <p class="text-lg font-medium text-foreground">{{ $t('views.PipelineListView.no_pipelines_match_your_search') }}</p>
        <p class="text-sm text-muted-foreground mt-1">{{ $t('views.PipelineListView.try_a_different_search_term') }}</p>
      </div>

      <div v-else-if="allPipelines.length === 0 && !search" class="text-center py-16">
        <svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1" class="mx-auto mb-4 text-muted-foreground/40"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
        <p class="text-lg font-medium text-foreground">{{ $t('views.PipelineListView.no_pipelines_yet') }}</p>
        <p class="text-sm text-muted-foreground mt-1 mb-6">
          Browse the Library to find a template to adapt, or copy an existing pipeline.
        </p>
        <div class="flex items-center justify-center gap-3">
          <Button
            variant="default"
            as="router-link"
            to="/library"
            data-testid="pipeline-list-browse-library"
          >
            Browse Library
          </Button>
          <Button
            variant="outline"
            as="router-link"
            to="/pipelines/copy"
            data-testid="pipeline-list-copy-pipeline"
          >
            Copy Pipeline
          </Button>
        </div>
      </div>

      <div v-else class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <div
          v-for="p in pagedPipelines"
          :key="p.id"
          class="card card-hover p-5 cursor-pointer"
          @click="openPipeline(p)"
          data-testid="pipeline-list-card"
        >
          <div class="flex items-start justify-between gap-2 mb-3">
            <h3 class="text-base font-medium text-foreground truncate">{{ p.name }}</h3>
            <div class="flex items-center gap-1 shrink-0">
              <span
                v-if="p.archived_at"
                class="badge text-xs badge-status-warning"
              >Archived</span>
              <span
                class="badge text-xs"
                :class="p.visibility === 'org' ? 'badge-context-blue' : 'badge-context-purple'"
                data-testid="pipeline-list-visibility-badge"
              >
                {{ p.visibility === 'org' ? 'Org' : 'Team' }}
              </span>
              <div class="relative">
                <button
                  class="rounded p-1 hover:bg-accent"
                  @click.stop="showActionMenu = (showActionMenu === p.id ? null : p.id)"
                  data-testid="pipeline-list-action-menu"
                >
                  <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="1"/><circle cx="12" cy="5" r="1"/><circle cx="12" cy="19" r="1"/></svg>
                </button>
                <div
                  v-if="showActionMenu === p.id"
                  class="absolute right-0 top-full z-20 mt-1 w-40 rounded-lg border bg-card py-1 shadow-lg"
                  @click.stop
                >
                  <button class="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-accent" @click.stop="openRename(p)">Rename</button>
                  <button v-if="!p.archived_at" class="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-accent" @click.stop="handleArchive(p)">Archive</button>
                  <button v-else class="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-accent" @click.stop="handleUnarchive(p)">Unarchive</button>
                  <button v-if="planStore.featureEnabled('pipeline_delete')" class="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-destructive hover:bg-destructive/10" @click.stop="openDelete(p)">Delete</button>
                </div>
              </div>
            </div>
          </div>

          <p v-if="p.description" class="text-sm text-muted-foreground mb-4 line-clamp-2">
            {{ p.description }}
          </p>
          <div v-else class="mb-10" />

          <div class="flex items-center gap-3 text-xs text-muted-foreground">
            <span class="flex items-center gap-1">
              <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 8v4l3 3"/><circle cx="12" cy="12" r="10"/></svg>
              Created {{ formatDate(p.created_at) }}
            </span>
          </div>

          <div class="mt-4 pt-3 border-t border-border flex gap-2">
            <Button
              variant="default"
              class="flex-1"
              data-testid="pipeline-list-open-editor"
            >
              {{ $t('views.PipelineListView.open_in_editor') }}
            </Button>
            <Button
              variant="outline"
              class="flex-1"
              @click.stop="openRunDialog(p)"
              data-testid="pipeline-list-run"
            >
              {{ $t('views.PipelineListView.run') }}
            </Button>
          </div>
        </div>
      </div>

      <div v-if="totalPages > 1 && !loading" class="flex justify-center items-center gap-2 mt-8">
        <button
          :disabled="page <= 1"
          class="px-4 py-2 text-sm border border-input bg-background rounded-lg disabled:opacity-30 hover:bg-accent transition-colors"
          @click="prevPage"
          data-testid="pipeline-list-prev-page"
        >
          {{ $t('views.PipelineListView.previous') }}
        </button>
        <span class="px-4 py-2 text-sm text-muted-foreground">
          {{ $t('views.PipelineListView.page_x_of_y', { page, total: totalPages }) }}
        </span>
        <button
          :disabled="page >= totalPages"
          class="px-4 py-2 text-sm border border-input bg-background rounded-lg disabled:opacity-30 hover:bg-accent transition-colors"
          @click="nextPage"
          data-testid="pipeline-list-next-page"
        >
          {{ $t('views.PipelineListView.next') }}
        </button>
      </div>
    </main>
      <!-- Run dialog modal -->
      <div
        v-if="showRunDialog"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
        @click.self="closeRunDialog"
      >
        <div class="bg-card border border-border rounded-xl shadow-xl w-full max-w-lg mx-4 p-6 space-y-4">
          <div class="flex items-center justify-between">
            <h2 class="text-base font-semibold text-foreground">{{ $t('views.PipelineListView.run_pipeline') }}</h2>
            <button
              class="text-muted-foreground hover:text-foreground transition-colors"
              @click="closeRunDialog"
              data-testid="pipeline-list-run-dialog-close"
              aria-label="Close"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </button>
          </div>

          <p class="text-sm text-muted-foreground">
            Run <span class="font-medium text-foreground">{{ selectedPipeline?.name }}</span>
          </p>

          <div class="space-y-2">
            <label class="block text-sm font-medium text-foreground">{{ $t('views.PipelineListView.prompt') }}</label>
            <textarea
              v-model="prompt"
              placeholder="Enter a prompt (optional)"
              rows="4"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm resize-none focus:outline-none focus:ring-2 focus:ring-primary"
              data-testid="pipeline-list-run-prompt"
            />
          </div>

          <div>
            <button
              class="text-sm text-muted-foreground hover:text-foreground transition-colors flex items-center gap-1"
              @click="showAdvanced = !showAdvanced"
              data-testid="pipeline-list-run-advanced-toggle"
            >
              <svg
                xmlns="http://www.w3.org/2000/svg"
                width="14"
                height="14"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                :class="{ 'rotate-180': showAdvanced }"
                class="transition-transform"
              ><polyline points="6 9 12 15 18 9"/></svg>
              {{ $t('views.PipelineListView.advanced') }}
            </button>
          </div>

          <div v-if="showAdvanced" class="space-y-2">
            <label class="block text-sm font-medium text-foreground">Input Payload (JSON)</label>
            <textarea
              v-model="advancedPayload"
              placeholder='{"prompt": "...", "temperature": 0.7}'
              rows="4"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono resize-none focus:outline-none focus:ring-2 focus:ring-primary"
              data-testid="pipeline-list-run-advanced-payload"
            />
          </div>

          <div v-if="runError" class="rounded-lg bg-destructive/10 border border-destructive/30 p-3 text-sm text-destructive" data-testid="pipeline-list-run-error">
            {{ runError }}
          </div>

          <div class="flex justify-end gap-2 pt-2">
            <button
              class="px-4 py-2 border border-input bg-background text-foreground text-sm font-medium rounded-lg hover:bg-accent transition-colors"
              @click="closeRunDialog"
              data-testid="pipeline-list-run-cancel"
            >
              {{ $t('common.cancel') }}
            </button>
            <Button
              variant="default"
              :disabled="running"
              class="border-primary/30"
              @click="triggerRun"
              data-testid="pipeline-list-run-submit"
            >
              <svg
                v-if="running"
                class="animate-spin h-4 w-4"
                xmlns="http://www.w3.org/2000/svg"
                fill="none"
                viewBox="0 0 24 24"
              >
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
              {{ running ? $t('views.PipelineListView.running') : $t('views.PipelineListView.run_pipeline') }}
            </Button>
          </div>
        </div>
      </div>

      <!-- Rename dialog -->
      <div
        v-if="showRenameDialog"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
        @click.self="showRenameDialog = false"
      >
        <div class="w-full max-w-md rounded-lg border bg-card p-6 shadow-lg">
          <h3 class="mb-4 text-lg font-semibold">Rename Pipeline</h3>
          <div class="space-y-4">
            <div>
              <label class="mb-1 block text-sm font-medium">Name</label>
              <input
                v-model="renameName"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                placeholder="Pipeline name"
                @keyup.enter="handleRename"
              />
            </div>
            <div v-if="renameError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
              {{ renameError }}
            </div>
            <div class="flex justify-end gap-2">
              <button
                class="rounded-lg border border-input bg-background px-4 py-2 text-sm hover:bg-accent"
                @click="showRenameDialog = false"
              >
                Cancel
              </button>
              <button
                :disabled="!renameName.trim() || renaming"
                class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                @click="handleRename"
              >
                {{ renaming ? 'Saving...' : 'Save' }}
              </button>
            </div>
          </div>
        </div>
      </div>

      <!-- Delete confirmation dialog -->
      <div
        v-if="showDeleteConfirm"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
        @click.self="showDeleteConfirm = false"
      >
        <div class="w-full max-w-md rounded-lg border bg-card p-6 shadow-lg">
          <h3 class="mb-4 text-lg font-semibold text-destructive">Delete Pipeline</h3>
          <p class="mb-4 text-sm text-muted-foreground">
            Are you sure? This permanently deletes the pipeline and all its runs.
          </p>
          <div v-if="deleteError" class="mb-4 rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
            {{ deleteError }}
          </div>
          <div class="flex justify-end gap-2">
            <button
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm hover:bg-accent"
              @click="showDeleteConfirm = false"
            >
              Cancel
            </button>
            <button
              class="rounded-lg bg-destructive px-4 py-2 text-sm font-medium text-destructive-foreground hover:bg-destructive/90"
              @click="handleDelete"
            >
              Delete
            </button>
          </div>
        </div>
      </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import PageHeader from '../components/shared/PageHeader.vue'
import FilterBar from '../components/shared/FilterBar.vue'
import { useDataFetch } from '../composables/useDataFetch'
import { usePlanStore } from '../stores/planStore'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import { formatApiError } from '../lib/api/formatError'
import { Button } from '@/components/ui/button'
import { api } from '../lib/api/client'
import { formatDateShort } from '../lib/formatDate'

interface PipelineItem {
  id: string
  organisation_id: string
  name: string
  description: string | null
  visibility: string
  created_at: string
  updated_at: string
  archived_at: string | null
}

interface PipelineListResponse {
  items: PipelineItem[]
  total: number
  page: number
  page_size: number
}

const router = useRouter()
const planStore = usePlanStore()

const { loading, error, data: pipelinesResp, load: loadPipelines } = useDataFetch<PipelineListResponse>(
  () => api.GET('/api/v1/pipelines', { params: { query: { page_size: 100 } } }),
  { initialValue: { items: [] as PipelineItem[], total: 0, page: 1, page_size: 100 } },
)

const allPipelines = computed(() => pipelinesResp.value?.items ?? [])
const showActionMenu = ref<string | null>(null)
const showRenameDialog = ref(false)
const renameTarget = ref<PipelineItem | null>(null)
const renameName = ref('')
const renameError = ref<string | null>(null)
const renaming = ref(false)
const showDeleteConfirm = ref(false)
const deleteTarget = ref<PipelineItem | null>(null)
const deleteError = ref<string | null>(null)
const showRunDialog = ref(false)
const selectedPipeline = ref<PipelineItem | null>(null)
const prompt = ref('')
const showAdvanced = ref(false)
const advancedPayload = ref('')
const running = ref(false)
const runError = ref<string | null>(null)
const search = ref('')
const page = ref(1)
const pageSize = 12

const filteredPipelines = computed(() => {
  const q = search.value.toLowerCase().trim()
  if (!q) return allPipelines.value
  return allPipelines.value.filter(p =>
    p.name.toLowerCase().includes(q) ||
    (p.description?.toLowerCase() ?? '').includes(q)
  )
})

const totalPages = computed(() => Math.ceil(filteredPipelines.value.length / pageSize))

const pagedPipelines = computed(() => {
  const start = (page.value - 1) * pageSize
  return filteredPipelines.value.slice(start, start + pageSize)
})

function prevPage() {
  page.value--
}

function nextPage() {
  page.value++
}

function formatDate(dateStr: string): string {
  const d = new Date(dateStr)
  if (isNaN(d.getTime())) return dateStr
  return formatDateShort(d)
}

function openPipeline(p: PipelineItem) {
  router.push({ name: 'pipeline-editor', params: { id: p.id } })
}

function openRunDialog(p: PipelineItem) {
  selectedPipeline.value = p
  prompt.value = ''
  showAdvanced.value = false
  advancedPayload.value = ''
  runError.value = null
  showRunDialog.value = true
}

function openRename(p: PipelineItem) {
  showActionMenu.value = null
  renameTarget.value = p
  renameName.value = p.name
  renameError.value = null
  showRenameDialog.value = true
}

async function handleRename() {
  if (!renameTarget.value || !renameName.value.trim()) return
  renaming.value = true
  renameError.value = null
  try {
    await api.PATCH('/api/v1/pipelines/{pipeline_id}', {
      params: { path: { pipeline_id: renameTarget.value.id } },
      body: { name: renameName.value.trim() },
    })
    showRenameDialog.value = false
    showActionMenu.value = null
    await loadPipelines()
  } catch (e: unknown) {
    renameError.value = formatApiError(e)
  } finally {
    renaming.value = false
  }
}

async function handleArchive(p: PipelineItem) {
  try {
    await api.POST('/api/v1/pipelines/{pipeline_id}/archive', {
      params: { path: { pipeline_id: p.id } },
    })
    showActionMenu.value = null
    await loadPipelines()
  } catch (e) {
    error.value = formatApiError(e)
  }
}

async function handleUnarchive(p: PipelineItem) {
  try {
    await api.POST('/api/v1/pipelines/{pipeline_id}/unarchive', {
      params: { path: { pipeline_id: p.id } },
    })
    showActionMenu.value = null
    await loadPipelines()
  } catch (e) {
    error.value = formatApiError(e)
  }
}

function openDelete(p: PipelineItem) {
  showActionMenu.value = null
  deleteTarget.value = p
  deleteError.value = null
  showDeleteConfirm.value = true
}

async function handleDelete() {
  if (!deleteTarget.value) return
  deleteError.value = null
  try {
    await api.DELETE('/api/v1/pipelines/{pipeline_id}', {
      params: { path: { pipeline_id: deleteTarget.value.id } },
    })
    showDeleteConfirm.value = false
    deleteTarget.value = null
    await loadPipelines()
  } catch (e: unknown) {
    deleteError.value = formatApiError(e)
  }
}

function closeRunDialog() {
  showRunDialog.value = false
  selectedPipeline.value = null
  prompt.value = ''
  runError.value = null
}

async function triggerRun() {
  if (!selectedPipeline.value) return
  running.value = true
  runError.value = null
  try {
    let inputPayload: Record<string, unknown>
    if (showAdvanced.value && advancedPayload.value.trim()) {
      try {
        inputPayload = JSON.parse(advancedPayload.value)
      } catch {
        runError.value = 'Invalid JSON in advanced payload'
        running.value = false
        return
      }
    } else if (prompt.value.trim()) {
      inputPayload = { prompt: prompt.value }
    } else {
      inputPayload = {}
    }
    const { data } = await api.POST('/api/v1/runs', {
      body: {
        pipeline_id: selectedPipeline.value.id,
        input_payload: inputPayload,
      },
    })
    showRunDialog.value = false
    if (data) router.push({ name: 'run-detail', params: { id: (data as any).id } })
  } catch (e) {
    runError.value = formatApiError(e)
  } finally {
    running.value = false
  }
}
</script>
