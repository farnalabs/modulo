<template>
  <div class="fixed right-4 top-16 z-40 w-96 max-w-[calc(100vw-2rem)] rounded-lg border bg-card p-4 shadow-lg">
    <div class="mb-3 flex items-center justify-between">
      <h3 class="text-sm font-semibold">{{ $t('components.PipelineSnapshotTimeline.version_timeline') }}</h3>
      <button
        type="button"
        class="rounded p-1 hover:bg-accent"
        :aria-label="$t('components.PipelineSnapshotTimeline.close')"
        data-testid="snapshot-timeline-close"
        @click="$emit('close')"
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
      </button>
    </div>

    <LoadingSpinner v-if="loading" />

    <!-- M3: a FAILED load is not an empty history. Distinguish the two so a
         network/server failure never reads as "this pipeline has no versions",
         and offer an explicit retry. role="alert" announces it when it appears. -->
    <div
      v-else-if="loadError"
      role="alert"
      data-testid="snapshot-timeline-load-error"
      class="space-y-2"
    >
      <p class="text-sm text-muted-foreground">{{ $t('components.PipelineSnapshotTimeline.load_failed') }}</p>
      <Button
        size="small"
        class="text-xs"
        data-testid="snapshot-timeline-load-retry"
        @click="loadSnapshots"
      >
        {{ $t('components.PipelineSnapshotTimeline.retry') }}
      </Button>
    </div>

    <template v-else>
      <p v-if="snapshots.length === 0" class="text-sm text-muted-foreground">{{ $t('components.PipelineSnapshotTimeline.no_snapshots_yet') }}</p>

      <template v-else>
        <!-- FAR-1817: PRIMARY rows are SUBSTANTIVE versions only
             (created_kind initial / edit / rollback). Run snapshots are NOT
             rows — they annotate the version they executed ("ran Nx") and
             expand on demand. A row (or an expanded run) click opens the
             read-only historic version view (FAR-1812) via the host editor's
             `view` event; the radio selects the version for the diff/restore
             actions, which stay on this component. -->
        <!-- FAR-1895: the loaded window holds no substantive version row. That
             is NOT the same as "this pipeline has no version" — a run streak
             longer than the window pushes the newest version below it. Say so
             explicitly (with the runs, unattributed, in the bucket below)
             rather than fabricating an `initial` row from a run snapshot. -->
        <p
          v-if="versionGroups.length === 0"
          class="mb-3 text-sm text-muted-foreground"
          data-testid="snapshot-timeline-no-version-in-window"
        >
          {{ $t('components.PipelineSnapshotTimeline.no_version_in_window') }}
        </p>

        <div class="mb-3 max-h-56 space-y-2 overflow-y-auto">
          <div v-for="g in versionGroups" :key="g.key" class="space-y-1">
            <div class="flex items-stretch gap-1">
              <input
                type="radio"
                class="mt-2 shrink-0"
                name="snapshot-timeline-select"
                :value="g.anchor.id"
                v-model="selectedSnapshotId"
                :aria-label="$t('components.PipelineSnapshotTimeline.select_version_aria', { version: g.anchor.snapshot_version })"
                :data-testid="`snapshot-timeline-select-${g.anchor.id}`"
              />
              <button
                type="button"
                :data-testid="`snapshot-timeline-row-${g.anchor.id}`"
                :aria-label="$t('components.PipelineSnapshotTimeline.open_version_aria', { version: g.anchor.snapshot_version })"
                class="flex min-w-0 flex-1 items-center justify-between gap-2 rounded-md border px-2 py-1.5 text-left hover:bg-accent"
                @click="emit('view', g.anchor.id)"
              >
                <div class="flex min-w-0 items-center gap-2">
                  <span class="text-xs font-medium">v{{ g.anchor.snapshot_version }}</span>
                  <span class="rounded bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium text-primary">
                    {{ kindLabel(g.kind) }}
                  </span>
                  <span
                    v-if="g.anchor.channel && g.anchor.channel !== 'none'"
                    class="rounded bg-warning/10 px-1.5 py-0.5 text-[10px] font-medium text-warning-text"
                  >
                    {{ g.anchor.channel }}
                  </span>
                  <span class="truncate text-[10px] text-muted-foreground">{{ formatDate(g.anchor.created_at) }}</span>
                </div>
                <span class="shrink-0 text-[10px] text-muted-foreground">{{ g.anchor.tag || '' }}</span>
              </button>
              <button
                v-if="g.runs.length > 0"
                type="button"
                class="shrink-0 self-center rounded-md border border-input bg-background px-2 py-1 text-[10px] font-medium hover:bg-accent"
                :aria-expanded="expandedGroups.has(g.key)"
                :aria-controls="`snapshot-timeline-runs-${g.anchor.id}`"
                :data-testid="`snapshot-timeline-runs-toggle-${g.anchor.id}`"
                @click="toggleGroup(g.key)"
              >
                {{ $t('components.PipelineSnapshotTimeline.ran_times', { count: g.runs.length }) }}
              </button>
            </div>
            <!-- aria-controls must reference an element that exists even while
                 collapsed, so the runs container is always mounted for a group
                 with runs and hidden with the `hidden` attribute (never v-if). -->
            <div
              v-if="g.runs.length > 0"
              :hidden="!expandedGroups.has(g.key)"
              :id="`snapshot-timeline-runs-${g.anchor.id}`"
              :data-testid="`snapshot-timeline-runs-list-${g.anchor.id}`"
              class="ml-5 space-y-1"
            >
              <button
                v-for="r in g.runs"
                :key="r.id"
                type="button"
                class="flex w-full items-center justify-between gap-2 rounded-md border border-dashed px-2 py-1 text-left text-[10px] text-muted-foreground hover:bg-accent"
                :aria-label="$t('components.PipelineSnapshotTimeline.open_version_aria', { version: r.snapshot_version })"
                :data-testid="`snapshot-timeline-run-${r.id}`"
                @click="emit('view', r.id)"
              >
                <span class="font-medium">v{{ r.snapshot_version }}</span>
                <span>{{ formatDate(r.created_at) }}</span>
              </button>
            </div>
          </div>

          <!-- M2: runs that predate the OLDEST substantive version are NOT
               attributed to a version they did not execute. They render as an
               explicit "earlier versions" bucket instead of being folded onto
               the oldest row (which would assert false provenance). -->
          <div
            v-if="earlierRuns.length > 0"
            class="rounded-md border border-dashed p-1.5"
            data-testid="snapshot-timeline-earlier"
          >
            <button
              type="button"
              class="w-full rounded-md px-1 py-0.5 text-left text-[10px] font-medium text-muted-foreground hover:bg-accent"
              :aria-expanded="expandedGroups.has('earlier')"
              aria-controls="snapshot-timeline-runs-earlier"
              data-testid="snapshot-timeline-earlier-toggle"
              @click="toggleGroup('earlier')"
            >
              {{ $t('components.PipelineSnapshotTimeline.ran_earlier', { count: earlierRuns.length }) }}
            </button>
            <div
              :hidden="!expandedGroups.has('earlier')"
              id="snapshot-timeline-runs-earlier"
              data-testid="snapshot-timeline-earlier-list"
              class="mt-1 space-y-1"
            >
              <button
                v-for="r in earlierRuns"
                :key="r.id"
                type="button"
                class="flex w-full items-center justify-between gap-2 rounded-md border border-dashed px-2 py-1 text-left text-[10px] text-muted-foreground hover:bg-accent"
                :aria-label="$t('components.PipelineSnapshotTimeline.open_version_aria', { version: r.snapshot_version })"
                :data-testid="`snapshot-timeline-earlier-run-${r.id}`"
                @click="emit('view', r.id)"
              >
                <span class="font-medium">v{{ r.snapshot_version }}</span>
                <span>{{ formatDate(r.created_at) }}</span>
              </button>
            </div>
          </div>

          <!-- FAR-1895: a version-DESC all-run window with NO resolved current
               version holds the NEWEST runs (the newest version is below the
               window, or the resolving query failed). They are NOT "earlier"
               and must not claim to be, so they render in their own bucket with
               no version attribution. -->
          <div
            v-if="unattributedRuns.length > 0"
            class="rounded-md border border-dashed p-1.5"
            data-testid="snapshot-timeline-unattributed"
          >
            <button
              type="button"
              class="w-full rounded-md px-1 py-0.5 text-left text-[10px] font-medium text-muted-foreground hover:bg-accent"
              :aria-expanded="expandedGroups.has('unattributed')"
              aria-controls="snapshot-timeline-runs-unattributed"
              data-testid="snapshot-timeline-unattributed-toggle"
              @click="toggleGroup('unattributed')"
            >
              {{ $t('components.PipelineSnapshotTimeline.ran_unattributed', { count: unattributedRuns.length }) }}
            </button>
            <div
              :hidden="!expandedGroups.has('unattributed')"
              id="snapshot-timeline-runs-unattributed"
              data-testid="snapshot-timeline-unattributed-list"
              class="mt-1 space-y-1"
            >
              <button
                v-for="r in unattributedRuns"
                :key="r.id"
                type="button"
                class="flex w-full items-center justify-between gap-2 rounded-md border border-dashed px-2 py-1 text-left text-[10px] text-muted-foreground hover:bg-accent"
                :aria-label="$t('components.PipelineSnapshotTimeline.open_version_aria', { version: r.snapshot_version })"
                :data-testid="`snapshot-timeline-unattributed-run-${r.id}`"
                @click="emit('view', r.id)"
              >
                <span class="font-medium">v{{ r.snapshot_version }}</span>
                <span>{{ formatDate(r.created_at) }}</span>
              </button>
            </div>
          </div>
        </div>

        <!-- M1: history is windowed (page_size 100). Never leave the user
             believing they are seeing their whole history. -->
        <div
          v-if="hasMore"
          class="mb-3 space-y-1"
          data-testid="snapshot-timeline-truncated"
        >
          <p class="text-[10px] text-muted-foreground">
            {{ $t('components.PipelineSnapshotTimeline.truncated_notice', { loaded: snapshots.length, total: snapshotsTotal }) }}
          </p>
          <Button
            size="small"
            class="text-xs"
            :loading="loadingMore"
            data-testid="snapshot-timeline-load-more"
            @click="loadMoreSnapshots"
          >
            {{ $t('components.PipelineSnapshotTimeline.load_more') }}
          </Button>
          <p v-if="loadMoreError" role="alert" class="text-[10px] text-destructive" data-testid="snapshot-timeline-load-more-error">
            {{ $t('components.PipelineSnapshotTimeline.load_more_failed') }}
          </p>
        </div>
      </template>

      <span class="mb-1 block text-[10px] text-muted-foreground">{{ $t('components.PipelineSnapshotTimeline.compare_to') }}</span>
      <div class="mb-3 flex items-center gap-2">
        <Select
          data-testid="snapshot-timeline-compare"
          :aria-label="$t('components.PipelineSnapshotTimeline.select_base_snapshot_aria')"
          :placeholder="$t('components.PipelineSnapshotTimeline.select_base_snapshot')"
          :options="compareOptions"
          option-value="value"
          option-label="label"
          v-model="compareB"
        />
        <Button
          size="small"
          class="text-xs"
          :disabled="!canDiff"
          @click="runDiff"
          data-testid="snapshot-timeline-diff"
        >
          {{ $t('components.PipelineSnapshotTimeline.diff') }}
        </Button>
      </div>

      <div v-if="diffResult" class="space-y-2" data-testid="snapshot-timeline-diff-result">
        <div class="text-xs">
          <span class="font-medium">{{ $t('components.PipelineSnapshotTimeline.impacted_nodes') }}</span>
          <span class="text-muted-foreground">{{ impactedLabel ? impactedLabel : $t('components.PipelineSnapshotTimeline.none') }}</span>
        </div>

        <div v-if="diffResult.semantic?.breaking_changes?.length" class="space-y-1">
          <div class="text-xs font-medium text-destructive">{{ $t('components.PipelineSnapshotTimeline.breaking_changes') }}</div>
          <div
            v-for="(b, i) in diffResult.semantic.breaking_changes"
            :key="i"
            class="rounded-md border border-destructive/30 bg-destructive/5 px-2 py-1 text-[10px]"
          >
            <span :class="b.severity === 'block' ? 'font-medium text-destructive' : 'text-warning-text'">
              {{ b.severity }}
            </span>
            — {{ b.reason }}
          </div>
        </div>
        <p v-else class="text-[11px] text-muted-foreground">{{ $t('components.PipelineSnapshotTimeline.no_breaking_port_changes') }}</p>
      </div>

      <div class="mt-3 flex justify-end border-t pt-2">
        <!-- FAR-1816: "Restore this version" replaces the old "Rollback to
             selected" label. The action no longer POSTs the rollback itself —
             it asks the host editor to open the SHARED restore dialog (backed
             by the same endpoint), so the diff summary and guard-refusal copy
             live in one place. -->
        <Button
          size="small"
          severity="danger"
          outlined
          class="text-xs"
          :disabled="!selectedSnapshotId"
          @click="requestRestore"
          data-testid="snapshot-timeline-rollback"
        >
          {{ $t('components.PipelineSnapshotTimeline.restore_version') }}
        </Button>
      </div>
      <p v-if="actionError" role="alert" class="mt-1 text-[11px] text-destructive">{{ actionError }}</p>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import Select from '../shared/AppSelect.vue'
import Button from 'primevue/button'
import LoadingSpinner from '../shared/LoadingSpinner.vue'
import { formatDateShort } from '../../lib/formatDate'
import { api } from '../../lib/api/client'
import { formatApiError } from '../../lib/api/formatError'
import { withTimeout } from '../../lib/requestTimeout'
import { isSubstantiveKind, createdKindLabel, SNAPSHOT_PAGE_SIZE } from '../../lib/pipelineVersion'

interface TimelineSnapshot {
  id: string
  snapshot_version: number
  tag: string | null
  created_at: string | null
  version_kind: string
  created_kind: string
  channel: string
  draft: boolean
}

/** FAR-1817: a version the history timeline shows as its own row. */
interface VersionGroup {
  /** Stable key for the expand/collapse state (unique per anchor). */
  key: string
  /** The snapshot the row renders and deep-links to. */
  anchor: TimelineSnapshot
  /** Badge kind: the anchor's created_kind (a real version, never a fabricated one). */
  kind: string
  /** Run snapshots that executed this version, newest first. */
  runs: TimelineSnapshot[]
}

const props = defineProps<{
  pipelineId: string
  /**
   * FAR-1895: the pipeline's resolved CURRENT version (the same value the
   * toolbar chip shows), resolved by the host through a query no run streak
   * can starve. Used ONLY as the anchor when this panel's own (windowed) page
   * contains no substantive row — see groupHistory. Optional so the component
   * still mounts standalone; absent it renders the explicit
   * "no version in the loaded history" state instead of inventing one.
   */
  currentVersion?: TimelineSnapshot | null
}>()
// FAR-1812: `view` asks the host editor to open this snapshot as a read-only
// historical version (deep link).
// FAR-1816: `restore` hands the selected version to the host editor's shared
// restore dialog (the version number travels with the id so the dialog can
// name it even when the row is outside the host's snapshot page).
const emit = defineEmits<{ close: []; view: [id: string]; restore: [id: string, version: number] }>()

const { t } = useI18n()

const loading = ref(true)
const loadError = ref<string | null>(null)
const loadingMore = ref(false)
const loadMoreError = ref<string | null>(null)
const snapshots = ref<TimelineSnapshot[]>([])
const snapshotsTotal = ref(0)
const snapshotsPage = ref(1)
const compareB = ref<string | null>(null)
const diffResult = ref<Record<string, any> | null>(null)
const selectedSnapshotId = ref<string | null>(null)
const actionError = ref<string | null>(null)
const expandedGroups = ref<Set<string>>(new Set())

// M6: only the newest request's result may be applied. A mount load, a
// host-triggered reload after a restore, and a "load more" all share the token
// so a slow older response can never overwrite a newer one.
let snapshotsRequestSeq = 0
// M6: an earlier restore-dialog open must not land after a later one and show
// the wrong version's summary beside an enabled Confirm.
let diffRequestSeq = 0

const hasMore = computed(() => snapshots.value.length < snapshotsTotal.value)

function isRenderableSnapshot(s: TimelineSnapshot): boolean {
  // FAR-1817: match the editor's own snapshot load — a version-0 placeholder
  // must never render as a row or capture every run.
  // FAR-1895: a draft auto-save (`draft: true`, created_kind='edit') is not a
  // version. The request excludes drafts server-side; drop any that still
  // arrive so a draft can never be a version row (nor be mistaken for a run
  // annotation, which would attribute it to a version it did not execute).
  return typeof s.snapshot_version === 'number' && s.snapshot_version > 0 && s.draft !== true
}

/**
 * FAR-1817 grouping. Snapshots arrive version-DESCENDING.
 *
 * Every substantive snapshot becomes a primary row. A run snapshot belongs to
 * the substantive version it executed — the newest substantive version at or
 * below the run's own version number, since versions are allocated
 * monotonically and a run freezes the live graph at run-start (the state the
 * newest prior substantive version recorded).
 *
 * FAR-1895: a loaded window with NO substantive row is NOT evidence that the
 * pipeline has no version — the window is bounded (page_size + "Load more"),
 * and a run streak longer than the window pushes the newest version below it.
 * The panel therefore anchors on the host-resolved `currentVersion` (the same
 * value the chip shows, resolved by a query no run streak can starve) when the
 * window has no substantive row, and renders an explicit "no version in the
 * loaded history" state when even that is unavailable. It never promotes a run
 * to a fabricated `initial` version: that is exactly how the timeline could
 * show "v500 Initial" while the chip showed "v2".
 *
 * M2: runs OLDER than every substantive version in the window are NOT
 * attributed to any version — they are returned as `earlierRuns` and rendered
 * as an explicit "earlier versions" bucket. Folding them onto the oldest row
 * would assert a provenance the run does not have.
 *
 * FAR-1895: a window with NO substantive row AND no resolved `currentVersion`
 * is a THIRD case, distinct from M2. Those runs are not known to be earlier —
 * rows arrive version-DESC, so an all-run window holds the NEWEST runs (the
 * newest version is below the window, or the query that resolves it failed).
 * They are returned as `unattributedRuns` and rendered without any version
 * claim, never folded into the "earlier" bucket, which would assert a
 * provenance (and a direction) the rows do not have.
 */
function groupHistory(
  rows: TimelineSnapshot[],
  currentVersion: TimelineSnapshot | null,
): { groups: VersionGroup[]; earlierRuns: TimelineSnapshot[]; unattributedRuns: TimelineSnapshot[] } {
  const substantive = rows.filter((s) => isSubstantiveKind(s.created_kind))
  const runs = rows.filter((s) => !isSubstantiveKind(s.created_kind))
  if (substantive.length === 0) {
    if (runs.length === 0) return { groups: [], earlierRuns: [], unattributedRuns: [] }
    if (!currentVersion) {
      // No substantive row in the window and no resolved version to anchor on:
      // we cannot say whether these runs executed a version newer or older than
      // the window, so they carry NO version attribution and are NOT labelled
      // "earlier". The panel states the gap; the runs stay reachable here.
      return { groups: [], earlierRuns: [], unattributedRuns: runs }
    }
    // The resolved version sits BELOW every run in the window (it is the
    // newest version and is absent from the window), so each run executed it —
    // attributing them to it is truthful, and clicking the row opens it.
    return {
      groups: [{
        key: currentVersion.id,
        anchor: currentVersion,
        kind: currentVersion.created_kind,
        runs,
      }],
      earlierRuns: [],
      unattributedRuns: [],
    }
  }
  const groups: VersionGroup[] = substantive.map((s) => ({
    key: s.id,
    anchor: s,
    kind: s.created_kind,
    runs: [],
  }))
  // Groups are version-DESC, so the first anchor at or below a run is the
  // newest substantive version it executed. Anything below EVERY anchor has no
  // substantive version it can be attributed to and goes to `earlierRuns`.
  const earlierRuns: TimelineSnapshot[] = []
  for (const run of runs) {
    const group = groups.find((g) => g.anchor.snapshot_version <= run.snapshot_version)
    if (group) group.runs.push(run)
    else earlierRuns.push(run)
  }
  return { groups, earlierRuns, unattributedRuns: [] }
}

const groupedHistory = computed(() => groupHistory(snapshots.value, props.currentVersion ?? null))
const versionGroups = computed(() => groupedHistory.value.groups)
const earlierRuns = computed(() => groupedHistory.value.earlierRuns)
const unattributedRuns = computed(() => groupedHistory.value.unattributedRuns)

const compareOptions = computed(() =>
  versionGroups.value.map((g) => {
    // S4624: build the optional tag suffix separately rather than nesting a
    // second template literal inside the row label.
    const tagSuffix = g.anchor.tag ? ` — ${g.anchor.tag}` : ''
    return {
      value: g.anchor.id,
      label: `v${g.anchor.snapshot_version} — ${kindLabel(g.kind)}${tagSuffix}`,
    }
  }),
)

const canDiff = computed(() => compareB.value != null && selectedSnapshotId.value != null && compareB.value !== selectedSnapshotId.value)

const impactedLabel = computed(() => {
  const ids = diffResult.value?.semantic?.impacted_nodes ?? []
  if (!Array.isArray(ids) || ids.length === 0) return ''
  return ids.map((id: unknown) => String(id).slice(0, 8)).join(', ')
})

// FAR-1817: one shared created_kind→label mapping (lib/pipelineVersion), used
// here and by the version chip. An unknown/future kind renders its raw value
// (visible) rather than being silently mislabelled as a run.
function kindLabel(kind: string): string {
  return createdKindLabel(kind, t, t('components.PipelineVersionChip.no_value'))
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) return '—'
  try {
    return formatDateShort(new Date(dateStr))
  } catch (e: unknown) {
    return dateStr
  }
}

const selectedSnapshot = computed(() => {
  const group = versionGroups.value.find(g => g.anchor.id === selectedSnapshotId.value)
  return group?.anchor ?? null
})

/** FAR-1817: expand/collapse the run annotation for one version. */
function toggleGroup(key: string): void {
  const next = new Set(expandedGroups.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  expandedGroups.value = next
}

/** FAR-1816: hand the selected version to the host's shared restore dialog. */
function requestRestore(): void {
  const snapshot = selectedSnapshot.value
  if (!snapshot) return
  emit('restore', snapshot.id, snapshot.snapshot_version)
}

/** Drop a selection that is no longer present in the (re)loaded page. */
function reconcileSelection(): void {
  if (!versionGroups.value.some(g => g.anchor.id === selectedSnapshotId.value)) {
    selectedSnapshotId.value = null
  }
}

async function loadSnapshots(): Promise<void> {
  const seq = ++snapshotsRequestSeq
  loading.value = true
  loadError.value = null
  loadMoreError.value = null
  // A full reload resets to page 1, so any in-flight "load more" is superseded
  // and must not leave the button stuck in its loading state.
  loadingMore.value = false
  try {
    // FAR-1817: the grouping annotates each substantive version with the run
    // snapshots that executed it, so load the widest window the endpoint
    // allows (page_size caps at 100) rather than the 20 that sufficed for a
    // flat list.
    // FAR-1895: `exclude_drafts` drops in-progress auto-saves from the window
    // (and from `total`, so the truncation notice stays honest). Runs are
    // committed snapshots, so they are kept for the annotation.
    const { data, error } = await withTimeout((signal) =>
      api.GET('/api/v1/pipelines/{pipeline_id}/snapshots', {
        params: {
          path: { pipeline_id: props.pipelineId },
          query: { page_size: SNAPSHOT_PAGE_SIZE, exclude_drafts: true },
        },
        signal,
      }),
    )
    if (seq !== snapshotsRequestSeq) return
    if (error) {
      // A failed load is NOT an empty history: keep any rows already shown and
      // surface the failure with a retry (M3).
      loadError.value = formatApiError(error)
      return
    }
    const payload = data as unknown as { items?: TimelineSnapshot[]; total?: number } | null
    snapshots.value = (payload?.items ?? []).filter(isRenderableSnapshot)
    snapshotsTotal.value = typeof payload?.total === 'number' ? payload.total : snapshots.value.length
    snapshotsPage.value = 1
    reconcileSelection()
  } catch (e: unknown) {
    if (seq !== snapshotsRequestSeq) return
    loadError.value = formatApiError(e)
  } finally {
    if (seq === snapshotsRequestSeq) loading.value = false
  }
}

/** M1: fetch the next page of history and append it (history is windowed). */
async function loadMoreSnapshots(): Promise<void> {
  if (loadingMore.value || !hasMore.value) return
  const seq = ++snapshotsRequestSeq
  loadingMore.value = true
  loadMoreError.value = null
  try {
    const nextPage = snapshotsPage.value + 1
    const { data, error } = await withTimeout((signal) =>
      api.GET('/api/v1/pipelines/{pipeline_id}/snapshots', {
        params: {
          path: { pipeline_id: props.pipelineId },
          query: { page_size: SNAPSHOT_PAGE_SIZE, page: nextPage, exclude_drafts: true },
        },
        signal,
      }),
    )
    if (seq !== snapshotsRequestSeq) return
    if (error) {
      loadMoreError.value = formatApiError(error)
      return
    }
    const payload = data as unknown as { items?: TimelineSnapshot[]; total?: number } | null
    const next = (payload?.items ?? []).filter(isRenderableSnapshot)
    const seen = new Set(snapshots.value.map((s) => s.id))
    snapshots.value = [...snapshots.value, ...next.filter((s) => !seen.has(s.id))]
    if (typeof payload?.total === 'number') snapshotsTotal.value = payload.total
    snapshotsPage.value = nextPage
  } catch (e: unknown) {
    if (seq !== snapshotsRequestSeq) return
    loadMoreError.value = formatApiError(e)
  } finally {
    if (seq === snapshotsRequestSeq) loadingMore.value = false
  }
}

async function runDiff(): Promise<void> {
  if (!selectedSnapshotId.value || !compareB.value) return
  const snapshotA = selectedSnapshotId.value
  const snapshotB = compareB.value
  const seq = ++diffRequestSeq
  actionError.value = null
  // M4: clear the previous result BEFORE the new request so a failure never
  // leaves a stale diff on screen next to a fresh error.
  diffResult.value = null
  try {
    const { data, error } = await withTimeout((signal) =>
      api.POST('/api/v1/pipelines/{pipeline_id}/snapshots/diff', {
        params: { path: { pipeline_id: props.pipelineId } },
        body: { snapshot_a_id: snapshotA, snapshot_b_id: snapshotB },
        signal,
      }),
    )
    if (seq !== diffRequestSeq) return
    if (error) {
      actionError.value = formatApiError(error)
      return
    }
    diffResult.value = (data as Record<string, any>) ?? null
  } catch (e: unknown) {
    if (seq !== diffRequestSeq) return
    actionError.value = formatApiError(e)
  }
}

onMounted(loadSnapshots)

// FAR-1816: the host editor re-fetches after a successful restore so the newly
// created version shows up here without the user reopening the timeline.
defineExpose({ reload: loadSnapshots })
</script>
