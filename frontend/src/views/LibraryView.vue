<template>
  <div class="min-h-screen">
    <header class="bg-card border-b border-border px-6 py-4">
      <div class="mx-auto flex items-center justify-between gap-3 max-w-6xl">
        <PageHeader :title="$t('views.LibraryView.title')" />
        <div class="flex items-center gap-3">
          <Button as="router-link" to="/library?type=pipeline_template" class="px-4 py-1.5" data-testid="library-create-pipeline-header">
            {{ $t('views.LibraryView.create_pipeline') }}
          </Button>
          <FilterBar
            :search="{ placeholder: $t('views.LibraryView.search_primitives') }"
            :search-value="search"
            @update:search="search = $event"
          />
          <div class="relative" ref="typeFilterRef">
            <button
              type="button"
              class="flex items-center gap-2 rounded-lg border border-input bg-background px-3 py-2 text-sm hover:bg-accent transition-colors"
              @click="showTypeDropdown = !showTypeDropdown"
              data-testid="library-type-filter-button"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg>
              {{ $t('views.LibraryView.all_types') }}
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
                {{ $t(opt.labelKey) }}
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

      <div
        v-else-if="error"
        class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive"
        role="alert"
        data-testid="library-error"
      >
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
        <LibraryPrimitiveCard
          v-for="prim in nativePrimitives"
          :key="prim.id"
          :prim="prim"
          badge="modulo"
          show-auto-update
          :toggle-loading="toggleLoading"
          :adapting="adapting"
          @create-pipeline="createPipeline"
          @create-lifecycle-map="createLifecycleMap"
          @view-details="viewPrimitive"
          @toggle-auto-update="toggleAutoUpdate"
        />
      </div>

      <details v-if="section === 'native' && previewPrimitives.length > 0" class="rounded-lg border bg-card" data-testid="library-preview-section">
        <summary class="cursor-pointer px-4 py-3 text-sm font-medium text-muted-foreground hover:text-foreground">
          {{ $t('views.LibraryView.preview_integrations_count', { count: previewPrimitives.length }, previewPrimitives.length) }}
        </summary>
        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 border-t p-4">
          <LibraryPrimitiveCard
            v-for="prim in previewPrimitives"
            :key="prim.id"
            :prim="prim"
            badge="preview"
            :show-tags="false"
            :adapting="adapting"
            @create-pipeline="createPipeline"
            @create-lifecycle-map="createLifecycleMap"
            @view-details="viewPrimitive"
            @toggle-auto-update="toggleAutoUpdate"
          />
        </div>
      </details>

      <div v-if="section === 'community' && communityPrimitives.length > 0" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <LibraryPrimitiveCard
          v-for="prim in communityPrimitives"
          :key="prim.id"
          :prim="prim"
          badge="community"
          :adapting="adapting"
          @create-pipeline="createPipeline"
          @create-lifecycle-map="createLifecycleMap"
          @view-details="viewPrimitive"
          @toggle-auto-update="toggleAutoUpdate"
        />
      </div>

      <div v-if="total > pageSize" class="flex justify-center items-center gap-2 mt-8">
        <button type="button"
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
        <button type="button"
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
import { watchDebounced } from '@vueuse/core'
import { useRouter, useRoute } from 'vue-router'
import Button from 'primevue/button'
import PageHeader from '../components/shared/PageHeader.vue'
import FilterBar from '../components/shared/FilterBar.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import LibraryPrimitiveCard from '../components/library/LibraryPrimitiveCard.vue'
import { useDataFetch } from '../composables/useDataFetch'
import { formatApiError } from '../lib/api/formatError'
import { api } from '../lib/api/client'
import { useI18n } from 'vue-i18n'
import type { LibraryPrimitive } from '../components/library/LibraryPrimitiveCard.vue'

interface ListResponse {
  items: LibraryPrimitive[]
  total: number
  page: number
  page_size: number
}

const router = useRouter()
const route = useRoute()
const { t } = useI18n()

const search = ref('')
const selectedTypes = ref<string[]>([])
const showTypeDropdown = ref(false)
const typeFilterRef = ref<HTMLElement | null>(null)
const page = ref(1)
const pageSize = ref(12)
const total = ref(0)

const typeOptions = [
  { value: 'pipeline_template', labelKey: 'views.LibraryView.type_pipeline_templates' },
  { value: 'workflow', labelKey: 'views.LibraryView.type_workflows' },
  { value: 'agent', labelKey: 'views.LibraryView.type_agents' },
  { value: 'schema', labelKey: 'views.LibraryView.type_schemas' },
  { value: 'integration', labelKey: 'views.LibraryView.type_integrations' },
  { value: 'composite', labelKey: 'views.LibraryView.type_composites' },
  { value: 'lifecycle_map', labelKey: 'views.LibraryView.type_lifecycle_maps' },
]

function typeLabel(type: string): string {
  const key = typeLabelKey(type)
  const label = t(key)
  return label !== key ? label : type
}

function typeLabelKey(type: string): string {
  return typeOptions.find(o => o.value === type)?.labelKey ?? type
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
    if (selectedTypes.value.length > 1) params.set('primitive_types', selectedTypes.value.join(','))

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

watchDebounced(search, () => {
  page.value = 1
  loadPrimitives()
}, { debounce: 300 })

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

function createPipeline(prim: LibraryPrimitive) {
  router.push({ name: 'library-pipeline-wizard', params: { id: prim.id } })
}

const adapting = ref<Record<string, boolean>>({})

async function createLifecycleMap(prim: LibraryPrimitive): Promise<void> {
  if (adapting.value[prim.id]) return
  adapting.value[prim.id] = true
  error.value = null
  try {
    const { data, error: err } = await api.POST('/api/v1/libraries/{primitive_id}/create-lifecycle-map', {
      params: { path: { primitive_id: prim.id } },
    })
    if (err) {
      error.value = formatApiError(err)
      return
    }
    const created = data as unknown as { id: string }
    router.push({ name: 'lifecycle-map-detail', params: { id: created.id } })
  } catch (e) {
    error.value = formatApiError(e)
  } finally {
    adapting.value[prim.id] = false
  }
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
