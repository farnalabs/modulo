<template>
  <div class="min-h-screen bg-background">
    <header class="bg-card border-b border-border px-6 py-4">
      <div class="max-w-6xl mx-auto flex items-center justify-between gap-3">
        <h1 class="text-xl font-semibold text-foreground">{{ $t('views.LibraryView.pipeline_templates') }}</h1>
        <div class="relative">
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground pointer-events-none"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
          <input
            v-model="search"
            type="text"
            :placeholder="$t('views.PipelineTemplateGallery.search_templates')"
            class="pl-9 pr-3 py-1.5 border border-input bg-background rounded-lg text-sm w-64"
            @input="debouncedSearch"
            data-testid="template-gallery-search"
          />
        </div>
      </div>
    </header>

    <main class="max-w-6xl mx-auto px-6 py-6 space-y-6">
      <div class="flex items-center gap-2 flex-wrap">
        <button
          v-for="cat in categories"
          :key="cat.value"
          class="px-4 py-1.5 text-sm font-medium rounded-full border transition-colors"
          :class="category === cat.value ? 'bg-primary text-primary-foreground border-primary' : 'border-input text-muted-foreground hover:bg-accent'"
          @click="category = cat.value; loadTemplates()"
          data-testid="template-gallery-category-tab"
        >
          {{ cat.label }}
        </button>
      </div>

      <div v-if="loading" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4" data-testid="template-gallery-skeleton">
        <div v-for="i in 6" :key="i" class="card p-5 animate-pulse">
          <div class="h-3 w-16 bg-muted rounded mb-3" />
          <div class="h-5 w-3/4 bg-muted rounded mb-2" />
          <div class="h-3 w-full bg-muted rounded mb-1" />
          <div class="h-3 w-2/3 bg-muted rounded mb-4" />
          <div class="flex gap-2 mb-4">
            <div class="h-5 w-14 bg-muted rounded" />
            <div class="h-5 w-14 bg-muted rounded" />
          </div>
          <div class="h-9 w-full bg-muted rounded" />
        </div>
      </div>

      <ErrorAlert v-else-if="error" :message="error" :on-retry="loadTemplates" class="mb-6" />

      <div v-else-if="templates.length === 0" class="text-center py-16">
        <svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1" class="mx-auto mb-4 text-muted-foreground/40"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
        <p class="text-lg font-medium text-foreground">{{ search || category ? 'No templates match your search' : 'No templates available' }}</p>
        <p class="text-sm text-muted-foreground mt-1">{{ search || category ? 'Try a different search term or clear the filter.' : 'Check back later for new templates.' }}</p>
      </div>

      <div v-else class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <div
          v-for="tpl in templates"
          :key="tpl.id"
          class="card card-hover p-5 flex flex-col"
          data-testid="template-gallery-card"
        >
          <div class="mb-3">
            <span class="badge" :class="categoryBadgeClass(tpl.category)">
              {{ tpl.category || 'Uncategorised' }}
            </span>
          </div>

          <h3 class="text-base font-medium text-foreground mb-1">{{ tpl.name }}</h3>

          <p v-if="tpl.description" class="text-sm text-muted-foreground mb-4 line-clamp-2 flex-1">
            {{ tpl.description }}
          </p>
          <div v-else class="mb-4 flex-1" />

          <div class="flex items-center gap-3 text-xs text-muted-foreground mb-4">
            <span class="flex items-center gap-1">
              <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
              {{ tpl.agent_count }} {{ tpl.agent_count === 1 ? 'agent' : 'agents' }}
            </span>
            <span class="flex items-center gap-1">
              <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
              {{ complexityLabel(tpl.agent_count) }}
            </span>
          </div>

          <div v-if="tpl.tags && tpl.tags.length > 0" class="flex items-center gap-2 flex-wrap mb-4">
            <span
              v-for="tag in tpl.tags.slice(0, 3)"
              :key="tag"
              class="text-xs bg-muted text-muted-foreground px-2 py-0.5 rounded"
            >
              {{ tag }}
            </span>
            <span v-if="tpl.tags.length > 3" class="text-xs text-muted-foreground">
              +{{ tpl.tags.length - 3 }}
            </span>
          </div>

          <button
            class="w-full px-3 py-2 bg-primary text-primary-foreground text-sm font-medium rounded-lg border border-primary/30 hover:border-primary/60 hover:brightness-110 transition-all"
            @click="openUseDialog(tpl)"
            data-testid="template-gallery-use-btn"
          >
            Use Template
          </button>
        </div>
      </div>

      <div v-if="total > pageSize && !loading" class="flex justify-center items-center gap-2 mt-8">
        <button
          :disabled="page <= 1"
          class="px-4 py-2 text-sm border border-input bg-background rounded-lg disabled:opacity-30 hover:bg-accent transition-colors"
          @click="prevPage"
          data-testid="template-gallery-prev-page"
        >
          Previous
        </button>
        <span class="px-4 py-2 text-sm text-muted-foreground">
          Page {{ page }} of {{ Math.ceil(total / pageSize) }}
        </span>
        <button
          :disabled="page >= Math.ceil(total / pageSize)"
          class="px-4 py-2 text-sm border border-input bg-background rounded-lg disabled:opacity-30 hover:bg-accent transition-colors"
          @click="nextPage"
          data-testid="template-gallery-next-page"
        >
          Next
        </button>
      </div>
    </main>

    <Teleport to="body">
      <div
        v-if="showDialog"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
        @click.self="showDialog = false"
        data-testid="template-gallery-dialog-overlay"
      >
        <div class="bg-card rounded-xl border border-border shadow-xl w-full max-w-lg mx-4 max-h-[90vh] overflow-y-auto">
          <div class="p-6">
            <div class="flex items-center justify-between mb-6">
              <h2 class="text-lg font-semibold text-foreground">{{ $t('views.PipelineTemplateGallery.use_template') }}</h2>
              <button
                class="text-muted-foreground hover:text-foreground transition-colors"
                @click="showDialog = false"
                data-testid="template-gallery-dialog-close"
              >
                <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
              </button>
            </div>

            <div class="card bg-muted/50 p-4 mb-6">
              <p class="text-sm font-medium text-foreground">{{ selectedTemplate?.name }}</p>
              <p v-if="selectedTemplate?.description" class="text-xs text-muted-foreground mt-1 line-clamp-2">{{ selectedTemplate?.description }}</p>
            </div>

            <div class="space-y-4">
              <div>
                <label class="block text-sm font-medium text-foreground mb-1">{{ $t('views.LibraryPipelineWizard.pipeline_name') }}</label>
                <input
                  v-model="pipelineName"
                  type="text"
                  class="w-full px-3 py-2 border border-input bg-background rounded-lg text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  :placeholder="$t('views.PipelineTemplateGallery.selectedtemplatename_pipeline')"
                  data-testid="template-gallery-dialog-name"
                />
              </div>

              <div>
                <label class="block text-sm font-medium text-foreground mb-1">{{ $t('views.PipelineTemplateGallery.target_ownership') }}</label>
                <OwnershipPicker v-model="ownership" :label="$t('views.LibraryPipelineWizard.owner')" />
              </div>

              <div>
                <label class="block text-sm font-medium text-foreground mb-1">{{ $t('views.PipelineTemplateGallery.initial_trigger') }}</label>
                <select
                  v-model="triggerType"
                  class="w-full px-3 py-2 border border-input bg-background rounded-lg text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  data-testid="template-gallery-dialog-trigger"
                >
                  <option value="manual">{{ $t('views.PipelineTemplateGallery.manual_run_on_demand') }}</option>
                  <option value="webhook">Webhook</option>
                  <option value="cron">{{ $t('views.PipelineTemplateGallery.cron_scheduled') }}</option>
                </select>
              </div>
            </div>

            <div class="flex items-center justify-end gap-3 mt-8">
              <button
                class="px-4 py-2 border border-input bg-background text-foreground text-sm font-medium rounded-lg hover:bg-accent transition-colors"
                @click="showDialog = false"
                data-testid="template-gallery-dialog-cancel"
              >
                Cancel
              </button>
              <button
                :disabled="creating"
                class="px-4 py-2 bg-primary text-primary-foreground text-sm font-medium rounded-lg hover:brightness-110 disabled:opacity-50 transition-all"
                @click="useTemplate"
                data-testid="template-gallery-dialog-confirm"
              >
                {{ creating ? 'Creating...' : 'Create Pipeline' }}
              </button>
            </div>

            <div
              v-if="createError"
              class="mt-4 rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-destructive text-sm"
            >
              {{ createError }}
            </div>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { ref, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { useApi } from '../composables/useApi'
import OwnershipPicker from '../components/OwnershipPicker.vue'
import type { OwnershipValue } from '../components/OwnershipPicker.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'

interface TemplateItem {
  id: string
  name: string
  description: string | null
  category: string | null
  tags: string[]
  agent_count: number
  preview_data: Record<string, unknown>
  created_at: string
  updated_at: string
}

interface TemplateListResponse {
  items: TemplateItem[]
  total: number
  page: number
  page_size: number
}

interface CreateFromTemplateResponse {
  pipeline_id: string
  pipeline_name: string
  agent_count: number
  edge_count: number
}

const router = useRouter()
const { get, post } = useApi()

const templates = ref<TemplateItem[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const search = ref('')
const category = ref('')
const page = ref(1)
const pageSize = ref(12)
const total = ref(0)

const showDialog = ref(false)
const selectedTemplate = ref<TemplateItem | null>(null)
const pipelineName = ref('')
const ownership = ref<OwnershipValue>({ owner_team_id: null, visibility: 'org' })
const triggerType = ref<'manual' | 'webhook' | 'cron'>('manual')
const creating = ref(false)
const createError = ref<string | null>(null)

const categories = [
  { label: 'All', value: '' },
  { label: 'SDLC', value: 'sdlc' },
  { label: 'DevOps', value: 'devops' },
  { label: 'Security', value: 'security' },
  { label: 'Data', value: 'data' },
  { label: 'Custom', value: 'custom' },
]

function categoryBadgeClass(cat: string | null): string {
  const map: Record<string, string> = {
    sdlc: 'badge-context-blue',
    devops: 'badge-context-teal',
    security: 'badge-context-amber',
    data: 'badge-context-purple',
    custom: 'badge-context-cyan',
  }
  return map[cat?.toLowerCase() ?? ''] ?? 'badge-context-slate'
}

function complexityLabel(agentCount: number): string {
  if (agentCount <= 2) return 'Simple'
  if (agentCount <= 5) return 'Moderate'
  return 'Complex'
}

let debounceTimer: ReturnType<typeof setTimeout> | null = null
function debouncedSearch() {
  if (debounceTimer) clearTimeout(debounceTimer)
  debounceTimer = setTimeout(() => {
    page.value = 1
    loadTemplates()
  }, 300)
}

async function loadTemplates() {
  loading.value = true
  error.value = null
  try {
    const params = new URLSearchParams({
      page: String(page.value),
      page_size: String(pageSize.value),
    })
    if (category.value) params.set('category', category.value)
    if (search.value) params.set('search', search.value)

    const data = await get<TemplateListResponse>(`/api/v1/templates?${params}`)
    templates.value = data.items
    total.value = data.total
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to load templates'
  } finally {
    loading.value = false
  }
}

function prevPage() {
  if (page.value > 1) {
    page.value--
    loadTemplates()
  }
}

function nextPage() {
  if (page.value < Math.ceil(total.value / pageSize.value)) {
    page.value++
    loadTemplates()
  }
}

function openUseDialog(tpl: TemplateItem) {
  selectedTemplate.value = tpl
  pipelineName.value = tpl.name
  ownership.value = { owner_team_id: null, visibility: 'org' }
  triggerType.value = 'manual'
  createError.value = null
  showDialog.value = true
}

async function useTemplate() {
  if (!selectedTemplate.value) return
  creating.value = true
  createError.value = null
  try {
    const data = await post<CreateFromTemplateResponse>(
      `/api/v1/pipelines/from-template/${selectedTemplate.value.id}`,
      {
        name: pipelineName.value || undefined,
        owner_team_id: ownership.value.owner_team_id,
        visibility: ownership.value.visibility,
        trigger_type: triggerType.value,
      },
    )
    showDialog.value = false
    router.push({ name: 'pipeline-editor', params: { id: data.pipeline_id } })
  } catch (e) {
    createError.value = e instanceof Error ? e.message : 'Failed to create pipeline from template'
  } finally {
    creating.value = false
  }
}

onBeforeUnmount(() => {
  if (debounceTimer) clearTimeout(debounceTimer)
})
</script>
