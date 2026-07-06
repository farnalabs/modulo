<template>
  <div class="min-h-screen">
    <header class="bg-card border-b border-border px-6 py-4">
      <div class="max-w-6xl mx-auto flex items-center justify-between gap-3">
        <h1 class="text-xl font-semibold text-foreground">Library</h1>
        <div class="flex items-center gap-3">
          <router-link
            to="/templates"
            class="rounded-lg bg-primary px-4 py-1.5 text-sm font-semibold text-primary-foreground hover:brightness-110 transition-all"
            data-testid="library-create-pipeline-header"
          >
            Create Pipeline
          </router-link>
          <input
            v-model="search"
            type="text"
            :placeholder="$t('views.LibraryView.search_primitives')"
            class="input-teal px-3 py-1.5 border border-input bg-background rounded-lg text-sm"
            @input="onSearchInput"
            data-testid="library-search"
          />
          <select
            v-model="typeFilter"
            class="input-teal px-3 py-1.5 pr-8 border border-input bg-background rounded-lg text-sm"
            @change="onFilterChange"
            data-testid="library-type-filter"
          >
            <option value="">{{ $t('views.AdminNotificationDeliveryLogView.all_types') }}</option>
            <option value="pipeline_template">{{ $t('views.LibraryView.pipeline_templates') }}</option>
            <option value="workflow">{{ $t('views.LibraryView.type_workflows') }}</option>
            <option value="agent">{{ $t('views.LibraryView.type_agents') }}</option>
            <option value="schema">{{ $t('views.LibraryView.type_schemas') }}</option>
            <option value="integration">{{ $t('views.LibraryView.type_integrations') }}</option>
            <option value="composite">{{ $t('views.LibraryView.type_composites') }}</option>
          </select>
        </div>
      </div>
    </header>

    <main class="max-w-6xl mx-auto px-6 py-8 space-y-6">
      <div class="flex items-center gap-2 border-b border-border" role="tablist">
        <button
          type="button"
          role="tab"
          :aria-selected="section === 'native'"
          class="px-4 py-2 text-sm font-medium border-b-2 transition-colors"
          :class="section === 'native' ? 'border-primary text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground'"
          data-testid="library-section-native"
          @click="switchSection('native')"
        >
          {{ $t('views.LibraryView.native_library') }}
        </button>
        <button
          type="button"
          role="tab"
          :aria-selected="section === 'community'"
          class="px-4 py-2 text-sm font-medium border-b-2 transition-colors"
          :class="section === 'community' ? 'border-primary text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground'"
          data-testid="library-section-community"
          @click="switchSection('community')"
        >
          {{ $t('views.LibraryView.community_tab') }}
        </button>
      </div>

      <p v-if="section === 'community'" class="text-sm text-muted-foreground" data-testid="library-community-disclaimer">
        {{ $t('views.LibraryView.community_disclaimer') }}
      </p>

      <div v-if="loading" class="text-center py-12 text-muted-foreground">{{ $t('views.LibraryView.loading') }}</div>

      <div v-else-if="error" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive">
        {{ error }}
      </div>

      <div
        v-else-if="section === 'native' && nativePrimitives.length === 0 && previewPrimitives.length === 0"
        class="text-center py-12 text-muted-foreground"
      >
        {{ $t('views.LibraryView.no_primitives_found') }}
      </div>

      <div
        v-else-if="section === 'community' && communityPrimitives.length === 0"
        class="text-center py-12 text-muted-foreground"
      >
        {{ $t('views.LibraryView.no_primitives_found') }}
      </div>

      <div v-else-if="section === 'native' && nativePrimitives.length > 0" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <div
          v-for="prim in nativePrimitives"
          :key="prim.id"
          class="card card-hover p-5 flex flex-col"
          :data-testid="`library-item-${prim.id}`"
        >
          <div class="flex items-start justify-between mb-3">
            <div>
              <span :class="typeBadgeClass(prim.primitive_type)">
                {{ prim.primitive_type }}
              </span>
              <h3 class="mt-2 text-base font-medium text-foreground">{{ prim.name }}</h3>
            </div>
            <div v-if="prim.source === 'modulo'" class="text-xs text-primary font-medium bg-primary/10 px-2 py-0.5 rounded">
              {{ $t('views.LibraryView.modulo_badge') }}
            </div>
            <div
              v-else-if="prim.source === 'community'"
              class="text-xs text-muted-foreground font-medium bg-muted px-2 py-0.5 rounded"
              data-testid="library-community-badge"
            >
              {{ $t('views.LibraryView.community_badge') }}
            </div>
          </div>

          <p v-if="prim.description" class="text-sm text-muted-foreground flex-1 mb-4 line-clamp-2">
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

          <div class="flex items-center gap-2 mt-auto">
            <button
              v-if="prim.primitive_type === 'pipeline_template' || prim.primitive_type === 'composite'"
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

      <details v-if="section === 'native' && previewPrimitives.length > 0" class="rounded-lg border bg-card" data-testid="library-preview-section">
        <summary class="cursor-pointer px-4 py-3 text-sm font-medium text-muted-foreground hover:text-foreground">
          {{ $t('views.LibraryView.preview_integrations_count', { count: previewPrimitives.length }, previewPrimitives.length) }}
        </summary>
        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 border-t p-4">
          <div
            v-for="prim in previewPrimitives"
            :key="prim.id"
            class="card card-hover p-5 flex flex-col"
            :data-testid="`library-item-${prim.id}`"
          >
            <div class="flex items-start justify-between mb-3">
              <div>
                <span :class="typeBadgeClass(prim.primitive_type)">
                  {{ prim.primitive_type }}
                </span>
                <h3 class="mt-2 text-base font-medium text-foreground">{{ prim.name }}</h3>
              </div>
              <span class="badge badge-context-amber text-xs">{{ $t('views.LibraryView.preview_badge') }}</span>
            </div>

            <p v-if="prim.description" class="text-sm text-muted-foreground flex-1 mb-4 line-clamp-2">
              {{ prim.description }}
            </p>

            <div class="flex items-center gap-2 mt-auto">
            <button
              v-if="prim.primitive_type === 'pipeline_template' || prim.primitive_type === 'composite'"
              class="flex-1 px-3 py-2 bg-primary text-primary-foreground text-sm font-medium rounded-lg border border-primary/30 hover:border-primary/60 hover:brightness-110 transition-all"
              @click="createPipeline(prim)"
              data-testid="library-create-pipeline"
            >
              {{ $t('views.LibraryView.create_pipeline') }}
            </button>
            <button
              class="flex-1 px-3 py-2 border border-border bg-background text-foreground text-sm font-medium rounded-lg hover:bg-accent transition-colors"
              @click="viewPrimitive(prim)"
              data-testid="library-view-details"
            >
              {{ $t('views.LibraryView.view_details') }}
            </button>
            </div>
          </div>
        </div>
      </details>

      <div v-if="section === 'community' && communityPrimitives.length > 0" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <div
          v-for="prim in communityPrimitives"
          :key="prim.id"
          class="card card-hover p-5 flex flex-col"
          :data-testid="`library-item-${prim.id}`"
        >
          <div class="flex items-start justify-between mb-3">
            <div>
              <span :class="typeBadgeClass(prim.primitive_type)">
                {{ prim.primitive_type }}
              </span>
              <h3 class="mt-2 text-base font-medium text-foreground">{{ prim.name }}</h3>
            </div>
            <div
              class="text-xs text-muted-foreground font-medium bg-muted px-2 py-0.5 rounded"
              data-testid="library-community-badge"
            >
              Community — not verified
            </div>
          </div>

          <p v-if="prim.description" class="text-sm text-muted-foreground flex-1 mb-4 line-clamp-2">
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

          <div class="flex items-center gap-2 mt-auto">
            <button
              v-if="prim.primitive_type === 'pipeline_template' || prim.primitive_type === 'composite'"
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
          {{ $t('views.LibraryView.previous_page') }}
        </button>
        <span class="px-4 py-2 text-sm text-muted-foreground">
          {{ $t('views.LibraryView.page_of', { page: page, total: Math.ceil(total / pageSize) }) }}
        </span>
        <button
          :disabled="page >= Math.ceil(total / pageSize)"
          class="px-4 py-2 text-sm border border-input bg-background rounded-lg disabled:opacity-30 hover:bg-accent transition-colors"
          @click="nextPage"
          data-testid="library-next-page"
        >
          {{ $t('views.LibraryView.next_page') }}
        </button>
      </div>
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
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
  tier?: 'native' | 'preview' | 'in_dev'
  created_at: string
  updated_at: string
}

interface ListResponse {
  items: LibraryPrimitive[]
  total: number
  page: number
  page_size: number
}

const { t } = useI18n()
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

// 'native' = Modulo-maintained structural workflows + the org's own saved
// primitives (existing default library view). 'community' = opinionated,
// narrower example pipelines contributed by users (ADR 010 §2) — always
// fetched and rendered as a separate section, never mixed with Native.
type LibrarySection = 'native' | 'community'
const section = ref<LibrarySection>('native')

function switchSection(next: LibrarySection) {
  if (section.value === next) return
  section.value = next
  page.value = 1
  loadPrimitives()
}

// Within the Native section: in-dev primitives are hidden entirely; native
// items stay in the primary grid; preview items are segregated into a
// collapsed disclosure section. The Community section (source === 'community')
// bypasses this tier split entirely — community items aren't tiered.
const nativePrimitives = computed(() => primitives.value.filter(p => (p.tier ?? 'native') !== 'preview' && (p.tier ?? 'native') !== 'in_dev'))
const previewPrimitives = computed(() => primitives.value.filter(p => p.tier === 'preview'))
const communityPrimitives = computed(() => primitives.value.filter(p => p.source === 'community'))

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
    if (section.value === 'community') params.set('source', 'community')

    const data = await get<ListResponse>(`/api/v1/libraries?${params}`)
    // Community items are never mixed into the Native section, even though
    // the default (no `source` filter) API response merges all sources.
    primitives.value =
      section.value === 'native' ? data.items.filter((p) => p.source !== 'community') : data.items
    total.value = section.value === 'native' ? primitives.value.length : data.total
  } catch (e) {
    error.value = e instanceof Error ? e.message : t('views.LibraryView.failed_to_load_primitives')
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
