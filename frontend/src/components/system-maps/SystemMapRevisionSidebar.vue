<template>
  <div class="flex h-full flex-col" :data-testid="dataTestId">
    <div class="border-b p-4">
      <h2 class="font-semibold">{{ $t('views.SystemMapDetailView.revision_history') }}</h2>
      <p class="mt-1 text-xs text-muted-foreground">{{ $t('views.SystemMapDetailView.revision_history_hint') }}</p>
    </div>
    <div class="flex gap-2 border-b p-3">
      <button type="button" class="flex-1 rounded border px-2 py-1.5 text-xs" @click="$emit('new-draft')" data-testid="system-map-new-draft">{{ $t('views.SystemMapDetailView.new_draft') }}</button>
      <button type="button" class="flex-1 rounded border px-2 py-1.5 text-xs" @click="$emit('discover')" data-testid="system-map-sidebar-discover">{{ $t('views.SystemMapDetailView.discover') }}</button>
    </div>

    <!-- Version-aware switching: jump back to the revision published now, or
         resolve the revision that was ACTIVE at a chosen instant. -->
    <div class="space-y-2 border-b p-3">
      <button
        type="button"
        class="w-full rounded border px-2 py-1.5 text-xs"
        data-testid="system-map-revision-current"
        @click="$emit('select-current')"
      >{{ $t('views.SystemMapDetailView.view_current') }}</button>

      <form class="space-y-2" @submit.prevent="emitByTime">
        <label :for="atInputId" class="block text-xs font-medium">{{ $t('views.SystemMapDetailView.as_of_label') }}</label>
        <div class="flex gap-2">
          <input
            :id="atInputId"
            v-model="atValue"
            type="datetime-local"
            class="min-w-0 flex-1 rounded border bg-background px-2 py-1.5 text-xs"
            data-testid="system-map-revision-at"
          />
          <button
            type="submit"
            class="rounded border px-2 py-1.5 text-xs disabled:opacity-50"
            :disabled="!atIso || byTimeLoading"
            data-testid="system-map-revision-by-time"
          >
            {{ byTimeLoading ? $t('views.SystemMapDetailView.as_of_loading') : $t('views.SystemMapDetailView.view_at_time') }}
          </button>
        </div>
      </form>

      <!-- A 404 means nothing was published at that instant: an honest empty
           state, never an error. It lands asynchronously, so the region is
           announced politely. -->
      <div v-if="byTimeUnavailable" aria-live="polite" data-testid="system-map-revision-as-of-unavailable">
        <EmptyState
          :title="$t('views.SystemMapDetailView.as_of_unavailable_title')"
          :description="$t('views.SystemMapDetailView.as_of_unavailable_body', { at: formattedRequestedAt })"
        />
      </div>
      <div v-else-if="byTimeError" data-testid="system-map-revision-as-of-error">
        <ErrorAlert :message="byTimeError" :on-retry="retryAsOf" />
      </div>
    </div>

    <!-- Historical banner: explicit and persistent while a point-in-time
         revision is displayed. -->
    <div
      v-if="historicalAt"
      class="border-b bg-warning/10 p-3 text-xs"
      aria-live="polite"
      role="note"
      data-testid="system-map-revision-historical-banner"
    >
      <p class="font-medium text-warning-text">{{ $t('views.SystemMapDetailView.historical_banner_title') }}</p>
      <p class="mt-0.5 text-muted-foreground">{{ $t('views.SystemMapDetailView.historical_banner_body', { at: historicalAtLabel }) }}</p>
    </div>

    <div class="flex-1 overflow-y-auto p-3">
      <div v-if="loading" data-testid="system-map-history-loading">
        <TableSkeleton :columns="2" :rows="4" :aria-label="$t('views.SystemMapDetailView.revision_history_loading')" />
      </div>

      <div v-else-if="error" data-testid="system-map-history-error">
        <ErrorAlert :message="error" :on-retry="retryHistory" />
      </div>

      <EmptyState
        v-else-if="revisions.length === 0"
        :title="$t('views.SystemMapDetailView.no_revisions')"
        data-testid="system-map-history-empty"
      />

      <template v-else>
        <p class="mb-2 text-[10px] text-muted-foreground">
          {{ $t('views.SystemMapDetailView.revision_history_total', { count: displayTotal }) }}
        </p>
        <ol class="space-y-2">
          <li v-for="revision in sortedRevisions" :key="revision.id">
            <button
              type="button"
              class="w-full rounded-lg border p-3 text-left transition-colors hover:bg-accent"
              :class="activeRevisionId === revision.id ? 'border-primary bg-primary/5' : ''"
              :aria-current="activeRevisionId === revision.id ? 'true' : undefined"
              @click="$emit('select-revision', revision.id)"
              :data-testid="`system-map-revision-${revision.revision_number}`"
            >
              <div class="flex items-center justify-between gap-2">
                <span class="font-medium">{{ $t('views.SystemMapDetailView.revision', { number: revision.revision_number }) }}</span>
                <span class="flex items-center gap-1">
                  <span
                    v-if="isCurrentRevision(revision)"
                    class="rounded bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium uppercase text-primary-text"
                    data-testid="system-map-revision-current-badge"
                  >{{ $t('views.SystemMapDetailView.current_revision') }}</span>
                  <span class="rounded px-1.5 py-0.5 text-[10px] uppercase" :class="statusClass(revision.status)">{{ statusLabel(revision.status) }}</span>
                </span>
              </div>
              <div class="mt-1 text-xs text-muted-foreground">{{ $t('views.SystemMapDetailView.created_at') }}: {{ formatDate(revision.created_at) }}</div>
              <div v-if="revision.published_at" class="mt-1 text-[10px] text-muted-foreground">{{ $t('views.SystemMapDetailView.effective_from') }}: {{ formatDate(revision.published_at) }}</div>
            </button>
          </li>
        </ol>

        <nav
          v-if="pageCount > 1"
          class="mt-3 flex items-center justify-between gap-2"
          :aria-label="$t('views.SystemMapDetailView.revision_history_pagination')"
          data-testid="system-map-history-pagination"
        >
          <button
            type="button"
            class="rounded border px-2 py-1 text-xs disabled:opacity-50"
            :disabled="safePage <= 1"
            data-testid="system-map-history-prev"
            @click="$emit('page-change', safePage - 1)"
          >{{ $t('views.SystemMapDetailView.revision_history_prev') }}</button>
          <span class="text-[10px] text-muted-foreground" aria-live="polite" data-testid="system-map-history-page-status">
            {{ $t('views.SystemMapDetailView.revision_history_page_status', { page: safePage, pages: pageCount }) }}
          </span>
          <button
            type="button"
            class="rounded border px-2 py-1 text-xs disabled:opacity-50"
            :disabled="safePage >= pageCount"
            data-testid="system-map-history-next"
            @click="$emit('page-change', safePage + 1)"
          >{{ $t('views.SystemMapDetailView.revision_history_next') }}</button>
        </nav>
      </template>
    </div>

    <div class="border-t p-3">
      <button
        type="button"
        class="w-full rounded border px-2 py-1.5 text-xs disabled:opacity-50"
        :disabled="!activeRevisionId"
        data-testid="system-map-revision-compare"
        @click="$emit('compare')"
      >
        {{ $t('views.SystemMapDetailView.compare_revisions') }}
      </button>
    </div>
  </div>
</template>
<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import type { SystemMap, SystemMapRevision } from '../../stores/systemMaps'
import EmptyState from '../shared/EmptyState.vue'
import ErrorAlert from '../shared/ErrorAlert.vue'
import TableSkeleton from '../shared/TableSkeleton.vue'
import { formatDateShortWithTime, toDate } from '../../lib/formatDate'

const props = withDefaults(defineProps<{
  mapId: string
  revisions: SystemMapRevision[]
  activeRevisionId?: string
  activeMap: SystemMap | null
  /** Published revision id, per the history endpoint's is_current marker. */
  currentRevisionId?: string | null
  loading?: boolean
  error?: string | null
  /** 1-based page the parent is showing; the parent owns the slice. */
  page?: number
  pageCount?: number
  /** Total revisions across all pages. */
  total?: number
  byTimeLoading?: boolean
  byTimeRequestedAt?: string | null
  byTimeUnavailable?: boolean
  byTimeError?: string | null
  /** Set when the displayed revision came from the point-in-time lookup. */
  historicalAt?: string | null
  dataTestId?: string
}>(), {
  activeRevisionId: undefined,
  currentRevisionId: null,
  loading: false,
  error: null,
  page: 1,
  pageCount: 1,
  total: undefined,
  byTimeLoading: false,
  byTimeRequestedAt: null,
  byTimeUnavailable: false,
  byTimeError: null,
  historicalAt: null,
  dataTestId: undefined,
})

const emit = defineEmits<{
  (e: 'select-revision', revisionId: string): void
  (e: 'select-current'): void
  (e: 'select-by-time', at: string): void
  (e: 'new-draft'): void
  (e: 'discover'): void
  (e: 'page-change', page: number): void
  (e: 'retry-history'): void
  (e: 'compare'): void
}>()

const { t } = useI18n()

const atInputId = `system-map-revision-at-${props.mapId}`
const atValue = ref('')

// datetime-local carries no timezone; convert it to an absolute instant here so
// the backend's effective_from <= at < effective_to comparison is unambiguous.
const atIso = computed(() => {
  if (!atValue.value) return ''
  const date = toDate(atValue.value)
  return date ? date.toISOString() : ''
})

const safePage = computed(() => Math.max(1, props.page))
/**
 * The parent already supplies the history page newest-first; sorting here keeps
 * the component correct if it is handed an unordered set (and preserves its
 * original contract).
 */
const sortedRevisions = computed(() => [...props.revisions].sort((a, b) => b.revision_number - a.revision_number))
const displayTotal = computed(() => props.total ?? props.revisions.length)
// The history endpoint's is_current marker is authoritative; the map's
// published_revision_id is the fallback for a caller without the history.
const resolvedCurrentRevisionId = computed(() => props.currentRevisionId ?? props.activeMap?.published_revision_id ?? null)

function isCurrentRevision(revision: SystemMapRevision): boolean {
  return resolvedCurrentRevisionId.value === revision.id
}

function formatDate(value: string | null | undefined): string {
  return value ? formatDateShortWithTime(value) : '—'
}

const formattedRequestedAt = computed(() => formatDate(props.byTimeRequestedAt))
const historicalAtLabel = computed(() => formatDate(props.historicalAt))

function statusLabel(status: string): string {
  if (status === 'published') return t('views.SystemMapDetailView.status_published')
  if (status === 'archived') return t('views.SystemMapDetailView.status_archived')
  return t('views.SystemMapDetailView.status_draft')
}

function statusClass(status: string): string {
  if (status === 'published') return 'bg-success/10 text-success-text'
  if (status === 'archived') return 'bg-muted text-muted-foreground'
  return 'bg-warning/10 text-warning-text'
}

function retryHistory(): void {
  emit('retry-history')
}

function retryAsOf(): void {
  emitByTime()
}

function emitByTime(): void {
  if (!atIso.value) return
  emit('select-by-time', atIso.value)
}
</script>
