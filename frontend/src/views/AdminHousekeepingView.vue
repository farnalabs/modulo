<template>
  <FeatureGate feature-name="admin_housekeeping" required-tier="community" show-disabled>
    <div class="page-wide">
      <PageHeader
        title="Housekeeping"
        subtitle="Scan for cleanup candidates across your organisation"
      >
        <template #actions>
          <Button
            variant="outline"
            data-testid="hk-refresh"
            :disabled="loading"
            @click="scan"
          >
            {{ loading ? 'Scanningâ€¦' : 'Refresh Scan' }}
          </Button>
        </template>
      </PageHeader>

      <div v-if="loading" class="flex justify-center py-12">
        <LoadingSpinner />
      </div>

      <div
        v-else-if="error"
        class="rounded-lg border border-destructive/50 bg-destructive/10 p-4"
        data-testid="hk-error"
      >
        <p class="text-sm text-destructive">{{ error }}</p>
        <Button variant="outline" size="sm" class="mt-2" @click="scan" data-testid="hk-retry">
          Retry
        </Button>
      </div>

      <EmptyState
        v-else-if="categories.length === 0"
        title="All Clean!"
        description="No cleanup candidates found. Everything looks tidy."
        data-testid="hk-empty"
      />

      <div v-else>
        <div class="mb-4 flex items-center gap-4 rounded-lg border bg-card p-3 shadow-sm">
          <label class="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              :checked="allSelected"
              :indeterminate.prop="someSelected && !allSelected"
              data-testid="hk-select-all"
              @change="toggleSelectAll"
            />
            Select All
          </label>
          <span class="text-sm text-muted-foreground">
            {{ selectedCount }} of {{ totalCount }} selected
          </span>
          <span class="text-sm text-muted-foreground">
            {{ totalCount }} total candidate{{ totalCount === 1 ? '' : 's' }}
          </span>
          <div class="ml-auto">
            <Button
              v-if="selectedCount > 0"
              variant="destructive"
              data-testid="hk-delete-selected"
              @click="confirmDelete"
            >
              Delete {{ selectedCount }} Selected
            </Button>
          </div>
        </div>

        <div
          v-for="cat in categories"
          :key="cat.category"
          class="mb-4 rounded-lg border bg-card shadow-sm"
          data-testid="hk-category"
        >
          <div class="flex items-center gap-3 border-b px-4 py-3">
            <input
              type="checkbox"
              :checked="categorySelected(cat.category)"
              :indeterminate.prop="categoryPartial(cat.category)"
              data-testid="hk-category-checkbox"
              @change="toggleCategory(cat.category)"
            />
            <div class="flex-1">
              <h3 class="font-semibold">{{ cat.label }}</h3>
              <p class="text-xs text-muted-foreground">{{ cat.description }}</p>
            </div>
            <span class="text-sm text-muted-foreground">{{ cat.count }} item{{ cat.count === 1 ? '' : 's' }}</span>
          </div>

          <div v-if="cat.candidates.length === 0" class="px-4 py-3 text-sm text-muted-foreground">
            No candidates found.
          </div>

          <div
            v-for="c in cat.candidates"
            :key="c.id"
            class="flex items-center gap-3 border-t px-4 py-2.5"
            data-testid="hk-candidate"
          >
            <input
              type="checkbox"
              :checked="isSelected(c.id)"
              data-testid="hk-candidate-checkbox"
              @change="toggleItem(c.id)"
            />
            <div class="flex-1 min-w-0">
              <p class="truncate text-sm font-medium">{{ c.name }}</p>
              <p class="truncate text-xs text-muted-foreground">{{ c.detail }}</p>
            </div>
            <span v-if="c.created_at" class="whitespace-nowrap text-xs text-muted-foreground">
              {{ formatDate(c.created_at) }}
            </span>
            <span
              class="rounded bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground"
            >
              {{ cat.category }}
            </span>
          </div>
        </div>
      </div>

      <Dialog v-if="showConfirm" :open="showConfirm" @update:open="showConfirm = false">
        <DialogContent data-testid="hk-confirm-dialog">
          <DialogHeader>
            <DialogTitle>Confirm Cleanup</DialogTitle>
            <DialogDescription>
              This will delete the following items. This action cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <div class="max-h-48 space-y-2 overflow-y-auto">
            <div v-for="(ids, et) in groupedConfirmItems" :key="et">
              <p class="text-sm font-medium">{{ et }} ({{ ids.length }})</p>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" @click="showConfirm = false" data-testid="hk-cancel-cleanup">
              Cancel
            </Button>
            <Button
              variant="destructive"
              :disabled="cleaningUp"
              data-testid="hk-confirm-cleanup"
              @click="doCleanup"
            >
              {{ cleaningUp ? 'Cleaning upâ€¦' : `Delete ${selectedCount} items` }}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useApi } from '../composables/useApi'
import { Button } from '../components/ui/button'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import PageHeader from '../components/shared/PageHeader.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '../components/ui/dialog'
import FeatureGate from '../components/FeatureGate.vue'

interface CandidateItem {
  id: string
  name: string
  detail: string
  created_at: string | null
}

interface HousekeepingCategory {
  category: string
  label: string
  description: string
  candidates: CandidateItem[]
  count: number
}

interface HousekeepingScanResponse {
  categories: HousekeepingCategory[]
  total_count: number
}

interface CleanupItem {
  id: string
  entity_type: string
}

interface CleanupResponse {
  deleted_count: number
  errors: { entity_type?: string; id?: string; error: string }[]
}

const { get, post } = useApi()

const loading = ref(false)
const error = ref<string | null>(null)
const categories = ref<HousekeepingCategory[]>([])
const totalCount = ref(0)
const selectedIds = ref<Set<string>>(new Set())
const showConfirm = ref(false)
const cleaningUp = ref(false)

function formatDate(iso: string): string {
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

async function scan() {
  loading.value = true
  error.value = null
  try {
    const resp = await get<HousekeepingScanResponse>('/api/v1/admin/housekeeping')
    categories.value = resp.categories ?? []
    totalCount.value = resp.total_count ?? 0
    selectedIds.value = new Set()
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : 'Failed to scan for housekeeping candidates'
  } finally {
    loading.value = false
  }
}

const allCandidates = computed<{ id: string; category: string }[]>(() => {
  const result: { id: string; category: string }[] = []
  for (const cat of categories.value) {
    for (const c of cat.candidates) {
      result.push({ id: c.id, category: cat.category })
    }
  }
  return result
})

const allSelected = computed(() => {
  return allCandidates.value.length > 0 && allCandidates.value.every(c => selectedIds.value.has(c.id))
})

const someSelected = computed(() => {
  return allCandidates.value.some(c => selectedIds.value.has(c.id))
})

const selectedCount = computed(() => selectedIds.value.size)

function toggleSelectAll() {
  if (allSelected.value) {
    selectedIds.value = new Set()
  } else {
    selectedIds.value = new Set(allCandidates.value.map(c => c.id))
  }
}

function categorySelected(category: string): boolean {
  const cat = categories.value.find(c => c.category === category)
  if (!cat || cat.candidates.length === 0) return false
  return cat.candidates.every(c => selectedIds.value.has(c.id))
}

function categoryPartial(category: string): boolean {
  const cat = categories.value.find(c => c.category === category)
  if (!cat || cat.candidates.length === 0) return false
  const some = cat.candidates.some(c => selectedIds.value.has(c.id))
  return some && !categorySelected(category)
}

function toggleCategory(category: string) {
  const cat = categories.value.find(c => c.category === category)
  if (!cat) return
  const next = new Set(selectedIds.value)
  const allInCat = cat.candidates.map(c => c.id)
  const allCatSelected = allInCat.every(id => next.has(id))
  if (allCatSelected) {
    for (const id of allInCat) next.delete(id)
  } else {
    for (const id of allInCat) next.add(id)
  }
  selectedIds.value = next
}

function isSelected(id: string): boolean {
  return selectedIds.value.has(id)
}

function toggleItem(id: string) {
  const next = new Set(selectedIds.value)
  if (next.has(id)) {
    next.delete(id)
  } else {
    next.add(id)
  }
  selectedIds.value = next
}

const groupedConfirmItems = computed(() => {
  const map: Record<string, string[]> = {}
  for (const c of allCandidates.value) {
    if (selectedIds.value.has(c.id)) {
      if (!map[c.category]) map[c.category] = []
      map[c.category].push(c.id)
    }
  }
  return map
})

function confirmDelete() {
  showConfirm.value = true
}

async function doCleanup() {
  cleaningUp.value = true
  try {
    const items: CleanupItem[] = allCandidates.value
      .filter(c => selectedIds.value.has(c.id))
      .map(c => ({ id: c.id, entity_type: c.entity_type || c.category }))

    await post<CleanupResponse>('/api/v1/admin/housekeeping/cleanup', { items })
    showConfirm.value = false
    await scan()
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : 'Cleanup failed'
  } finally {
    cleaningUp.value = false
  }
}

scan()
</script>
