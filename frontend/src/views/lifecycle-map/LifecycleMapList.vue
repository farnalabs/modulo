<template>
  <div class="min-h-screen bg-background">
    <header class="bg-card border-b border-border px-6 py-4">
      <div class="mx-auto flex items-center justify-between gap-3 max-w-6xl">
        <PageHeader title="Lifecycle Maps" />
        <div class="flex items-center gap-3">
          <div class="relative">
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground pointer-events-none"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
            <input
              v-model="search"
              type="text"
              placeholder="Search maps..."
              class="pl-9 pr-3 py-1.5 border border-input bg-background rounded-lg text-sm w-64"
              @input="page = 1"
              data-testid="lifecycle-map-list-search"
            />
          </div>
          <select
            v-model="ownerFilter"
            class="rounded-lg border border-input bg-background px-3 py-1.5 text-sm"
            data-testid="lifecycle-map-list-owner-filter"
          >
            <option value="">All teams</option>
            <option v-for="owner in uniqueOwners" :key="owner" :value="owner">
              {{ owner }}
            </option>
          </select>
          <Button
            variant="default"
            as="router-link"
            to="/lifecycle-maps/new"
            data-testid="lifecycle-map-list-new"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="mr-1"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
            New Map
          </Button>
        </div>
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
        <Button
          variant="default"
          as="router-link"
          to="/lifecycle-maps/new"
          data-testid="lifecycle-map-list-empty-new"
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="mr-1"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          Create Map
        </Button>
      </EmptyState>

      <div v-else class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <div
          v-for="m in pagedMaps"
          :key="m.id"
          class="card card-hover p-5 cursor-pointer"
          @click="openMap(m)"
          data-testid="lifecycle-map-list-card"
        >
          <div class="flex items-start justify-between gap-2 mb-2">
            <h3 class="text-base font-medium text-foreground truncate">{{ m.name }}</h3>
            <span class="shrink-0 rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
              v{{ m.current_version }}
            </span>
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
        <button
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
        <button
          :disabled="page >= totalPages"
          class="px-4 py-2 text-sm border border-input bg-background rounded-lg disabled:opacity-30 hover:bg-accent transition-colors"
          @click="nextPage"
          data-testid="lifecycle-map-list-next-page"
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
import PageHeader from '../../components/shared/PageHeader.vue'
import { useLifecycleMapsStore } from '../../stores/lifecycleMaps'
import ErrorAlert from '../../components/shared/ErrorAlert.vue'
import EmptyState from '../../components/shared/EmptyState.vue'
import { Button } from '@/components/ui/button'
import type { LifecycleMapSummary } from '../../stores/lifecycleMaps'

const router = useRouter()
const store = useLifecycleMapsStore()

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
  if (isNaN(d.getTime())) return dateStr
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
}

function openMap(m: LifecycleMapSummary): void {
  router.push(`/lifecycle-maps/${m.id}`)
}

onMounted(loadMaps)
</script>
