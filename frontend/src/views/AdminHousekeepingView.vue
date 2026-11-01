<template>
    <div class="page-wide">
      <PageHeader
        :title="$t('views.AdminHousekeepingView.title')"
        :subtitle="$t('views.AdminHousekeepingView.subtitle')"
      >
        <template #right>
          <Button severity="secondary" outlined data-testid="hk-refresh" :disabled="loading" @click="scan">
            {{ loading ? $t('views.AdminHousekeepingView.scanning') : $t('views.AdminHousekeepingView.refresh_scan') }}
          </Button>
        </template>
      </PageHeader>

      <div v-if="loading" data-testid="hk-loading">
        <output class="sr-only" aria-live="polite">{{ $t('common.loading') }}</output>
        <div class="space-y-4" aria-busy="true">
          <div
            v-for="n in 3"
            :key="`hk-skeleton-${n}`"
            class="rounded-lg border bg-card shadow-sm"
          >
            <div class="flex items-center gap-3 border-b px-4 py-3">
              <SkeletonBlock height-class="h-4 w-4" />
              <div class="flex-1 space-y-2">
                <SkeletonBlock height-class="h-4 w-40" />
                <SkeletonBlock height-class="h-3 w-64" />
              </div>
              <SkeletonBlock height-class="h-4 w-16" />
            </div>
            <div class="space-y-3 px-4 py-3">
              <SkeletonBlock height-class="h-3 w-full" />
              <SkeletonBlock height-class="h-3 w-5/6" />
            </div>
          </div>
        </div>
      </div>

      <ErrorAlert
        v-else-if="error"
        :message="error"
        :on-retry="scan"
        data-testid="hk-error"
      />

      <EmptyState
        v-else-if="categories.length === 0"
        :title="$t('views.AdminHousekeepingView.all_clean_title')"
        :description="$t('views.AdminHousekeepingView.all_clean_description')"
        data-testid="hk-empty"
      />

      <div v-else>
        <!--
          FAR-1977: when ANY category's scanner failed, the scan is INCOMPLETE.
          Without this the page could read as a clean scan (0 candidates) even
          though one or more scanners never completed.
        -->
        <div
          v-if="failedCategoryCount > 0"
          role="alert"
          class="mb-4 rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive-text"
        >
          <p class="font-medium">{{ $t('views.AdminHousekeepingView.scan_incomplete_title') }}</p>
          <p class="mt-1">{{ $t('views.AdminHousekeepingView.scan_incomplete_description', failedCategoryCount) }}</p>
        </div>

        <DataCard class="mb-4" variant="bordered" padding="sm" elevation="sm">
          <div class="flex items-center gap-4">
            <label class="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                :checked="allSelected"
                :indeterminate.prop="someSelected && !allSelected"
                data-testid="hk-select-all"
                @change="toggleSelectAll"
              />
              {{ $t('views.AdminHousekeepingView.select_all') }}
            </label>
            <span class="text-sm text-muted-foreground">
              {{ $t('views.AdminHousekeepingView.selected_count', { count: selectedCount, total: totalCount }) }}
            </span>
            <span class="text-sm text-muted-foreground">
              {{ $t('views.AdminHousekeepingView.total_candidates', totalCount) }}
            </span>
            <div class="ml-auto">
              <Button v-if="selectedCount > 0" severity="danger" data-testid="hk-delete-selected" @click="confirmDelete">
                {{ $t('views.AdminHousekeepingView.delete_selected', { count: selectedCount }) }}
              </Button>
            </div>
          </div>
        </DataCard>

        <DataCard
          v-for="cat in categories"
          :key="cat.category"
          class="mb-4"
          variant="bordered"
          padding="none"
          elevation="sm"
          data-testid="hk-category"
        >
          <div class="flex items-center gap-3 border-b px-4 py-3">
            <input
              type="checkbox"
              :aria-label="cat.label"
              :checked="categorySelected(cat.category)"
              :indeterminate.prop="categoryPartial(cat.category)"
              :disabled="isFailed(cat)"
              data-testid="hk-category-checkbox"
              @change="toggleCategory(cat.category)"
            />
            <div class="flex-1">
              <h3 class="font-semibold">{{ cat.label }}</h3>
              <p class="text-xs text-muted-foreground">{{ cat.description }}</p>
            </div>
            <span
              v-if="isFailed(cat)"
              class="rounded-full bg-destructive/10 px-2 py-0.5 text-xs font-medium text-destructive-text"
            >
              {{ $t('views.AdminHousekeepingView.scan_failed_badge') }}
            </span>
            <span v-else class="text-sm text-muted-foreground">{{ $t('views.AdminHousekeepingView.item_count', cat.count) }}</span>
          </div>

          <!--
            FAR-1977: a scanner that RAISED returns status="failed" with an empty
            candidate list. That must never render as the clean "No candidates
            found." state — a failed scan's results are UNKNOWN, not empty.
          -->
          <div
            v-if="isFailed(cat)"
            role="alert"
            class="px-4 py-3"
          >
            <div class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive-text">
              <p class="font-medium">{{ $t('views.AdminHousekeepingView.scan_failed_title') }}</p>
              <p class="mt-1">{{ $t('views.AdminHousekeepingView.scan_failed_reason', { reason: failureReason(cat) }) }}</p>
              <p v-if="failureException(cat)" class="mt-1">
                {{ $t('views.AdminHousekeepingView.scan_failed_exception', { exception: failureException(cat) }) }}
              </p>
            </div>
          </div>

          <div v-else-if="cat.candidates.length === 0" class="px-4 py-3 text-sm text-muted-foreground">
            {{ $t('views.AdminHousekeepingView.no_candidates') }}
          </div>

          <div
            v-for="c in cat.candidates"
            :key="c.id"
            class="flex items-center gap-3 border-t px-4 py-2.5"
            data-testid="hk-candidate"
          >
            <input
              type="checkbox"
              :aria-label="c.name"
              :checked="isSelected(c.id)"
              data-testid="hk-candidate-checkbox"
              @change="toggleItem(c.id)"
            />
            <div class="flex-1 min-w-0">
              <p class="truncate text-sm font-medium">{{ c.name }}</p>
              <p class="truncate text-xs text-muted-foreground">{{ c.detail }}</p>
            </div>
            <span class="whitespace-nowrap text-xs text-muted-foreground">
              {{ c.created_at ? formatDate(c.created_at) : '—' }}
            </span>
            <span
              class="rounded bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground"
            >
              {{ cat.category }}
            </span>
          </div>
        </DataCard>
      </div>

      <DataCard
        id="checkpoint-retention"
        class="mb-4"
        variant="bordered"
        padding="none"
        elevation="sm"
        data-testid="hk-checkpoint-retention"
      >
        <div class="flex items-center gap-3 border-b px-4 py-3">
          <div class="flex-1">
            <h3 class="font-semibold">{{ $t('views.AdminHousekeepingView.checkpoint_retention') }}</h3>
            <p class="text-xs text-muted-foreground">
              {{ $t('views.AdminHousekeepingView.checkpoint_retention_description') }}
            </p>
          </div>
          <span v-if="checkpointCategoryFailed" class="text-sm font-medium text-destructive-text">
            {{ $t('views.AdminHousekeepingView.scan_failed_badge') }}
          </span>
          <span v-else class="text-sm text-muted-foreground">
            {{ $t('views.AdminHousekeepingView.checkpoint_reclaimable', { count: checkpointCandidateCount }) }}
          </span>
        </div>
        <div class="flex flex-wrap items-center gap-3 px-4 py-3">
          <label class="flex items-center gap-2 text-sm">
            {{ $t('views.AdminHousekeepingView.checkpoint_purge_older_than') }}
            <input
              v-model.number="ckptMaxAge"
              type="number"
              min="1"
              class="w-20 rounded border bg-transparent px-2 py-1 text-sm text-right"
              data-testid="hk-ckpt-max-age"
            />
            {{ $t('views.AdminHousekeepingView.checkpoint_day', { count: ckptMaxAge }) }}
          </label>
          <Button v-if="!ckptConfirming" severity="danger" :disabled="ckptPurgeLoading || ckptMaxAge < 1" data-testid="hk-ckpt-purge" @click="ckptConfirming = true">
            {{ $t('views.AdminHousekeepingView.checkpoint_purge_button') }}
          </Button>
          <template v-else>
            <span class="text-sm text-muted-foreground">{{ $t('views.AdminHousekeepingView.checkpoint_confirm_prompt', { count: ckptMaxAge }) }}</span>
            <Button severity="danger" :disabled="ckptPurgeLoading" data-testid="hk-ckpt-purge-confirm" @click="doCheckpointPurge">
              {{ ckptPurgeLoading ? $t('views.AdminHousekeepingView.checkpoint_purging') : $t('views.AdminHousekeepingView.checkpoint_confirm_purge') }}
            </Button>
            <Button severity="secondary" outlined :disabled="ckptPurgeLoading" data-testid="hk-ckpt-purge-cancel" @click="ckptConfirming = false">
              {{ $t('views.AdminHousekeepingView.cancel') }}
            </Button>
          </template>
          <span v-if="ckptResult" class="text-sm text-muted-foreground" data-testid="hk-ckpt-result">
            {{ $t('views.AdminHousekeepingView.checkpoint_purged_prefix') }}
            {{ $t('views.AdminHousekeepingView.checkpoint_purged_rows', { count: ckptResult.checkpoints_purged }) }}
            {{ $t('views.AdminHousekeepingView.checkpoint_purged_from', { count: ckptResult.threads_purged }) }}
            {{ $t('views.AdminHousekeepingView.checkpoint_freed', { bytes: formatBytes(ckptResult.bytes_freed) }) }}
          </span>
          <span v-if="ckptError" class="text-sm text-destructive" data-testid="hk-ckpt-error">{{ ckptError }}</span>
        </div>
      </DataCard>

      <FormDialog
        v-model:open="showConfirm"
        :title="$t('views.AdminHousekeepingView.confirm_cleanup')"
        :description="$t('views.AdminHousekeepingView.delete_warning')"
        :confirm-text="$t('views.AdminHousekeepingView.delete_items', { count: selectedCount })"
        :confirm-loading-text="$t('views.AdminHousekeepingView.cleaning_up')"
        confirm-severity="danger"
        cancel-test-id="hk-cancel-cleanup"
        confirm-test-id="hk-confirm-cleanup"
        :confirm-disabled="cleaningUp"
        :loading="cleaningUp"
        data-testid="hk-confirm-dialog"
        @confirm="doCleanup"
      >
        <div class="max-h-48 space-y-2 overflow-y-auto">
          <div v-for="(ids, et) in groupedConfirmItems" :key="et">
            <p class="text-sm font-medium">{{ et }} ({{ ids.length }})</p>
          </div>
        </div>
      </FormDialog>
    </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useApi } from '../composables/useApi'
import type { components } from '../lib/api/schema'
import Button from 'primevue/button'
import FormDialog from '../components/shared/FormDialog.vue'
import SkeletonBlock from '../components/shared/SkeletonBlock.vue'
import PageHeader from '../components/shared/PageHeader.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import DataCard from '../components/ui/DataCard.vue'

// FAR-1977: derive the wire types from the generated OpenAPI schema rather
// than hand-declaring them. A local duplicate silently drifted from the
// contract (it lacked the new `status`/`error` fields, so a FAILED scanner
// rendered identically to a genuinely clean category).
type HousekeepingCategory = components['schemas']['HousekeepingCategory']
type HousekeepingScanResponse = components['schemas']['HousekeepingScanResponse']

interface CleanupItem {
  id: string
  entity_type: string
}

interface CleanupResponse {
  deleted_count: number
  errors: { entity_type?: string; id?: string; error: string }[]
}

interface CheckpointRetentionPurgeResponse {
  checkpoints_purged: number
  threads_purged: number
  bytes_freed: number
}

const { get, post } = useApi()
const { t } = useI18n()

const loading = ref(false)
const error = ref<string | null>(null)
const categories = ref<HousekeepingCategory[]>([])
const totalCount = ref(0)
const selectedIds = ref<Set<string>>(new Set())
const showConfirm = ref(false)
const cleaningUp = ref(false)

const ckptMaxAge = ref(3)
const ckptConfirming = ref(false)
const ckptPurgeLoading = ref(false)
const ckptResult = ref<CheckpointRetentionPurgeResponse | null>(null)
const ckptError = ref<string | null>(null)

// The Checkpoint Retention category is a bulk age-based action, not a
// per-candidate delete — exclude it from the generic select/cleanup flow.
const NON_GENERIC_CATEGORIES = new Set(['checkpoint_retention'])

// FAR-1977: `status` is the ONLY thing that distinguishes "ran, found nothing"
// from "scanner raised". A failed category may be selected/deleted as if empty
// so selection is disabled for it, its card renders an alert rather than the
// clean empty state, and the page-level banner marks the scan incomplete.
function isFailed(cat: HousekeepingCategory): boolean {
  return cat.status === 'failed'
}

function failureReason(cat: HousekeepingCategory): string {
  return cat.error?.reason ?? t('views.AdminHousekeepingView.scan_failed_unknown_reason')
}

function failureException(cat: HousekeepingCategory): string | null {
  return cat.error?.exception ?? null
}

function formatDate(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

function formatBytes(bytes: number): string {
  if (!bytes || bytes <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const idx = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
  const value = bytes / Math.pow(1024, idx)
  return `${value.toFixed(value >= 10 || idx === 0 ? 0 : 1)} ${units[idx]}`
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
    error.value = e instanceof Error ? e.message : t('views.AdminHousekeepingView.scan_error')
  } finally {
    loading.value = false
  }
}

const allCandidates = computed<{ id: string; category: string; entity_type: string }[]>(() => {
  const result: { id: string; category: string; entity_type: string }[] = []
  for (const cat of categories.value) {
    if (NON_GENERIC_CATEGORIES.has(cat.category)) continue
    for (const c of cat.candidates) {
      result.push({ id: c.id, category: cat.category, entity_type: c.entity_type })
    }
  }
  return result
})

const checkpointCategory = computed<HousekeepingCategory | null>(() => {
  return categories.value.find(c => c.category === 'checkpoint_retention') ?? null
})

const checkpointCategoryFailed = computed(() => {
  const cat = checkpointCategory.value
  return cat !== null && isFailed(cat)
})

const checkpointCandidateCount = computed(() => {
  const cat = checkpointCategory.value
  return cat ? cat.count : 0
})

// A scan is incomplete if ANY category's scanner raised. Surfaced at page level
// so a failed category can never be read as an all-clean scan.
const failedCategoryCount = computed(() => categories.value.filter(isFailed).length)

async function doCheckpointPurge() {
  ckptPurgeLoading.value = true
  ckptError.value = null
  try {
    const resp = await post<CheckpointRetentionPurgeResponse>('/api/v1/admin/housekeeping/checkpoints/purge', {
      max_age_days: ckptMaxAge.value,
      confirm: true,
    })
    ckptResult.value = resp
    ckptConfirming.value = false
    await scan()
  } catch (e: unknown) {
    ckptError.value = e instanceof Error ? e.message : t('views.AdminHousekeepingView.checkpoint_purge_failed')
  } finally {
    ckptPurgeLoading.value = false
  }
}

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

function entityTypeOf(c: { id: string; category: string; entity_type?: string }): string {
  return c.entity_type && c.entity_type.length > 0 ? c.entity_type : c.category
}

const groupedConfirmItems = computed(() => {
  const map: Record<string, string[]> = {}
  for (const c of allCandidates.value) {
    if (selectedIds.value.has(c.id)) {
      const et = entityTypeOf(c)
      if (!map[et]) map[et] = []
      map[et].push(c.id)
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
      .map(c => ({ id: c.id, entity_type: entityTypeOf(c) }))

    await post<CleanupResponse>('/api/v1/admin/housekeeping/cleanup', { items })
    showConfirm.value = false
    await scan()
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : t('views.AdminHousekeepingView.cleanup_error')
  } finally {
    cleaningUp.value = false
  }
}

scan()
</script>
