<template>
  <div class="min-h-screen bg-background flex flex-col">
    <header class="bg-card border-b border-border px-6 py-4">
      <div class="mx-auto flex flex-wrap items-center justify-between gap-3 max-w-6xl">
        <PageHeader :title="$t('views.PipelineListView.title')">
          <template #right>
            <div class="flex items-center gap-2">
              <div class="w-48 sm:w-auto">
                <FilterBar
                  :search="{ placeholder: $t('views.PipelineListView.search_pipelines') }"
                  :search-value="search"
                  @update:search="search = $event"
                />
              </div>
              <Button :class="allPipelines.length > 0 && !loading ? '' : 'invisible'" as="router-link" to="/library" data-testid="pipeline-list-new-pipeline">
                {{ $t('views.PipelineListView.new_pipeline') }}
              </Button>
            </div>
          </template>
        </PageHeader>
      </div>
    </header>

    <div class="flex flex-1 min-h-0">
      <!-- Folder sidebar -->
      <FolderTree
        :selected-folder-id="selectedFolderId"
        :pipeline-counts="folderPipelineCounts"
        @select-folder="onSelectFolder"
        @folders-changed="loadPipelines"
        @move-pipeline="onMovePipeline"
      />
      <p v-if="folderError" class="px-4 py-2 text-xs text-destructive">
        Failed to load folders: {{ folderError }}
      </p>

      <main class="flex-1 page-wide min-w-0">
        <div v-if="moveError && !showMoveToFolder" class="mb-4 flex items-center justify-between gap-3 rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive" role="alert" data-testid="pipeline-list-move-error">
          <span>{{ moveError }}</span>
          <button type="button" class="shrink-0 text-destructive/70 hover:text-destructive" aria-label="Dismiss" @click="moveError = null">
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
          </button>
        </div>
        <div v-if="loading || !foldersReady">
          <!-- Reserve the mobile folder-filter row and the breadcrumb row that the
               real content renders above the table, so the table does not drop
               (layout shift) when the skeleton is replaced by the loaded content. -->
          <div class="md:hidden mb-4" aria-hidden="true">
            <div class="h-9 w-full rounded-lg border border-input bg-muted animate-pulse" />
          </div>
          <div class="mb-4" aria-hidden="true">
            <div class="h-6 w-40 bg-muted rounded animate-pulse" />
          </div>
          <div class="card rounded-lg border border-border overflow-hidden animate-pulse" aria-hidden="true">
            <div class="overflow-x-auto">
              <table class="w-full text-left text-sm">
                <thead class="bg-muted/50 text-xs font-medium uppercase text-muted-foreground">
                  <tr>
                    <th v-for="i in 7" :key="`th-${i}`" class="px-4 py-3">
                      <div class="h-4 w-16 bg-muted rounded" />
                    </th>
                  </tr>
                </thead>
                <tbody class="divide-y">
                  <tr v-for="i in 6" :key="`row-${i}`">
                    <td v-for="j in 7" :key="`cell-${j}`" class="px-4 py-3">
                      <div class="h-6 bg-muted rounded" :class="j === 1 ? 'w-3/4' : 'w-full'" />
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
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
            Create a new pipeline or browse the Library to find a template.
          </p>
          <div class="flex items-center justify-center gap-3">
            <Button as="router-link" to="/library" data-testid="pipeline-list-new-pipeline">
              New Pipeline
            </Button>
            <Button severity="secondary" outlined as="router-link" to="/library" data-testid="pipeline-list-browse-library">
              Browse Library
            </Button>
          </div>
        </div>

        <div v-else>
          <!-- Mobile folder filter — the FolderTree is hidden below md, so offer folder selection here -->
          <div v-if="foldersList.length > 0" class="md:hidden mb-4">
            <Select
  v-model="mobileFolderSelectValue"
  :aria-label="$t('views.PipelineListView.folders')"
  :placeholder="$t('views.PipelineListView.folders')"
  data-testid="pipeline-list-mobile-folder-select"
  class="w-full"
  :options="[{ value: '__all__', label: $t('views.PipelineListView.all_pipelines') }, ...foldersList.map(f => ({ value: f.id, label: f.name }))]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
          </div>
          <!-- Breadcrumb navigation -->
          <div class="mb-4 flex items-center gap-2 text-sm">
            <template v-if="selectedFolderId && selectedFolderName">
              <button type="button" class="text-muted-foreground hover:text-foreground transition-colors" @click="onSelectFolder(null)">
                {{ $t('views.PipelineListView.all_pipelines') }}
              </button>
              <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="text-muted-foreground"><polyline points="9 18 15 12 9 6"/></svg>
              <span class="font-medium text-foreground">{{ selectedFolderName }}</span>
            </template>
            <h2 v-else class="text-base font-semibold text-foreground">{{ $t('views.PipelineListView.all_pipelines') }}</h2>
          </div>
          <!-- Table / Tree view -->
          <div class="card rounded-lg border border-border overflow-hidden">
            <div class="overflow-x-auto">
              <table class="w-full text-left text-sm">
              <thead class="bg-muted/50 text-xs font-medium uppercase text-muted-foreground">
                <tr>
                  <th class="px-4 py-3">{{ $t('views.PipelineListView.name') }}</th>
                  <th class="px-4 py-3">{{ $t('views.PipelineListView.description') }}</th>
                  <th class="px-4 py-3">{{ $t('views.PipelineListView.visibility') }}</th>
                  <th class="px-4 py-3">{{ $t('views.PipelineListView.last_run') }}</th>
                  <th class="px-4 py-3">{{ $t('views.PipelineListView.trigger') }}</th>
                  <th class="px-4 py-3">{{ $t('views.PipelineListView.created') }}</th>
                  <th class="px-4 py-3 text-right">{{ $t('views.PipelineListView.actions') }}</th>
                </tr>
              </thead>
              <tbody class="divide-y">
                <template v-for="(row, i) in treeRows" :key="i">
                  <tr v-if="row.type === 'folder'" class="bg-muted/20 hover:bg-muted/30 transition-colors" data-testid="pipeline-tree-folder-row" @dragover.prevent @drop="onTableFolderDrop((row.data as FolderItem).id, $event)">
                    <td colspan="7" class="px-4 py-2">
                      <button
                        type="button"
                        class="flex w-full items-center gap-2 text-sm font-medium text-foreground text-left"
                        @click="toggleFolder((row.data as FolderItem).id)"
                        :aria-expanded="isFolderExpanded((row.data as FolderItem).id)"
                        data-testid="pipeline-tree-folder-toggle"
                      >
                        <svg
                          xmlns="http://www.w3.org/2000/svg"
                          width="14"
                          height="14"
                          viewBox="0 0 24 24"
                          fill="none"
                          stroke="currentColor"
                          stroke-width="2"
                          stroke-linecap="round"
                          stroke-linejoin="round"
                          :class="{ 'rotate-90': isFolderExpanded((row.data as FolderItem).id) }"
                          class="transition-transform shrink-0"
                        >
                          <polyline points="9 18 15 12 9 6" />
                        </svg>
                        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="shrink-0"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>
                        {{ (row.data as FolderItem).name }}
                        <span class="text-muted-foreground text-xs ml-2">{{ pipelineFolderCount.get((row.data as FolderItem).id) || 0 }} {{ $t('views.PipelineListView.pipelines') }}</span>
                      </button>
                    </td>
                  </tr>

                  <tr v-else-if="row.type === 'uncategorised-header'" class="bg-muted/20">
                    <td colspan="7" class="px-4 py-2">
                      <span class="flex items-center gap-2 text-sm font-medium text-muted-foreground">
                        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="shrink-0"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
                        {{ $t('views.PipelineListView.uncategorised') }}
                      </span>
                    </td>
                  </tr>

                  <tr
                    v-else-if="row.type === 'pipeline'"
                    class="cursor-pointer transition-colors hover:bg-muted/30"
                    @click="openPipeline(row.data as PipelineItem)"
                    :data-testid="`pipeline-tree-row-${(row.data as PipelineItem).id}`"
                    draggable="true"
                    @dragstart="onPipelineDragStart(row.data as PipelineItem, $event)"
                    @dragover.prevent
                    @drop="onPipelineDrop(row.data as PipelineItem, $event)"
                  >
                    <td class="px-4 py-3" :style="{ paddingLeft: `${12 + (row.depth || 0) * 16}px` }">
                      <span class="font-medium text-foreground block truncate">{{ (row.data as PipelineItem).name }}</span>
                    </td>
                    <td class="px-4 py-3">
                      <span v-if="(row.data as PipelineItem).description" class="text-muted-foreground truncate block max-w-xs">{{ (row.data as PipelineItem).description }}</span>
                      <span v-else class="text-muted-foreground/50 italic">{{ $t('views.PipelineListView.no_description') }}</span>
                    </td>
                    <td class="px-4 py-3">
                      <span class="badge text-xs" :class="(row.data as PipelineItem).visibility === 'org' ? 'badge-context-blue' : 'badge-context-purple'">
                        {{ (row.data as PipelineItem).visibility === 'org' ? 'Org' : 'Team' }}
                      </span>
                    </td>
                    <td class="px-4 py-3">
                      <span class="text-muted-foreground block truncate max-w-[8rem]">{{ getLastRun((row.data as PipelineItem).id) || '\u2014' }}</span>
                    </td>
                    <td class="px-4 py-3">
                      <span class="text-xs text-muted-foreground block truncate max-w-[7rem]">{{ getPipelineTrigger((row.data as PipelineItem).id) || '\u2014' }}</span>
                    </td>
                    <td class="px-4 py-3">
                      <span class="text-muted-foreground">{{ formatDate((row.data as PipelineItem).created_at) }}</span>
                    </td>
                    <td class="px-4 py-3">
                      <div class="flex justify-end items-center gap-1">
                        <button type="button" class="rounded p-1 hover:bg-accent" :aria-label="$t('views.PipelineListView.pipeline_actions')" data-testid="pipeline-list-action-menu" @click.stop="openActionMenu($event, row.data as PipelineItem)">
                          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="1"/><circle cx="12" cy="5" r="1"/><circle cx="12" cy="19" r="1"/></svg>
                        </button>
                      </div>
                    </td>
                  </tr>
                </template>
              </tbody>
              </table>
            </div>
          </div>
        </div>

      </main>
    </div>

      <!-- Move to Folder dialog -->
      <dialog
        ref="moveDialogRef"
        v-if="showMoveToFolder"
        open
        class="fixed inset-0 z-50 m-auto flex w-full max-w-md items-center justify-center border border-border bg-card p-6 shadow-lg"
        aria-modal="true"
        :aria-label="$t('views.PipelineListView.move_to_folder')"
        tabindex="-1"
      >
          <h3 class="mb-4 text-lg font-semibold">{{ $t('views.PipelineListView.move_to_folder') }}</h3>
          <div class="space-y-3">
            <button type="button"
              v-for="f in foldersList"
              :key="f.id"
              class="flex w-full items-center gap-2 rounded-lg border px-3 py-2 text-sm hover:bg-accent transition-colors text-left"
              :class="moveToFolderId === f.id ? 'border-primary bg-accent' : 'border-border'"
              @click="moveToFolderId = f.id"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="shrink-0 text-muted-foreground"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>
              {{ f.name }}
            </button>
            <button type="button"
              class="flex w-full items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm hover:bg-accent transition-colors text-left"
              :class="moveToFolderId === null ? 'border-primary bg-accent' : ''"
              @click="moveToFolderId = null"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="shrink-0 text-muted-foreground"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
              {{ $t('views.PipelineListView.no_folder') }}
            </button>
          </div>
          <div v-if="moveError" class="mt-4 rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
            {{ moveError }}
          </div>
          <div class="mt-4 flex justify-end gap-2">
            <button type="button" class="rounded-lg border border-input bg-background px-4 py-2 text-sm hover:bg-accent" @click="closeMoveToFolder">
              {{ $t('common.cancel') }}
            </button>
            <Button :disabled="moving" @click="handleMoveToFolder">
              {{ moving ? $t('common.saving') : $t('common.save') }}
            </Button>
          </div>
        </dialog>

      <!-- Rename dialog -->
      <dialog
        ref="renameDialogRef"
        v-if="showRenameDialog"
        open
        class="fixed inset-0 z-50 m-auto flex w-full max-w-md items-center justify-center border border-border bg-card p-6 shadow-lg"
        aria-modal="true"
        :aria-label="$t('views.PipelineListView.rename_pipeline')"
        tabindex="-1"
      >
          <h3 class="mb-4 text-lg font-semibold">{{ $t('views.PipelineListView.rename_pipeline') }}</h3>
          <div class="space-y-4">
            <div>
              <label for="pipelinelistview-field-1" class="mb-1 block text-sm font-medium">{{ $t('views.PipelineListView.name') }}</label>
              <input id="pipelinelistview-field-1"
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
                type="button"
                class="rounded-lg border border-input bg-background px-4 py-2 text-sm hover:bg-accent"
                @click="closeRename"
              >
                Cancel
              </button>
              <Button :disabled="!renameName.trim() || renaming" @click="handleRename">
                {{ renaming ? 'Saving...' : 'Save' }}
              </Button>
            </div>
          </div>
        </dialog>

      <!-- Delete confirmation dialog -->
      <dialog
        ref="deleteDialogRef"
        v-if="showDeleteConfirm"
        open
        class="fixed inset-0 z-50 m-auto flex w-full max-w-md items-center justify-center border border-border bg-card p-6 shadow-lg"
        aria-modal="true"
        :aria-label="$t('views.PipelineListView.delete_pipeline')"
        tabindex="-1"
      >
          <h3 class="mb-4 text-lg font-semibold text-destructive">{{ $t('views.PipelineListView.delete_pipeline') }}</h3>
          <p class="mb-4 text-sm text-muted-foreground">
            Are you sure? This permanently deletes the pipeline and all its runs.
          </p>
          <div v-if="deleteError" class="mb-4 rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
            {{ deleteError }}
          </div>
          <div class="flex justify-end gap-2">
            <button
              type="button"
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm hover:bg-accent"
              @click="closeDelete"
            >
              Cancel
            </button>
            <button
              type="button"
              class="rounded-lg bg-destructive px-4 py-2 text-sm font-medium text-destructive-foreground hover:bg-destructive/90"
              @click="handleDelete"
            >
              Delete
            </button>
          </div>
        </dialog>

      <Menu ref="actionMenuRef" :model="actionMenuItems" popup />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch, nextTick, type Ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import PageHeader from '../components/shared/PageHeader.vue'
import FilterBar from '../components/shared/FilterBar.vue'
import FolderTree from '../components/pipelines/FolderTree.vue'
import { useDataFetch } from '../composables/useDataFetch'
import { usePlanStore } from '../stores/planStore'
import { FOCUSABLE_SELECTOR, trapTabInElement } from '../composables/useFocusTrap'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { formatApiError } from '../lib/api/formatError'
import Button from 'primevue/button'
import Select from 'primevue/select'
import Menu from 'primevue/menu'
import { api } from '../lib/api/client'
import { useApi } from '../composables/useApi'
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
  folder_id?: string | null
  trigger_type?: string
}

interface TriggerItem {
  id: string
  pipeline_id: string
  trigger_type: string
  active: boolean
}

interface FolderItem {
  id: string
  organisation_id: string
  name: string
  parent_id: string | null
  sort_order: number
}

interface PipelineListResponse {
  items: PipelineItem[]
  total: number
  page: number
  page_size: number
}

const router = useRouter()
const planStore = usePlanStore()
const { t } = useI18n()
const { get, post: postUntyped, patch: patchUntyped } = useApi()

const selectedFolderId = ref<string | null>(null)

const { loading, error, data: pipelinesResp, load: loadPipelines } = useDataFetch<PipelineListResponse>(
  async () => {
    const params: Record<string, any> = { page_size: 100 }
    if (selectedFolderId.value) {
      params.folder_id = selectedFolderId.value
    }
    const response = await api.GET('/api/v1/pipelines', { params: { query: params } })
    return { data: response.data as unknown as PipelineListResponse | undefined, error: response.error }
  },
  { initialValue: { items: [] as PipelineItem[], total: 0, page: 1, page_size: 100 } },
)

const allPipelines = computed(() => pipelinesResp.value?.items ?? [])

const foldersList = ref<FolderItem[]>([])
const folderError = ref<string | null>(null)
const foldersReady = ref(false)

const triggerTypes = ref<Record<string, string>>({})
const lastRunDates = ref<Record<string, string>>({})

async function loadLastRunDates() {
  try {
    const response = await api.GET('/api/v1/runs', { params: { query: { page_size: 500, sort_by: 'created_at', sort_order: 'desc' } } })
    if (response.data) {
      const items = (response.data as any).items as any[]
      const map: Record<string, string> = {}
      for (const run of items) {
        if (run.pipeline_id && !map[run.pipeline_id]) {
          map[run.pipeline_id] = run.created_at
        }
      }
      lastRunDates.value = map
    }
  } catch {
    // last run dates are optional
  }
}

function getLastRun(pipelineId: string): string | undefined {
  const dateStr = lastRunDates.value[pipelineId]
  if (!dateStr) return undefined
  return formatDate(dateStr)
}

async function loadTriggers() {
  try {
    const response = await api.GET('/api/v1/triggers', { params: { query: { page_size: 500 } } })
    if (response.data) {
      const items = (response.data as any).items as TriggerItem[]
      const map: Record<string, string> = {}
      for (const t of items) {
        if (!map[t.pipeline_id]) {
          map[t.pipeline_id] = t.trigger_type
        }
      }
      triggerTypes.value = map
    }
  } catch {
    // triggers are optional — column shows '—' if unavailable
  }
}

function getPipelineTrigger(pipelineId: string): string | undefined {
  return triggerTypes.value[pipelineId]
}

watch(allPipelines, () => {
  loadTriggers()
  loadLastRunDates()
}, { immediate: true })

watch(selectedFolderId, () => {
  loadTriggers()
  loadLastRunDates()
})
const totalPipelineCount = ref(0)
const folderPipelineCounts = computed(() => {
  const counts: Record<string, number> = {}
  for (const p of allPipelines.value) {
    if (p.folder_id) {
      counts[p.folder_id] = (counts[p.folder_id] || 0) + 1
    }
  }
  counts.__all__ = totalPipelineCount.value
  return counts
})

async function loadTotalCount() {
  try {
    const response = await api.GET('/api/v1/pipelines', { params: { query: { page_size: 1 } } })
    if (response.data) {
      totalPipelineCount.value = (response.data as any).total ?? 0
    }
  } catch {
    // optional
  }
}

async function loadFolders() {
  folderError.value = null
  try {
    foldersList.value = await get<FolderItem[]>('/api/v1/pipeline-folders')
  } catch (e) {
    folderError.value = formatApiError(e)
    console.warn('Failed to load folders', e)
  } finally {
    foldersReady.value = true
  }
}

const folderNameMap = computed(() => {
  const map = new Map<string, string>()
  for (const f of foldersList.value) {
    map.set(f.id, f.name)
  }
  return map
})

const selectedFolderName = computed(() => {
  if (!selectedFolderId.value) return ''
  return folderNameMap.value.get(selectedFolderId.value) || ''
})

const mobileFolderSelectValue = computed<string>({
  get: () => selectedFolderId.value ?? '__all__',
  set: (val: string) => onSelectFolder(val === '__all__' ? null : val),
})

function onPipelineDragStart(pipeline: PipelineItem, event: DragEvent) {
  event.dataTransfer?.setData('text/plain', pipeline.id)
  event.dataTransfer!.effectAllowed = 'move'
}

function onTableFolderDrop(folderId: string, event: DragEvent) {
  const pipelineId = event.dataTransfer?.getData('text/plain')
  if (pipelineId) {
    onMovePipeline({ pipelineId, folderId })
  }
}

function onPipelineDrop(targetPipeline: PipelineItem, event: DragEvent) {
  const pipelineId = event.dataTransfer?.getData('text/plain')
  if (pipelineId && pipelineId !== targetPipeline.id) {
    onMovePipeline({ pipelineId, folderId: targetPipeline.folder_id ?? null })
  }
}

async function onMovePipeline(ev: { pipelineId: string; folderId: string | null }) {
  if (moving.value) return
  const pipeline = allPipelines.value.find(p => p.id === ev.pipelineId)
  if (!pipeline) return
  if ((pipeline.folder_id ?? null) === ev.folderId) return
  moveTarget.value = pipeline
  moveToFolderId.value = ev.folderId
  moveError.value = null
  await handleMoveToFolder()
}

function onSelectFolder(folderId: string | null) {
  selectedFolderId.value = folderId
  loadPipelines()
  loadFolders()
  loadTotalCount()
}

// Move to folder state
const showMoveToFolder = ref(false)
const moveTarget = ref<PipelineItem | null>(null)
const moveToFolderId = ref<string | null>(null)
const moving = ref(false)
const moveError = ref<string | null>(null)
const showRenameDialog = ref(false)
const renameTarget = ref<PipelineItem | null>(null)
const renameName = ref('')
const renameError = ref<string | null>(null)
const renaming = ref(false)
const showDeleteConfirm = ref(false)
const deleteTarget = ref<PipelineItem | null>(null)
const deleteError = ref<string | null>(null)

// Accessible modal dialog helpers: capture/restore focus, move initial focus
// into the first meaningful control of the open panel, and trap Tab focus
// within the active dialog using the shared focus-trap primitive.
const lastFocusedBeforeDialog = ref<HTMLElement | null>(null)
const moveDialogRef = ref<HTMLElement | null>(null)
const renameDialogRef = ref<HTMLElement | null>(null)
const deleteDialogRef = ref<HTMLElement | null>(null)
const activeDialogPanel = ref<HTMLElement | null>(null)

function openDialog(show: Ref<boolean>, panel: Ref<HTMLElement | null>) {
  lastFocusedBeforeDialog.value = document.activeElement as HTMLElement | null
  show.value = true
  nextTick(() => {
    if (!panel.value) return
    activeDialogPanel.value = panel.value
    // Focus the first meaningful control, not the tabindex="-1" wrapper, so
    // keyboard users see a focus indicator immediately and the trap guard
    // (which only fires for focusables inside the panel) stays armed.
    const first = panel.value.querySelector<HTMLElement>(FOCUSABLE_SELECTOR)
    if (first) first.focus()
    else panel.value.focus()
  })
}

function closeDialog(show: Ref<boolean>) {
  show.value = false
  activeDialogPanel.value = null
  nextTick(() => lastFocusedBeforeDialog.value?.focus())
}

function closeMoveToFolder() { closeDialog(showMoveToFolder) }
function closeRename() { closeDialog(showRenameDialog) }
function closeDelete() { closeDialog(showDeleteConfirm) }

function trapKeydown(e: KeyboardEvent) {
  if (!activeDialogPanel.value) return
  if (e.key === 'Escape') {
    e.preventDefault()
    if (showMoveToFolder.value) closeMoveToFolder()
    else if (showRenameDialog.value) closeRename()
    else if (showDeleteConfirm.value) closeDelete()
    return
  }
  if (e.key !== 'Tab') return
  trapTabInElement(e, activeDialogPanel.value)
}

onMounted(() => {
  document.addEventListener('keydown', trapKeydown)
})
onBeforeUnmount(() => {
  document.removeEventListener('keydown', trapKeydown)
})

const search = ref('')

const FOLDERS_STORAGE_KEY = 'modulo.pipelines.expandedFolders'

function loadExpandedFolders(): Set<string> {
  try {
    const raw = localStorage.getItem(FOLDERS_STORAGE_KEY)
    if (raw) return new Set(JSON.parse(raw))
  } catch {}
  return new Set()
}

function persistExpandedFolders() {
  try {
    localStorage.setItem(FOLDERS_STORAGE_KEY, JSON.stringify([...expandedFolders.value]))
  } catch {}
}

const expandedFolders = ref<Set<string>>(loadExpandedFolders())

interface TreeRow {
  type: 'folder' | 'pipeline' | 'uncategorised-header'
  depth: number
  data: PipelineItem | FolderItem | null
}

function toggleFolder(folderId: string) {
  const next = new Set(expandedFolders.value)
  if (next.has(folderId)) {
    next.delete(folderId)
  } else {
    next.add(folderId)
  }
  expandedFolders.value = next
  persistExpandedFolders()
}

function isFolderExpanded(folderId: string): boolean {
  return expandedFolders.value.has(folderId) || folderId === selectedFolderId.value
}

const pipelineFolderCount = computed(() => {
  const count = new Map<string, number>()
  for (const p of filteredPipelines.value) {
    if (p.folder_id) {
      count.set(p.folder_id, (count.get(p.folder_id) || 0) + 1)
    }
  }
  return count
})

const treeRows = computed<TreeRow[]>(() => {
  const rows: TreeRow[] = []
  const sortedFolders = [...foldersList.value].sort((a, b) => a.name.localeCompare(b.name))

  for (const folder of sortedFolders) {
    const pipelineCount = pipelineFolderCount.value.get(folder.id) || 0
    if (pipelineCount === 0) continue

    rows.push({ type: 'folder', depth: 0, data: folder })

    if (isFolderExpanded(folder.id)) {
      const folderPipelines = filteredPipelines.value
        .filter(p => p.folder_id === folder.id)
        .sort((a, b) => a.name.localeCompare(b.name))
      for (const p of folderPipelines) {
        rows.push({ type: 'pipeline', depth: 1, data: p })
      }
    }
  }

  const uncategorised = filteredPipelines.value
    .filter(p => !p.folder_id || !folderNameMap.value.has(p.folder_id))
    .sort((a, b) => a.name.localeCompare(b.name))

  if (uncategorised.length > 0) {
    rows.push({ type: 'uncategorised-header', depth: 0, data: null })
    for (const p of uncategorised) {
      rows.push({ type: 'pipeline', depth: 1, data: p })
    }
  }

  return rows
})

const filteredPipelines = computed(() => {
  const q = search.value.toLowerCase().trim()
  if (!q) return allPipelines.value
  return allPipelines.value.filter(p =>
    p.name.toLowerCase().includes(q) ||
    (p.description?.toLowerCase() ?? '').includes(q)
  )
})

function formatDate(dateStr: string): string {
  const d = new Date(dateStr)
  if (Number.isNaN(d.getTime())) return dateStr
  return formatDateShort(d)
}

function openPipeline(p: PipelineItem) {
  router.push({ name: 'pipeline-editor', params: { id: p.id } })
}

const actionMenuRef = ref<InstanceType<typeof Menu> | null>(null)
const actionMenuPipeline = ref<PipelineItem | null>(null)
const actionMenuItems = computed(() => {
  const p = actionMenuPipeline.value
  if (!p) return []
  return [
    { label: t('views.PipelineListView.runs'), command: () => router.push({ name: 'runs-list', query: { pipeline_id: p.id } }) },
    {
      label: t('views.PipelineListView.run_as_variant'),
      command: () => router.push({ path: '/variants/ab-test', query: { pipeline_id: p.id } }),
    },
    { label: t('views.PipelineListView.rename'), command: () => openRename(p) },
    ...(!p.archived_at
      ? [{ label: t('views.PipelineListView.archive'), command: () => handleArchive(p) }]
      : [{ label: t('views.PipelineListView.unarchive'), command: () => handleUnarchive(p) }]),
    { label: t('views.PipelineListView.move_to_folder'), command: () => openMoveToFolder(p) },
    ...(planStore.featureEnabled('pipeline_delete')
      ? [{ label: t('common.delete'), class: 'text-destructive', command: () => openDelete(p) }]
      : []),
  ]
})

function openActionMenu(event: MouseEvent, p: PipelineItem) {
  actionMenuPipeline.value = p
  actionMenuRef.value?.toggle(event)
}

function openRename(p: PipelineItem) {
  renameTarget.value = p
  renameName.value = p.name
  renameError.value = null
  openDialog(showRenameDialog, renameDialogRef)
}

function openMoveToFolder(p: PipelineItem) {
  moveTarget.value = p
  moveToFolderId.value = p.folder_id ?? null
  moveError.value = null
  openDialog(showMoveToFolder, moveDialogRef)
}

async function handleMoveToFolder() {
  if (!moveTarget.value) return
  moving.value = true
  moveError.value = null
  const targetId = moveTarget.value.id
  try {
    const folderId = moveToFolderId.value ?? null
    await patchUntyped(`/api/v1/pipelines/${targetId}/folder`, {
      folder_id: folderId,
    })
    closeMoveToFolder()
    moveTarget.value = null
    await loadPipelines()
    await loadFolders()
    await loadTotalCount()
  } catch (e: unknown) {
    moveError.value = formatApiError(e)
  } finally {
    moving.value = false
  }
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
    closeRename()
    await loadPipelines()
  } catch (e: unknown) {
    renameError.value = formatApiError(e)
  } finally {
    renaming.value = false
  }
}

async function handleArchive(p: PipelineItem) {
  try {
    await postUntyped(`/api/v1/pipelines/${p.id}/archive`)
    await loadPipelines()
  } catch (e) {
    error.value = formatApiError(e)
  }
}

async function handleUnarchive(p: PipelineItem) {
  try {
    await postUntyped(`/api/v1/pipelines/${p.id}/unarchive`)
    await loadPipelines()
  } catch (e) {
    error.value = formatApiError(e)
  }
}

function openDelete(p: PipelineItem) {
  deleteTarget.value = p
  deleteError.value = null
  openDialog(showDeleteConfirm, deleteDialogRef)
}

async function handleDelete() {
  if (!deleteTarget.value) return
  deleteError.value = null
  try {
    await api.DELETE('/api/v1/pipelines/{pipeline_id}', {
      params: { path: { pipeline_id: deleteTarget.value.id } },
    })
    closeDelete()
    deleteTarget.value = null
    router.push('/pipelines')
    await loadPipelines()
  } catch (e: unknown) {
    deleteError.value = formatApiError(e)
  }
}

onMounted(async () => {
  loadFolders()
  loadTotalCount()
})
</script>
