<template>
  <div class="min-h-screen">
    <header class="bg-card border-b border-border px-6 py-4">
      <div class="mx-auto flex items-center justify-between gap-3 max-w-6xl">
        <PageHeader :title="$t('views.LibraryView.title')" />
        <div class="flex items-center gap-3">
          <Button
            variant="default"
            as="router-link"
            to="/library?type=pipeline_template"
            class="px-4 py-1.5"
            data-testid="library-create-pipeline-header"
          >
            {{ $t('views.LibraryView.create_pipeline') }}
          </Button>
          <FilterBar
            :search="{ placeholder: $t('views.LibraryView.search_primitives') }"
            :search-value="search"
            @update:search="search = $event; onSearchInput()"
          />
          <div class="relative" ref="typeFilterRef">
            <button
              type="button"
              class="flex items-center gap-2 rounded-lg border border-input bg-background px-3 py-2 text-sm hover:bg-accent transition-colors"
              @click="showTypeDropdown = !showTypeDropdown"
              data-testid="library-type-filter-button"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg>
              {{ $t('views.AdminNotificationDeliveryLogView.all_types') }}
              <span v-if="selectedTypes.length > 0" class="flex h-5 w-5 items-center justify-center rounded-full bg-primary text-[10px] font-medium text-primary-foreground">{{ selectedTypes.length }}</span>
            </button>
            <div
              v-if="showTypeDropdown"
              class="absolute right-0 top-full z-50 mt-1 w-56 rounded-lg border bg-card p-2 shadow-lg"
              data-testid="library-type-filter-dropdown"
            >
              <label
                v-for="opt in typeOptions"
                :key="opt.value"
                class="flex items-center gap-2 rounded px-2 py-1.5 text-sm hover:bg-accent cursor-pointer"
              >
                <input
                  type="checkbox"
                  :checked="selectedTypes.includes(opt.value)"
                  class="rounded border-input"
                  @change="toggleType(opt.value)"
                />
                {{ opt.label }}
              </label>
            </div>
          </div>
        </div>
      </div>
    </header>

    <main class="page-wide">
      <div v-if="selectedTypes.length > 0" class="flex flex-wrap items-center gap-2 py-2">
        <span
          v-for="type in selectedTypes"
          :key="type"
          class="inline-flex items-center gap-1 rounded-full bg-primary/10 px-3 py-1 text-xs font-medium text-primary"
        >
          {{ typeLabel(type) }}
          <button
            type="button"
            class="ml-0.5 rounded-full p-0.5 hover:bg-primary/20 transition-colors"
            @click="removeType(type)"
            :aria-label="`Remove ${typeLabel(type)} filter`"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
          </button>
        </span>
        <button
          type="button"
          class="text-xs text-muted-foreground hover:text-foreground underline"
          @click="selectedTypes = []; onFilterChange()"
        >
          {{ $t('views.NotificationsPage.clear_filters') }}
        </button>
      </div>
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

      <EmptyState
        v-else-if="section === 'community' && communityPrimitives.length === 0"
        :title="$t('views.LibraryView.no_primitives_found')"
      />
      <EmptyState
        v-else-if="section === 'native' && nativePrimitives.length === 0 && previewPrimitives.length === 0"
        :title="$t('views.LibraryView.no_primitives_found')"
      />

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
            <span class="text-xs text-muted-foreground">{{ $t('views.LibraryView.auto_update') }}</span>
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
              variant="default"
              class="flex-1 px-3 py-2 border border-primary/30 hover:border-primary/60"
              @click="createPipeline(prim)"
              data-testid="library-create-pipeline"
            >
              {{ $t('views.LibraryView.create_pipeline') }}
            </Button>
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
              variant="default"
              class="flex-1 px-3 py-2 border border-primary/30 hover:border-primary/60"
              @click="createPipeline(prim)"
              data-testid="library-create-pipeline"
            >
              {{ $t('views.LibraryView.create_pipeline') }}
            </Button>
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

          <div class="flex items-center gap-2 mt-auto">
            <button
              v-if="prim.primitive_type === 'pipeline_template' || prim.primitive_type === 'composite'"
              variant="default"
              class="flex-1 px-3 py-2 border border-primary/30 hover:border-primary/60"
              @click="createPipeline(prim)"
              data-testid="library-create-pipeline"
            >
              {{ $t('views.LibraryView.create_pipeline') }}
            </Button>
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
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { Button } from '@/components/ui/button'
import PageHeader from '../components/shared/PageHeader.vue'
import FilterBar from '../components/shared/FilterBar.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import { useDataFetch } from '../composables/useDataFetch'
import { formatApiError } from '../lib/api/formatError'
import { api } from '../lib/api/client'

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

const router = useRouter()
const route = useRoute()

const search = ref('')
const selectedTypes = ref<string[]>([])
const showTypeDropdown = ref(false)
const typeFilterRef = ref<HTMLElement | null>(null)
const page = ref(1)
const pageSize = ref(12)
const total = ref(0)

const typeOptions = [
  { value: 'pipeline_template', label: 'Pipeline Templates' },
  { value: 'workflow', label: 'Workflows' },
  { value: 'agent', label: 'Agents' },
  { value: 'schema', label: 'Schemas' },
  { value: 'integration', label: 'Integrations' },
  { value: 'composite', label: 'Composites' },
]

const typeLabelMap = Object.fromEntries(typeOptions.map(o => [o.value, o.label]))

function typeLabel(type: string): string {
  return typeLabelMap[type] ?? type
}

function toggleType(value: string) {
  if (selectedTypes.value.includes(value)) {
    selectedTypes.value = selectedTypes.value.filter(t => t !== value)
  } else {
    selectedTypes.value = [...selectedTypes.value, value]
  }
  onFilterChange()
}

function removeType(value: string) {
  selectedTypes.value = selectedTypes.value.filter(t => t !== value)
  onFilterChange()
}

type LibrarySection = 'native' | 'community'
const section = ref<LibrarySection>('native')

const { loading, error, data: loadResp, load: loadPrimitives } = useDataFetch<ListResponse>(
  async () => {
    const params = new URLSearchParams({
      page: String(page.value),
      page_size: String(pageSize.value),
    })
    if (search.value) params.set('search', search.value)
    if (section.value === 'community') params.set('source', 'community')
    if (selectedTypes.value.length === 1) params.set('primitive_type', selectedTypes.value[0])

    const { data, error: err } = await api.GET('/api/v1/libraries', {
      params: { query: Object.fromEntries(params) as any },
    })
    if (err) return { data: undefined, error: err }
    return { data: data as unknown as ListResponse, error: undefined }
  },
  { initialValue: { items: [] as LibraryPrimitive[], total: 0, page: 1, page_size: 12 } },
)

const primitives = ref<LibraryPrimitive[]>([])

watch([loadResp, section], ([d]) => {
  if (d) {
    primitives.value = section.value === 'native' ? d.items.filter(p => p.source !== 'community') : d.items
    total.value = d.total
  }
}, { immediate: true })

function switchSection(next: LibrarySection) {
  if (section.value === next) return
  section.value = next
  page.value = 1
  loadPrimitives()
}

function applyTypeFilter(items: LibraryPrimitive[]): LibraryPrimitive[] {
  if (selectedTypes.value.length === 0) return items
  return items.filter(p => selectedTypes.value.includes(p.primitive_type))
}

const nativePrimitives = computed(() => applyTypeFilter(primitives.value.filter(p => (p.tier ?? 'native') !== 'preview' && (p.tier ?? 'native') !== 'in_dev')))
const previewPrimitives = computed(() => applyTypeFilter(primitives.value.filter(p => p.tier === 'preview')))
const communityPrimitives = computed(() => applyTypeFilter(primitives.value.filter(p => p.source === 'community')))

function onSearchInput() {
  page.value = 1
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(loadPrimitives, 300)
}

function onFilterChange() {
  page.value = 1
  showTypeDropdown.value = false
  loadPrimitives()
}

function onClickOutside(e: MouseEvent) {
  if (typeFilterRef.value && !typeFilterRef.value.contains(e.target as Node)) {
    showTypeDropdown.value = false
  }
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
    const { data } = await api.PATCH('/api/v1/libraries/{primitive_id}', {
      params: { path: { primitive_id: prim.id } },
      body: { auto_update: newValue },
    })
    const idx = primitives.value.findIndex(x => x.id === prim.id)
    if (idx !== -1 && data) primitives.value[idx] = data as unknown as LibraryPrimitive
  } catch (e) {
    error.value = formatApiError(e)
  } finally {
    toggleLoading.value[prim.id] = false
  }
}

onBeforeUnmount(() => {
  if (searchTimer) clearTimeout(searchTimer)
  document.removeEventListener('mousedown', onClickOutside)
})
onMounted(() => {
  const typeParam = route.query.type
  if (typeof typeParam === 'string' && typeParam) {
    selectedTypes.value = [typeParam]
  }
  document.addEventListener('mousedown', onClickOutside)
  loadPrimitives()
})
</script>
