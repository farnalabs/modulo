<template>
  <div class="min-h-screen">
    <header class="bg-card border-b border-border px-6 py-4">
      <div class="max-w-6xl mx-auto flex items-center justify-between gap-3">
        <h1 class="text-xl font-semibold text-foreground">Library</h1>
        <div class="flex items-center gap-3">
          <input
            v-model="search"
            type="text"
            placeholder="Search primitives..."
            class="input-teal px-3 py-1.5 border border-input bg-background rounded-lg text-sm"
            @input="onSearchInput"
            data-testid="library-search"
          />
          <select
            v-model="typeFilter"
            class="input-teal px-3 py-1.5 border border-input bg-background rounded-lg text-sm"
            @change="onFilterChange"
            data-testid="library-type-filter"
          >
            <option value="">All Types</option>
            <option value="pipeline_template">Pipeline Templates</option>
            <option value="workflow">Workflows</option>
            <option value="agent">Agents</option>
            <option value="schema">Schemas</option>
            <option value="integration">Integrations</option>
            <option value="test_fixture">Test Fixtures</option>
            <option value="composite">Composites</option>
          </select>
        </div>
      </div>
    </header>

    <main class="max-w-6xl mx-auto px-6 py-8 space-y-6">
      <div v-if="loading" class="text-center py-12 text-muted-foreground">Loading...</div>

      <div v-else-if="error" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive">
        {{ error }}
      </div>

      <div v-else-if="primitives.length === 0" class="text-center py-12 text-muted-foreground">
        No primitives found.
      </div>

      <div v-else class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <div
          v-for="prim in primitives"
          :key="prim.id"
          class="card card-hover p-5"
        >
          <div class="flex items-start justify-between mb-3">
            <div>
              <span :class="typeBadgeClass(prim.primitive_type)">
                {{ prim.primitive_type }}
              </span>
              <h3 class="mt-2 text-base font-medium text-foreground">{{ prim.name }}</h3>
            </div>
            <div v-if="prim.source === 'modulo'" class="text-xs text-primary font-medium bg-primary/10 px-2 py-0.5 rounded">
              Modulo
            </div>
          </div>

          <p v-if="prim.description" class="text-sm text-muted-foreground mb-4 line-clamp-2">
            {{ prim.description }}
          </p>

          <div class="flex items-center gap-2 flex-wrap mb-4">
            <span
              v-for="tag in (prim.tags || []).slice(0, 3)"
              :key="tag"
              class="text-xs bg-muted text-muted-foreground px-2 py-0.5 rounded"
            >
              {{ tag }}
            </span>
            <span v-if="(prim.tags || []).length > 3" class="text-xs text-muted-foreground">
              +{{ prim.tags.length - 3 }}
            </span>
          </div>

          <div v-if="prim.forked_from" class="flex items-center gap-2 mb-3">
            <span class="text-xs text-muted-foreground">Auto-update</span>
            <button
              class="relative inline-flex h-5 w-9 items-center rounded-full transition-colors focus:outline-none disabled:opacity-50"
              :class="prim.auto_update ? 'bg-primary' : 'bg-muted'"
              role="switch"
              :aria-checked="prim.auto_update"
              :disabled="toggleLoading[prim.id]"
              @click="toggleAutoUpdate(prim)"
              :data-testid="`auto-update-toggle-${prim.id}`"
            >
              <span
                class="inline-block h-3.5 w-3.5 rounded-full bg-background transition-transform"
                :class="prim.auto_update ? 'translate-x-[18px]' : 'translate-x-[2px]'"
              />
            </button>
          </div>

          <div class="flex items-center gap-2">
            <button
              v-if="prim.primitive_type === 'pipeline_template'"
              class="flex-1 px-3 py-2 bg-primary text-primary-foreground text-sm font-medium rounded-lg border border-primary/30 hover:border-primary/60 hover:brightness-110 transition-all"
              @click="createPipeline(prim)"
              data-testid="library-create-pipeline"
            >
              Create Pipeline
            </button>
            <button
              class="flex-1 px-3 py-2 border border-border bg-background text-foreground text-sm font-medium rounded-lg hover:bg-accent transition-colors"
              @click="viewPrimitive(prim)"
              data-testid="library-view-details"
            >
              View Details
            </button>
          </div>
        </div>
      </div>

      <div v-if="total > pageSize" class="flex justify-center items-center gap-2 mt-8">
        <button
          :disabled="page <= 1"
          class="px-4 py-2 text-sm border border-input bg-background rounded-lg disabled:opacity-30 hover:bg-accent transition-colors"
          @click="prevPage"
          data-testid="library-previous-page"
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
          data-testid="library-next-page"
        >
          Next
        </button>
      </div>
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { useApi } from '../composables/useApi'

let searchTimer: ReturnType<typeof setTimeout> | null = null

interface LibraryPrimitive {
  id: string
  organisation_id: string
  source: string
  primitive_type: string
  name: string
  slug: string
  description: string | null
  author: string
  version: string
  tags: string[]
  visibility: string
  forked_from: string | null
  auto_update: boolean
  created_at: string
  updated_at: string
}

interface ListResponse {
  items: LibraryPrimitive[]
  total: number
  page: number
  page_size: number
}

const router = useRouter()
const { get, patch } = useApi()

const primitives = ref<LibraryPrimitive[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const search = ref('')
const typeFilter = ref('')
const page = ref(1)
const pageSize = ref(12)
const total = ref(0)

async function loadPrimitives() {
  loading.value = true
  error.value = null
  try {
    const params = new URLSearchParams({
      page: String(page.value),
      page_size: String(pageSize.value),
    })
    if (typeFilter.value) params.set('primitive_type', typeFilter.value)
    if (search.value) params.set('search', search.value)

    const data = await get<ListResponse>(`/api/v1/libraries?${params}`)
    primitives.value = data.items
    total.value = data.total
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to load primitives'
  } finally {
    loading.value = false
  }
}

function onSearchInput() {
  page.value = 1
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(loadPrimitives, 300)
}

function onFilterChange() {
  page.value = 1
  loadPrimitives()
}

function prevPage() {
  if (page.value > 1) {
    page.value--
    loadPrimitives()
  }
}

function nextPage() {
  if (page.value < Math.ceil(total.value / pageSize.value)) {
    page.value++
    loadPrimitives()
  }
}

function typeBadgeClass(type: string): string {
  const map: Record<string, string> = {
    pipeline_template: 'badge badge-context-blue',
    workflow: 'badge badge-context-teal',
    agent: 'badge badge-context-purple',
    schema: 'badge badge-context-amber',
    integration: 'badge badge-context-cyan',
    test_fixture: 'badge badge-context-pink',
    composite: 'badge badge-context-green',
  }
  return map[type] ?? 'badge badge-context-slate'
}

function createPipeline(prim: LibraryPrimitive) {
  router.push({ name: 'library-pipeline-wizard', params: { id: prim.id } })
}

function viewPrimitive(prim: LibraryPrimitive) {
  router.push({ name: 'library-pipeline-wizard', params: { id: prim.id } })
}

const toggleLoading = ref<Record<string, boolean>>({})

async function toggleAutoUpdate(prim: LibraryPrimitive) {
  const newValue = !prim.auto_update
  toggleLoading.value[prim.id] = true
  try {
    const data = await patch<LibraryPrimitive>(`/api/v1/libraries/${prim.id}`, { auto_update: newValue })
    const idx = primitives.value.findIndex(x => x.id === prim.id)
    if (idx !== -1) primitives.value[idx] = data
  } catch (e) {
    const msg = e instanceof Error ? e.message : 'Failed to toggle auto-update'
    error.value = msg
  } finally {
    toggleLoading.value[prim.id] = false
  }
}

onBeforeUnmount(() => {
  if (searchTimer) clearTimeout(searchTimer)
})
onMounted(loadPrimitives)
</script>
