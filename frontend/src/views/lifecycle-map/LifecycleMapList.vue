<template>
  <div class="min-h-screen bg-background">
    <header class="bg-card border-b border-border px-6 py-4">
      <div class="mx-auto flex items-center justify-between gap-3 max-w-6xl">
        <PageHeader title="Lifecycle Maps" />
        <FilterBar
          :search="{ placeholder: 'Search maps...' }"
          :search-value="search"
          @update:search="search = $event; page = 1"
        >
          <template #after>
            <Select
  aria-label="Form control"
  v-model="ownerFilter"
  placeholder="All teams"
  data-testid="lifecycle-map-list-owner-filter"
  :options="uniqueOwners.map(owner => ({ value: owner, label: owner }))"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
          </template>
        </FilterBar>
          <Button class="cursor-pointer" @click="handleNewMap" data-testid="lifecycle-map-list-new">
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="mr-1"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
            New Map
          </Button>
        </div>
    </header>

    <main class="page-wide">
      <div v-if="store.isLoading" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <div v-for="i in 6" :key="i" class="card p-5 animate-pulse">
          <div class="h-5 w-3/4 bg-muted rounded mb-2" />
          <div class="h-3 w-full bg-muted rounded mb-1" />
          <div class="h-3 w-2/3 bg-muted rounded mb-4" />
          <div class="h-4 w-16 bg-muted rounded mb-3" />
          <div class="h-8 w-full bg-muted rounded" />
        </div>
      </div>

      <ErrorAlert v-else-if="store.error" :message="store.error" :on-retry="loadMaps" class="mb-6" />

      <EmptyState
        v-else-if="filteredMaps.length === 0 && search"
        title="No maps match your search"
        description="Try a different search term or clear the filters."
      />

      <EmptyState
        v-else-if="allMaps.length === 0"
        title="No Lifecycle Maps yet"
        description="Create one to model your SDLC."
      >
        <Button class="cursor-pointer" @click="handleNewMap" data-testid="lifecycle-map-list-empty-new">
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="mr-1"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          Create Map
        </Button>
      </EmptyState>

      <div v-else class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <div role="button" tabindex="0" @keydown.enter="($event.currentTarget as HTMLElement).click()" @keydown.space.prevent="($event.currentTarget as HTMLElement).click()"
          v-for="m in pagedMaps"
          :key="m.id"
          class="card card-hover p-5 cursor-pointer"
          @click="openMap(m)"
          data-testid="lifecycle-map-list-card"
        >
          <div class="flex items-start justify-between gap-2 mb-2">
            <h3 class="text-base font-medium text-foreground truncate">{{ m.name }}</h3>
            <div class="flex shrink-0 items-center gap-1">
              <span class="rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                v{{ m.current_version }}
              </span>
              <button type="button"
                class="rounded p-1 text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
                :aria-label="$t('views.LifecycleMapList.edit')"
                data-testid="lifecycle-map-list-edit"
                @click.stop="editMap(m)"
                @keydown.enter.stop
                @keydown.space.prevent.stop
              >
                <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/><path d="m15 5 4 4"/></svg>
              </button>
            </div>
          </div>

          <p v-if="m.description" class="text-sm text-muted-foreground mb-3 line-clamp-2">
            {{ m.description }}
          </p>
          <div v-else class="mb-8" />

          <div class="flex items-center gap-3 text-xs text-muted-foreground mb-3">
            <span class="flex items-center gap-1">
              <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>
              {{ m.stage_count }} stages
            </span>
            <span v-if="m.graduated_count > 0" class="flex items-center gap-1">
              <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="currentColor" class="text-amber-500"><path d="M12 2L15.09 8.26L22 9.27L17 14.14L18.18 21.02L12 17.77L5.82 21.02L7 14.14L2 9.27L8.91 8.26L12 2Z"/></svg>
              {{ m.graduated_count }} graduated
            </span>
          </div>

          <div class="flex items-center justify-between text-xs text-muted-foreground pt-3 border-t border-border">
            <span v-if="m.owner" class="flex items-center gap-1">
              <svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/></svg>
              {{ m.owner }}
            </span>
            <span>Updated {{ formatDate(m.updated_at) }}</span>
          </div>
        </div>
      </div>

      <div v-if="totalPages > 1 && !store.isLoading" class="flex justify-center items-center gap-2 mt-8">
        <button type="button"
          :disabled="page <= 1"
          class="px-4 py-2 text-sm border border-input bg-background rounded-lg disabled:opacity-30 hover:bg-accent transition-colors"
          @click="prevPage"
          data-testid="lifecycle-map-list-prev-page"
        >
          Previous
        </button>
        <span class="px-4 py-2 text-sm text-muted-foreground">
          Page {{ page }} of {{ totalPages }}
        </span>
        <button type="button"
          :disabled="page >= totalPages"
          class="px-4 py-2 text-sm border border-input bg-background rounded-lg disabled:opacity-30 hover:bg-accent transition-colors"
          @click="nextPage"
          data-testid="lifecycle-map-list-next-page"
        >
          Next
        </button>
      </div>
    </main>

    <!-- Create dialog -->
    <div role="button" tabindex="0" @keydown.enter="($event.currentTarget as HTMLElement).click()" @keydown.space.prevent="($event.currentTarget as HTMLElement).click()"
      v-if="showCreateDialog"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      @click.self="showCreateDialog = false"
    >
      <div class="w-full max-w-md rounded-lg border bg-card p-6 shadow-lg">
        <h3 class="mb-4 text-base font-semibold">{{ $t('views.LifecycleMapList.create_lifecycle_map') }}</h3>
        <div class="space-y-4">
          <div>
            <label for="lifecyclemaplist-field-2" class="mb-1 block text-sm font-medium">{{ $t('views.LifecycleMapList.name') }}</label>
            <input id="lifecyclemaplist-field-2"
              v-model="newName"
              @keydown.space.stop
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              placeholder="My Delivery Lifecycle"
            />
          </div>
          <div>
            <label for="lifecyclemaplist-field-1" class="mb-1 block text-sm font-medium">{{ $t('views.LifecycleMapList.description') }}</label>
            <textarea id="lifecyclemaplist-field-1"
              v-model="newDescription"
              @keydown.space.stop
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              rows="3"
              placeholder="Optional description"
            />
          </div>
          <div v-if="createError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
            {{ createError }}
          </div>
          <div class="flex justify-end gap-2">
            <button type="button"
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm hover:bg-accent"
              @click="showCreateDialog = false"
            >
              Cancel
            </button>
            <Button :disabled="!newName.trim() || creating" @click="handleCreateConfirm">
              {{ creating ? 'Creating...' : 'Create' }}
            </Button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import PageHeader from '../../components/shared/PageHeader.vue'
import FilterBar from '../../components/shared/FilterBar.vue'
import { useLifecycleMapsStore } from '../../stores/lifecycleMaps'
import ErrorAlert from '../../components/shared/ErrorAlert.vue'
import EmptyState from '../../components/shared/EmptyState.vue'
import Button from 'primevue/button'
import type { LifecycleMapSummary } from '../../stores/lifecycleMaps'
import { formatDateShort } from '../../lib/formatDate'
import { useApi } from '../../composables/useApi'
import { formatApiError } from '../../lib/api/formatError'
import Select from 'primevue/select'

const router = useRouter()
const route = useRoute()
const store = useLifecycleMapsStore()
const { post } = useApi()

const search = ref('')
const ownerFilter = ref('')
const page = ref(1)
const pageSize = 12

const allMaps = computed(() => store.maps)

const uniqueOwners = computed(() => {
  const owners = new Set(store.maps.map((m) => m.owner).filter((o): o is string => !!o))
  return Array.from(owners).sort()
})

const filteredMaps = computed(() => {
  let result = allMaps.value
  const q = search.value.toLowerCase().trim()
  if (q) {
    result = result.filter((m) =>
      m.name.toLowerCase().includes(q) ||
      (m.description?.toLowerCase() ?? '').includes(q)
    )
  }
  if (ownerFilter.value) {
    result = result.filter((m) => m.owner === ownerFilter.value)
  }
  return result
})

const totalPages = computed(() => Math.max(1, Math.ceil(filteredMaps.value.length / pageSize)))

const pagedMaps = computed(() => {
  const start = (page.value - 1) * pageSize
  return filteredMaps.value.slice(start, start + pageSize)
})

async function loadMaps(): Promise<void> {
  await store.fetchMaps()
}

function prevPage(): void {
  page.value--
}

function nextPage(): void {
  page.value++
}

function formatDate(dateStr: string): string {
  const d = new Date(dateStr)
  if (Number.isNaN(d.getTime())) return dateStr
  return formatDateShort(d)
}

function openMap(m: LifecycleMapSummary): void {
  router.push(`/lifecycle-maps/${m.id}`)
}

function editMap(m: LifecycleMapSummary): void {
  router.push({ name: 'lifecycle-map-editor', params: { id: m.id } })
}

const showCreateDialog = ref(false)
const newName = ref('')
const newDescription = ref('')
const creating = ref(false)
const createError = ref<string | null>(null)

function handleNewMap(): void {
  newName.value = ''
  newDescription.value = ''
  createError.value = null
  showCreateDialog.value = true
}

async function handleCreateConfirm(): Promise<void> {
  if (!newName.value.trim()) return
  creating.value = true
  createError.value = null
  try {
    const data = await post<LifecycleMapSummary>('/api/v1/lifecycle-maps', {
        name: newName.value.trim(),
        description: newDescription.value.trim() || null,
    })
    showCreateDialog.value = false
    if (data) router.push({ name: 'lifecycle-map-editor', params: { id: (data as LifecycleMapSummary).id } })
  } catch (e: unknown) {
    createError.value = formatApiError(e)
  } finally {
    creating.value = false
  }
}

onMounted(async () => {
  await loadMaps()
  if (route.query.create === 'true') {
    handleNewMap()
  }
})
</script>
