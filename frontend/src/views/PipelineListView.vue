<template>
  <div class="min-h-screen bg-background">
    <header class="bg-card border-b border-border px-6 py-4">
      <div class="max-w-6xl mx-auto flex items-center justify-between gap-3">
        <h1 class="text-xl font-semibold text-foreground">Pipelines</h1>
        <div class="relative">
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground pointer-events-none"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
          <input
            v-model="search"
            type="text"
            :placeholder="$t('views.PipelineListView.search_pipelines')"
            class="pl-9 pr-3 py-1.5 border border-input bg-background rounded-lg text-sm w-64"
            @input="page = 1"
            data-testid="pipeline-list-search"
          />
        </div>
      </div>
    </header>

    <main class="max-w-6xl mx-auto px-6 py-6 space-y-6">
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
          <router-link
            to="/library"
            class="px-4 py-2 bg-primary text-primary-foreground text-sm font-medium rounded-lg hover:brightness-110 transition-all"
            data-testid="pipeline-list-browse-library"
          >
            Browse Library
          </router-link>
          <router-link
            to="/pipelines/copy"
            class="px-4 py-2 border border-input bg-background text-foreground text-sm font-medium rounded-lg hover:bg-accent transition-colors"
            data-testid="pipeline-list-copy-pipeline"
          >
            Copy Pipeline
          </router-link>
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
            <span
              class="shrink-0 badge text-xs"
              :class="p.visibility === 'org' ? 'badge-context-blue' : 'badge-context-purple'"
              data-testid="pipeline-list-visibility-badge"
            >
              {{ p.visibility === 'org' ? 'Org' : 'Team' }}
            </span>
          </div>

          <p v-if="p.description" class="text-sm text-muted-foreground mb-4 line-clamp-2">
            {{ p.description }}
          </p>
          <div v-else class="mb-4" />

          <div class="flex items-center gap-3 text-xs text-muted-foreground">
            <span class="flex items-center gap-1">
              <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 8v4l3 3"/><circle cx="12" cy="12" r="10"/></svg>
              Created {{ formatDate(p.created_at) }}
            </span>
          </div>

          <div class="mt-4 pt-3 border-t border-border">
            <button
              class="w-full px-3 py-2 bg-primary text-primary-foreground text-sm font-medium rounded-lg border border-primary/30 hover:border-primary/60 hover:brightness-110 transition-all"
              data-testid="pipeline-list-open-editor"
            >
              Open in Editor
            </button>
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
          Previous
        </button>
        <span class="px-4 py-2 text-sm text-muted-foreground">
          Page {{ page }} of {{ totalPages }}
        </span>
        <button
          :disabled="page >= totalPages"
          class="px-4 py-2 text-sm border border-input bg-background rounded-lg disabled:opacity-30 hover:bg-accent transition-colors"
          @click="nextPage"
          data-testid="pipeline-list-next-page"
        >
          Next
        </button>
      </div>
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useApi } from '../composables/useApi'
import ErrorAlert from '../components/shared/ErrorAlert.vue'

interface PipelineItem {
  id: string
  organisation_id: string
  name: string
  description: string | null
  visibility: string
  created_at: string
  updated_at: string
}

interface PipelineListResponse {
  items: PipelineItem[]
  total: number
  page: number
  page_size: number
}

const router = useRouter()
const { get } = useApi()

const allPipelines = ref<PipelineItem[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
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

async function loadPipelines() {
  loading.value = true
  error.value = null
  try {
    const data = await get<PipelineListResponse>('/api/v1/pipelines?page_size=100')
    allPipelines.value = data.items || []
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to load pipelines'
  } finally {
    loading.value = false
  }
}

function prevPage() {
  page.value--
}

function nextPage() {
  page.value++
}

function formatDate(dateStr: string): string {
  const d = new Date(dateStr)
  if (isNaN(d.getTime())) return dateStr
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
}

function openPipeline(p: PipelineItem) {
  router.push({ name: 'pipeline-editor', params: { id: p.id } })
}

onMounted(loadPipelines)
</script>
