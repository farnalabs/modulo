<template>
  <div class="min-h-screen bg-background">
    <header class="bg-card border-b border-border px-6 py-4">
      <div class="mx-auto max-w-6xl">
        <PageHeader :title="$t('views.LifecycleMapList.title')">
          <template #right>
            <FilterBar
              :search="{ placeholder: $t('views.LifecycleMapList.search_placeholder') }"
              :search-value="search"
              @update:search="search = $event; page = 1"
            >
              <template #after>
                <Select
                  class="w-full sm:w-auto"
                  :aria-label="$t('views.LifecycleMapList.filter_owner_aria')"
                  v-model="ownerFilter"
                  :placeholder="$t('views.LifecycleMapList.filter_owner')"
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
            <Button @click="handleNewMap" data-testid="lifecycle-map-list-new">
              <Plus :size="14" aria-hidden="true" class="mr-1" />
              {{ $t('views.LifecycleMapList.new_map') }}
            </Button>
          </template>
        </PageHeader>
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
        :title="$t('views.LifecycleMapList.empty_search_title')"
        :description="$t('views.LifecycleMapList.empty_search_description')"
      />

      <EmptyState
        v-else-if="allMaps.length === 0"
        :title="$t('views.LifecycleMapList.empty_title')"
        :description="$t('views.LifecycleMapList.empty_description')"
      >
        <Button class="w-full sm:w-auto" @click="handleNewMap" data-testid="lifecycle-map-list-empty-new">
          <Plus :size="14" aria-hidden="true" class="mr-1" />
          {{ $t('views.LifecycleMapList.empty_create_map') }}
        </Button>
      </EmptyState>

      <div v-else class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <div
          v-for="m in pagedMaps"
          :key="m.id"
          class="card card-hover w-full p-5 text-left cursor-pointer"
          role="button"
          tabindex="0"
          @click="openMap(m)"
          @keydown.enter="openMap(m)"
          @keydown.space.prevent="openMap(m)"
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
                <Pencil :size="12" aria-hidden="true" />
              </button>
            </div>
          </div>

          <p v-if="m.description" class="text-sm text-muted-foreground mb-3 line-clamp-2">
            {{ m.description }}
          </p>
          <div v-else class="mb-8" />

          <div class="flex items-center gap-3 text-xs text-muted-foreground mb-3">
            <span class="flex items-center gap-1">
              <Clock :size="12" aria-hidden="true" />
              {{ $t('views.LifecycleMapList.stages_count', { count: m.stage_count }) }}
            </span>
            <span v-if="m.graduated_count > 0" class="flex items-center gap-1">
              <Star :size="12" aria-hidden="true" class="fill-amber-500 text-amber-500" />
              {{ $t('views.LifecycleMapList.graduated_count', { count: m.graduated_count }) }}
            </span>
          </div>

          <div class="flex items-center justify-between text-xs text-muted-foreground pt-3 border-t border-border">
            <span class="flex items-center gap-1">
              <User :size="10" aria-hidden="true" />
              {{ m.owner || $t('views.LifecycleMapList.owner_fallback') }}
            </span>
            <span>{{ $t('views.LifecycleMapList.updated', { date: formatDate(m.updated_at) }) }}</span>
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
          {{ $t('views.LifecycleMapList.previous') }}
        </button>
        <span class="px-4 py-2 text-sm text-muted-foreground">
          {{ $t('views.LifecycleMapList.page_of', { page, total: totalPages }) }}
        </span>
        <button type="button"
          :disabled="page >= totalPages"
          class="px-4 py-2 text-sm border border-input bg-background rounded-lg disabled:opacity-30 hover:bg-accent transition-colors"
          @click="nextPage"
          data-testid="lifecycle-map-list-next-page"
        >
          {{ $t('views.LifecycleMapList.next') }}
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
              :placeholder="$t('views.LifecycleMapList.name_placeholder')"
              data-testid="lifecycle-map-list-create-name"
            />
          </div>
          <div>
            <label for="lifecyclemaplist-field-1" class="mb-1 block text-sm font-medium">{{ $t('views.LifecycleMapList.description') }}</label>
            <textarea id="lifecyclemaplist-field-1"
              v-model="newDescription"
              @keydown.space.stop
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              rows="3"
              :placeholder="$t('views.LifecycleMapList.description_placeholder')"
              data-testid="lifecycle-map-list-create-description"
            />
          </div>
          <div v-if="createError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
            {{ createError }}
          </div>
          <div class="flex justify-end gap-2">
            <button type="button"
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm hover:bg-accent"
              @click="showCreateDialog = false"
              data-testid="lifecycle-map-list-create-cancel"
            >
              {{ $t('views.LifecycleMapList.cancel') }}
            </button>
            <Button :disabled="!newName.trim() || creating" @click="handleCreateConfirm" data-testid="lifecycle-map-list-create-submit">
              {{ creating ? $t('views.LifecycleMapList.creating') : $t('views.LifecycleMapList.create') }}
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
import { Clock, Pencil, Plus, Star, User } from '@lucide/vue'
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
import Select from '../../components/shared/AppSelect.vue'

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
