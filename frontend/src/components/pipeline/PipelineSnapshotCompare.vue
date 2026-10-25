<template>
  <!-- FAR-1818: semantic-led version compare view. A heavily-changed pipeline
       must read as a short narrative ("N nodes added, M removed, port signature
       changed on K -> these J downstream nodes are impacted"), NOT a wall of
       JSON. The backend semantic layer (breaking_changes + impacted_nodes) is
       presented FIRST; the raw JSON diff is the LAST thing and is behind a
       toggle that is OFF by default. No backend endpoint is added or changed —
       this binds to the existing POST /snapshots/diff response. -->
  <section
    class="flex h-full flex-col gap-4 p-4"
    :aria-label="$t('components.PipelineSnapshotCompare.region_label')"
    data-testid="pipeline-compare"
  >
    <!-- Header: pair pickers (deep-linkable) + close -->
    <div class="flex flex-wrap items-end justify-between gap-3">
      <div class="flex flex-wrap items-end gap-3">
        <div class="flex flex-col gap-1 text-xs">
          <span class="font-medium text-muted-foreground">{{ $t('components.PipelineSnapshotCompare.base_label') }}</span>
          <Select
            :model-value="baseSnapshotId"
            :options="snapshotOptions"
            option-value="value"
            option-label="label"
            :aria-label="$t('components.PipelineSnapshotCompare.base_label')"
            data-testid="pipeline-compare-base"
            @update:model-value="onBaseChange"
          />
        </div>
        <div class="flex flex-col gap-1 text-xs">
          <span class="font-medium text-muted-foreground">{{ $t('components.PipelineSnapshotCompare.compare_label') }}</span>
          <Select
            :model-value="effectiveCompareId"
            :options="snapshotOptions"
            option-value="value"
            option-label="label"
            :aria-label="$t('components.PipelineSnapshotCompare.compare_label')"
            data-testid="pipeline-compare-against"
            @update:model-value="onCompareChange"
          />
        </div>
      </div>
      <button
        type="button"
        class="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
        :aria-label="$t('components.PipelineSnapshotCompare.close')"
        data-testid="pipeline-compare-close"
        @click="emit('close')"
      >
        <XIcon class="h-4 w-4" aria-hidden="true" />
      </button>
    </div>

    <output
      v-if="status === 'loading'"
      class="block text-sm text-muted-foreground"
      data-testid="pipeline-compare-loading"
    >
      {{ $t('components.PipelineSnapshotCompare.loading') }}
    </output>

    <p
      v-else-if="!canCompare"
      class="text-sm text-muted-foreground"
      data-testid="pipeline-compare-no-target"
    >
      {{ $t('components.PipelineSnapshotCompare.no_target') }}
    </p>

    <!-- Truthful self-compare state: the same version on both sides has no
         meaningful diff, so it is reported as such instead of showing a
         meaningless all-zero comparison. -->
    <output
      v-else-if="isSelfCompare"
      class="block text-sm text-muted-foreground"
      data-testid="pipeline-compare-self"
    >
      {{ $t('components.PipelineSnapshotCompare.self_compare') }}
    </output>

    <!-- VIS-3: the shared ErrorAlert owns this region's box, message and
         retry affordance; the wrapper keeps the assertive role and the BDD
         testid the compare contract pins. -->
    <div
      v-else-if="status === 'error'"
      role="alert"
      data-testid="pipeline-compare-error"
    >
      <ErrorAlert
        compact
        :message="errorDetail"
        :on-retry="retryCompare"
      />
    </div>

    <template v-else>
      <!-- 1. SEMANTIC LAYER FIRST: what broke, and what it impacts. -->
      <section
        class="space-y-2"
        data-testid="pipeline-compare-semantic"
      >
        <output
          class="block rounded-lg border bg-muted/30 p-3 text-sm font-medium"
          data-testid="pipeline-compare-summary"
        >
          {{ summaryLine }}
        </output>

        <div
          v-if="breakingChanges.length > 0"
          class="space-y-1"
          data-testid="pipeline-compare-breaking"
        >
          <h3 class="text-xs font-semibold text-destructive">{{ $t('components.PipelineSnapshotCompare.breaking_title') }}</h3>
          <div
            v-for="(change, i) in breakingChanges"
            :key="i"
            class="rounded-md border border-destructive/30 bg-destructive/5 px-2 py-1 text-[11px]"
            data-testid="pipeline-compare-breaking-item"
          >
            <span :class="severityClass(change.severity)">{{ severityLabel(change.severity) }}</span>
            <span> — {{ change.reason }}</span>
          </div>
        </div>
        <p
          v-else
          class="text-xs text-muted-foreground"
          data-testid="pipeline-compare-breaking-none"
        >
          {{ $t('components.PipelineSnapshotCompare.no_breaking') }}
        </p>

        <div class="text-xs" data-testid="pipeline-compare-impacted">
          <span class="font-medium">{{ $t('components.PipelineSnapshotCompare.impacted_title', { count: impactedNodes.length }) }}</span>
          <span v-if="impactedNodes.length === 0" class="text-muted-foreground"> {{ $t('components.PipelineSnapshotCompare.impacted_none') }}</span>
          <ul v-else class="mt-1 list-inside list-disc space-y-0.5 text-muted-foreground">
            <li v-for="id in impactedNodes" :key="id">{{ nodeLabel(id) }}</li>
          </ul>
        </div>
      </section>

      <!-- 2. STRUCTURAL CHANGES, grouped per node; unchanged collapsed. -->
      <section class="space-y-3" data-testid="pipeline-compare-structural">
        <h3 class="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          {{ $t('components.PipelineSnapshotCompare.structural_title') }}
        </h3>

        <p
          v-if="!hasStructuralChanges"
          class="text-xs text-muted-foreground"
          data-testid="pipeline-compare-structural-none"
        >
          {{ $t('components.PipelineSnapshotCompare.no_structural_changes') }}
        </p>

        <template v-else>
          <div v-if="nodesAdded.length > 0" data-testid="pipeline-compare-nodes-added">
            <h4 class="text-xs font-medium">{{ $t('components.PipelineSnapshotCompare.nodes_added_title', { count: nodesAdded.length }) }}</h4>
            <ul class="mt-1 space-y-0.5 text-[11px]">
              <li v-for="(node, i) in nodesAdded" :key="nodeId(node, i)" class="flex items-center gap-1">
                <span class="font-medium">{{ nodeText(node) }}</span>
                <span class="text-muted-foreground">{{ nodeTypeOf(node) }}</span>
              </li>
            </ul>
          </div>

          <div v-if="nodesRemoved.length > 0" data-testid="pipeline-compare-nodes-removed">
            <h4 class="text-xs font-medium">{{ $t('components.PipelineSnapshotCompare.nodes_removed_title', { count: nodesRemoved.length }) }}</h4>
            <ul class="mt-1 space-y-0.5 text-[11px]">
              <li v-for="(node, i) in nodesRemoved" :key="nodeId(node, i)" class="flex items-center gap-1">
                <span class="font-medium">{{ nodeText(node) }}</span>
                <span class="text-muted-foreground">{{ nodeTypeOf(node) }}</span>
              </li>
            </ul>
          </div>

          <div v-if="nodesModified.length > 0" data-testid="pipeline-compare-nodes-modified">
            <h4 class="text-xs font-medium">{{ $t('components.PipelineSnapshotCompare.nodes_modified_title', { count: nodesModified.length }) }}</h4>
            <div
              v-for="(modified, i) in nodesModified"
              :key="modified.node_id ?? i"
              class="mt-1 rounded-md border px-2 py-1 text-[11px]"
              data-testid="pipeline-compare-node-modified"
            >
              <p class="font-medium">{{ nodeLabel(modified.node_id) }}</p>
              <ul class="mt-0.5 space-y-0.5 text-muted-foreground">
                <li v-for="field in fieldEntries(modified.changes)" :key="field.key">
                  {{ $t('components.PipelineSnapshotCompare.field_changed', {
                    field: fieldLabel(field.key),
                    old: formatValue(field.old),
                    new: formatValue(field.new),
                  }) }}
                </li>
                <li v-for="(port, pi) in portsOf(modified.changes)" :key="`port-${pi}`">
                  {{ portChangeLabel(port) }}
                </li>
              </ul>
            </div>
          </div>

          <div v-if="edgesAdded.length > 0" data-testid="pipeline-compare-edges-added">
            <h4 class="text-xs font-medium">{{ $t('components.PipelineSnapshotCompare.edges_added_title', { count: edgesAdded.length }) }}</h4>
            <ul class="mt-1 space-y-0.5 text-[11px] text-muted-foreground">
              <li v-for="(edge, i) in edgesAdded" :key="`ea-${i}`">+ {{ edgeText(edge) }}</li>
            </ul>
          </div>

          <div v-if="edgesRemoved.length > 0" data-testid="pipeline-compare-edges-removed">
            <h4 class="text-xs font-medium">{{ $t('components.PipelineSnapshotCompare.edges_removed_title', { count: edgesRemoved.length }) }}</h4>
            <ul class="mt-1 space-y-0.5 text-[11px] text-muted-foreground">
              <li v-for="(edge, i) in edgesRemoved" :key="`er-${i}`">− {{ edgeText(edge) }}</li>
            </ul>
          </div>

          <div v-if="edgesModified.length > 0" data-testid="pipeline-compare-edges-modified">
            <h4 class="text-xs font-medium">{{ $t('components.PipelineSnapshotCompare.edges_modified_title', { count: edgesModified.length }) }}</h4>
            <ul class="mt-1 space-y-0.5 text-[11px] text-muted-foreground">
              <li v-for="(modified, i) in edgesModified" :key="`em-${i}`">
                {{ modifiedEdgeText(modified) }}
              </li>
            </ul>
          </div>

          <!-- Populated collapse: expanding lists the nodes present in BOTH
               versions that did not change, instead of revealing nothing. -->
          <details
            v-if="unchangedCount > 0"
            class="text-[11px] text-muted-foreground"
            data-testid="pipeline-compare-unchanged"
          >
            <summary class="cursor-pointer">{{ $t('components.PipelineSnapshotCompare.unchanged_summary', { count: unchangedCount }, unchangedCount) }}</summary>
            <ul class="mt-1 list-inside list-disc space-y-0.5" data-testid="pipeline-compare-unchanged-list">
              <li v-for="id in unchangedNodeIds" :key="id">{{ nodeLabel(id) }}</li>
            </ul>
          </details>
        </template>
      </section>

      <!-- 3. RAW JSON, last and hidden by default. The REGION is always in the
           DOM so the toggle's aria-controls target is valid while collapsed;
           only the payload appears when expanded. -->
      <div data-testid="pipeline-compare-raw-section">
        <button
          type="button"
          class="rounded-md border border-input bg-background px-2 py-1 text-xs font-medium hover:bg-accent"
          :aria-expanded="showRaw"
          :aria-controls="rawRegionId"
          data-testid="pipeline-compare-raw-toggle"
          @click="showRaw = !showRaw"
        >
          {{ showRaw ? $t('components.PipelineSnapshotCompare.raw_hide') : $t('components.PipelineSnapshotCompare.raw_show') }}
        </button>
        <section :id="rawRegionId" :aria-label="$t('components.PipelineSnapshotCompare.raw_region_label')">
          <pre
            v-if="showRaw"
            class="mt-2 max-h-96 overflow-auto rounded-lg border bg-muted/40 p-3 text-[11px]"
            data-testid="pipeline-compare-raw"
          >{{ rawJson }}</pre>
        </section>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Select from '../shared/AppSelect.vue'
import ErrorAlert from '../shared/ErrorAlert.vue'
import { api } from '../../lib/api/client'
import { formatApiError } from '../../lib/api/formatError'
import { withTimeout, RequestTimeoutError } from '../../lib/requestTimeout'
import {
  hasStructuralChanges as diffHasStructuralChanges,
  isBlockingSeverity,
  readBreakingChanges,
  readChangeClassCounts,
  readChangeClasses,
  readFrozenGraphNodes,
  readImpactedNodes,
  readNodeSignatureNodeIds,
  readUnchangedNodeIds,
  severityLabel as resolveSeverityLabel,
  shortId,
  type NodePortDelta,
  type SnapshotDiffEdge,
  type SnapshotDiffNode,
  type SnapshotDiffResponse,
  type SnapshotModifiedEdge,
} from '../../lib/pipelineSnapshotDiff'
import { X as XIcon } from '@lucide/vue'

/** One selectable version in the pair pickers. */
interface CompareSnapshot {
  id: string
  snapshot_version: number
  tag: string | null
  created_at: string | null
}

/** Raw snapshot row as returned by the list/detail endpoints. */
interface RawSnapshot {
  id?: string
  snapshot_version?: number
  tag?: string | null
  created_at?: string | null
}

const props = defineProps<{
  pipelineId: string
  /** The "old" side — the resolved `?snapshot=` id. */
  baseSnapshotId: string
  /** The "new" side (`?compare=`); null means "default to the latest version". */
  compareSnapshotId: string | null
  snapshots: CompareSnapshot[]
  latestSnapshotId: string | null
}>()

const emit = defineEmits<{
  change: [payload: { base: string; compare: string }]
  close: []
}>()

const { t } = useI18n()

type CompareStatus = 'idle' | 'loading' | 'ready' | 'error'

const status = ref<CompareStatus>('idle')
const diff = ref<SnapshotDiffResponse | null>(null)
const errorMessage = ref<string | null>(null)
const showRaw = ref(false)

// ErrorAlert renders a single message line, so carry the region's headline into
// that message — the same `title: detail` shape it uses for a ProblemDetail.
const errorDetail = computed(
  () => `${t('components.PipelineSnapshotCompare.error_title')}: ${errorMessage.value ?? ''}`,
)

const rawRegionId = 'pipeline-compare-raw-region'
const rawJson = computed(() => JSON.stringify(diff.value, null, 2))

// `compare` defaults to the latest version when the query carries no id.
const effectiveCompareId = computed(() => props.compareSnapshotId ?? props.latestSnapshotId ?? '')
const canCompare = computed(() => props.baseSnapshotId !== '' && effectiveCompareId.value !== '')
// Comparing a version against itself has no meaningful diff.
const isSelfCompare = computed(() => canCompare.value && props.baseSnapshotId === effectiveCompareId.value)

// --- Full version history for the pickers (FAR-1818 M-C) -------------------
// The host's `snapshots` prop is the catalog's FIRST page (default page size
// 20), so on a long-lived pipeline the picker could not offer an old pair.
// The component pages the whole list itself (bounded), merges the host's page
// in, and targeted-fetches any deep-linked id absent from the merged set — so
// the picker reaches the whole history AND a deep link never renders a blank
// Select.
const CATALOG_PAGE_SIZE = 100
const CATALOG_MAX_PAGES = 20

const fullCatalog = ref<CompareSnapshot[]>([])
const extraSnapshots = ref<CompareSnapshot[]>([])
const fetchedSnapshotIds = new Set<string>()
let catalogToken = 0

function readSnapshot(raw: RawSnapshot | null | undefined): CompareSnapshot | null {
  if (!raw?.id || typeof raw.snapshot_version !== 'number') return null
  return {
    id: raw.id,
    snapshot_version: raw.snapshot_version,
    tag: raw.tag ?? null,
    created_at: raw.created_at ?? null,
  }
}

const mergedSnapshots = computed<CompareSnapshot[]>(() => {
  const byId = new Map<string, CompareSnapshot>()
  for (const list of [props.snapshots, fullCatalog.value, extraSnapshots.value]) {
    for (const snap of list) {
      if (snap?.id && !byId.has(snap.id)) byId.set(snap.id, snap)
    }
  }
  return [...byId.values()].sort((a, b) => b.snapshot_version - a.snapshot_version)
})

function snapshotOptionLabel(snap: CompareSnapshot): string {
  return snap.tag
    ? t('components.PipelineSnapshotCompare.version_option_tagged', { version: snap.snapshot_version, tag: snap.tag })
    : t('components.PipelineSnapshotCompare.version_option', { version: snap.snapshot_version })
}

const snapshotOptions = computed(() => {
  const options = mergedSnapshots.value.map((snap) => ({ value: snap.id, label: snapshotOptionLabel(snap) }))
  const known = new Set(options.map((option) => option.value))
  // A deep-linked snapshot that no loaded list carries still gets a real,
  // meaningful option so the Select never shows a blank/mismatched value.
  for (const id of [props.baseSnapshotId, effectiveCompareId.value]) {
    if (id && !known.has(id)) {
      options.push({
        value: id,
        label: t('components.PipelineSnapshotCompare.version_option_unknown', { id: shortId(id) }),
      })
      known.add(id)
    }
  }
  return options
})

/** One page of the snapshot catalog as read for the pickers. */
interface CatalogPage {
  /** Raw item count before unreadable rows are dropped — this drives paging. */
  rawCount: number
  /** The readable snapshots on this page (a real, positive version). */
  snaps: CompareSnapshot[]
  /** The server's declared total, when it reports a finite one. */
  total: number | null
}

/** Read one catalog page; null when it cannot be read (transient failure). */
async function fetchCatalogPage(page: number): Promise<CatalogPage | null> {
  const { data, error } = await withTimeout((signal) =>
    api.GET('/api/v1/pipelines/{pipeline_id}/snapshots', {
      params: { path: { pipeline_id: props.pipelineId }, query: { page, page_size: CATALOG_PAGE_SIZE } },
      signal,
    }),
  )
  if (error || !data) return null
  const items = data.items ?? []
  const snaps: CompareSnapshot[] = []
  for (const raw of items) {
    const snap = readSnapshot(raw)
    if (snap && snap.snapshot_version > 0) snaps.push(snap)
  }
  return {
    rawCount: items.length,
    snaps,
    total: Number.isFinite(data.total) ? (data.total as number) : null,
  }
}

/**
 * True when this page is the last worth fetching: an empty page, or the
 * server's own total reached, or (when the total is unknown) a short page.
 */
function isLastCatalogPage(rawCount: number, seen: number, total: number | null): boolean {
  if (rawCount === 0) return true
  if (total !== null) return seen >= total
  return rawCount < CATALOG_PAGE_SIZE
}

/** Page the whole snapshot history (bounded); null when a newer load superseded it. */
async function collectCatalogPages(token: number): Promise<CompareSnapshot[] | null> {
  const collected: CompareSnapshot[] = []
  let total: number | null = null
  let seen = 0
  for (let page = 1; page <= CATALOG_MAX_PAGES; page += 1) {
    const result = await fetchCatalogPage(page)
    if (token !== catalogToken) return null
    if (!result) break
    seen += result.rawCount
    collected.push(...result.snaps)
    if (total === null && result.total !== null) total = result.total
    if (isLastCatalogPage(result.rawCount, seen, total)) break
  }
  return collected
}

async function loadFullCatalog(): Promise<void> {
  const token = ++catalogToken
  try {
    const collected = await collectCatalogPages(token)
    if (token !== catalogToken || collected === null) return
    fullCatalog.value = collected
  } catch (e: unknown) {
    if (token !== catalogToken) return
    // A failed history load must never blank the picker: keep the host's page 1
    // and the deep-link fallback label.
    console.warn('Failed to load the full snapshot history for compare', e)
  }
}

async function ensureSnapshotLoaded(id: string | null): Promise<void> {
  if (!id || fetchedSnapshotIds.has(id)) return
  if (mergedSnapshots.value.some((snap) => snap.id === id)) return
  try {
    const { data, error } = await withTimeout((signal) =>
      api.GET('/api/v1/pipelines/{pipeline_id}/snapshots/{snapshot_id}', {
        params: { path: { pipeline_id: props.pipelineId, snapshot_id: id } },
        signal,
      }),
    )
    // Only a SUCCESSFUL lookup marks the id as fetched. A transient failure
    // (network blip, 5xx, timeout) must not permanently strand the picker on
    // the fallback label — leaving the id out of the set lets the next time it
    // is needed retry it.
    if (error || !data) return
    fetchedSnapshotIds.add(id)
    const snap = readSnapshot(data)
    if (snap) extraSnapshots.value = [...extraSnapshots.value, snap]
  } catch {
    // A transient failure (timeout, network) keeps the fallback option label
    // AND leaves the id unmarked, so a later Retry re-attempts it.
  }
}

// FAR-1893: the change-class enumeration lives in the shared module — this
// component never reads a raw response key, and neither do its siblings.
const changeClasses = computed(() => readChangeClasses(diff.value))
const changeCounts = computed(() => readChangeClassCounts(diff.value))
const nodesAdded = computed(() => changeClasses.value.nodesAdded)
const nodesRemoved = computed(() => changeClasses.value.nodesRemoved)
const nodesModified = computed(() => changeClasses.value.nodesModified)
const edgesAdded = computed(() => changeClasses.value.edgesAdded)
const edgesRemoved = computed(() => changeClasses.value.edgesRemoved)
const edgesModified = computed(() => changeClasses.value.edgesModified)


// FAR-1893: the semantic layer and the port-signature rule are interpreted by
// the shared module, so the timeline and this view can never disagree about
// what the diff says.
const breakingChanges = computed(() => readBreakingChanges(diff.value))
const impactedNodes = computed(() => readImpactedNodes(diff.value))
const portChangedNodeCount = computed(() => readNodeSignatureNodeIds(diff.value).length)

const hasStructuralChanges = computed(() => diffHasStructuralChanges(diff.value))

const unchangedNodeIds = computed<string[]>(() => readUnchangedNodeIds(diff.value))

const unchangedCount = computed(() => unchangedNodeIds.value.length)

function plural(key: string, count: number): string {
  return t(key, { count }, count)
}

const summaryLine = computed(() => {
  const counts = changeCounts.value
  const parts = [
    plural('components.PipelineSnapshotCompare.summary_node_added', counts.nodesAdded),
    plural('components.PipelineSnapshotCompare.summary_node_removed', counts.nodesRemoved),
    plural('components.PipelineSnapshotCompare.summary_node_modified', counts.nodesModified),
    plural('components.PipelineSnapshotCompare.summary_edge_added', counts.edgesAdded),
    plural('components.PipelineSnapshotCompare.summary_edge_removed', counts.edgesRemoved),
    plural('components.PipelineSnapshotCompare.summary_edge_modified', counts.edgesModified),
  ]
  if (portChangedNodeCount.value > 0) {
    parts.push(plural('components.PipelineSnapshotCompare.summary_ports', portChangedNodeCount.value))
  }
  parts.push(
    impactedNodes.value.length > 0
      ? plural('components.PipelineSnapshotCompare.summary_impacted', impactedNodes.value.length)
      : t('components.PipelineSnapshotCompare.summary_impacted_none'),
  )
  return parts.join(' · ')
})

/** node id -> display label, from the versions' frozen graphs (B wins). */
const nodeLabels = computed<Record<string, string>>(() => {
  const map: Record<string, string> = {}
  const add = (nodes?: SnapshotDiffNode[]) => {
    for (const node of nodes ?? []) {
      if (node?.id) map[node.id] = node.label || shortId(node.id)
    }
  }
  add(readFrozenGraphNodes(diff.value, 'a'))
  add(readFrozenGraphNodes(diff.value, 'b'))
  return map
})

function nodeLabel(id?: string | null): string {
  if (!id) return t('components.PipelineSnapshotCompare.no_value')
  return nodeLabels.value[id] ?? shortId(id)
}

function nodeId(node: SnapshotDiffNode, index: number): string {
  return node.id ?? `node-${index}`
}

function nodeText(node: SnapshotDiffNode): string {
  if (!node.id) return t('components.PipelineSnapshotCompare.no_value')
  return nodeLabels.value[node.id] ?? node.label ?? shortId(node.id)
}

function nodeTypeOf(node: SnapshotDiffNode): string {
  return node.node_type ?? ''
}

function edgeEndpoint(edge: { source?: string; target?: string; source_node_id?: string; target_node_id?: string }): string {
  const source = edge.source ?? edge.source_node_id
  const target = edge.target ?? edge.target_node_id
  return `${nodeLabel(source)} → ${nodeLabel(target)}`
}

function edgeText(edge: SnapshotDiffEdge): string {
  return edgeEndpoint(edge)
}

function modifiedEdgeText(modified: SnapshotModifiedEdge): string {
  const ends = edgeEndpoint(modified.edge ?? {})
  // Edge `changes.ports` is a `{source_port:{old,new}, target_port:{old,new}}`
  // delta DICT (unlike a node's list), so it needs its own reader — a repointed
  // edge must not render as a bare "A → B" with the port delta dropped.
  const changes = [...fieldEntries(modified.changes), ...edgePortEntries(modified.changes)].map(
    (field) =>
      t('components.PipelineSnapshotCompare.field_changed', {
        field: fieldLabel(field.key),
        old: formatValue(field.old),
        new: formatValue(field.new),
      }),
  )
  return changes.length > 0 ? `${ends} (${changes.join('; ')})` : ends
}

const FIELD_LABEL_KEYS: Record<string, string> = {
  agent_id: 'components.PipelineSnapshotCompare.field_agent',
  label: 'components.PipelineSnapshotCompare.field_label',
  node_type: 'components.PipelineSnapshotCompare.field_node_type',
  schema_id: 'components.PipelineSnapshotCompare.field_schema',
  connector_binding: 'components.PipelineSnapshotCompare.field_connector',
  environment_binding: 'components.PipelineSnapshotCompare.field_environment',
  source_port: 'components.PipelineSnapshotCompare.field_source_port',
  target_port: 'components.PipelineSnapshotCompare.field_target_port',
  // Behaviour-bearing fields the diff reports so a config-only edit is never
  // presented as "no differences": a node-level HITL gate, its execution
  // bound, its template/model class, the one-PR-per-run isolation flag, an
  // edge's routing expression and an edge review gate.
  hitl_config: 'components.PipelineSnapshotCompare.field_hitl_config',
  hitl_review_config: 'components.PipelineSnapshotCompare.field_hitl_review_config',
  timeout_seconds: 'components.PipelineSnapshotCompare.field_timeout_seconds',
  template_id: 'components.PipelineSnapshotCompare.field_template_id',
  model_class: 'components.PipelineSnapshotCompare.field_model_class',
  single_pr_per_run: 'components.PipelineSnapshotCompare.field_single_pr_per_run',
  condition_expression: 'components.PipelineSnapshotCompare.field_condition_expression',
}

function fieldLabel(key: string): string {
  const i18nKey = FIELD_LABEL_KEYS[key]
  return i18nKey ? t(i18nKey) : key
}

/** Field changes are `{old, new}`; a bare value is shown as an addition. */
function fieldEntries(changes?: Record<string, unknown>): Array<{ key: string; old: unknown; new: unknown }> {
  if (!changes) return []
  const out: Array<{ key: string; old: unknown; new: unknown }> = []
  for (const [key, value] of Object.entries(changes)) {
    if (key === 'ports') continue
    const record = value as Record<string, unknown> | null
    if (record && typeof record === 'object' && !Array.isArray(record) && ('old' in record || 'new' in record)) {
      out.push({ key, old: record.old, new: record.new })
    } else {
      out.push({ key, old: value, new: undefined })
    }
  }
  return out
}

/** Node port deltas are a LIST of `{direction, port, change}` entries. */
function portsOf(changes?: Record<string, unknown>): NodePortDelta[] {
  const raw = changes?.ports
  if (!Array.isArray(raw)) return []
  // Shape-guard the entries: a malformed element must degrade, not throw on
  // the first `.direction` access (FAR-1893 robustness contract).
  return raw.filter(
    (entry): entry is NodePortDelta => typeof entry === 'object' && entry !== null,
  )
}

/** Edge port deltas are a `{source_port, target_port}` -> `{old, new}` dict. */
function edgePortEntries(changes?: Record<string, unknown>): Array<{ key: string; old: unknown; new: unknown }> {
  const raw = changes?.ports
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return []
  const out: Array<{ key: string; old: unknown; new: unknown }> = []
  for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
    const record = value as Record<string, unknown> | null
    if (record && typeof record === 'object' && ('old' in record || 'new' in record)) {
      out.push({ key, old: record.old, new: record.new })
    }
  }
  return out
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return t('components.PipelineSnapshotCompare.no_value')
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

function directionLabel(direction?: string): string {
  if (direction === 'input') return t('components.PipelineSnapshotCompare.direction_input')
  if (direction === 'output') return t('components.PipelineSnapshotCompare.direction_output')
  return direction ?? ''
}

function portChangeLabel(change: NodePortDelta): string {
  const direction = directionLabel(change.direction)
  const port = change.port ?? ''
  if (change.change === 'added') return t('components.PipelineSnapshotCompare.port_added', { direction, port })
  if (change.change === 'removed') return t('components.PipelineSnapshotCompare.port_removed', { direction, port })
  if (change.change === 'modified') return t('components.PipelineSnapshotCompare.port_modified', { direction, port })
  return t('components.PipelineSnapshotCompare.port_other', {
    direction,
    port,
    change: change.change ?? '',
  })
}

function severityClass(severity?: string): string {
  // The tone is this surface's presentation; WHICH severities are blocking is
  // shared interpretation (FAR-1893).
  return isBlockingSeverity(severity) ? 'font-medium text-destructive' : 'font-medium text-warning-text'
}

function severityLabel(severity?: string): string {
  return resolveSeverityLabel(severity, t)
}

function onBaseChange(value: unknown): void {
  if (typeof value !== 'string' || value === '') return
  emit('change', { base: value, compare: effectiveCompareId.value })
}

function onCompareChange(value: unknown): void {
  if (typeof value !== 'string' || value === '') return
  emit('change', { base: props.baseSnapshotId, compare: value })
}

const DIFF_REQUEST_TIMEOUT_MS = 15000
// Monotonic request token: a superseded diff must never write its result over a
// newer one. Paired with an AbortController so the old request is actually
// cancelled (and aborted on unmount / compare-mode exit).
let diffRequestToken = 0
let diffAbort: AbortController | null = null

async function loadDiff(): Promise<void> {
  const token = ++diffRequestToken
  diffAbort?.abort()
  diffAbort = null
  if (!canCompare.value || isSelfCompare.value) {
    status.value = 'idle'
    diff.value = null
    errorMessage.value = null
    return
  }
  const controller = new AbortController()
  diffAbort = controller
  status.value = 'loading'
  errorMessage.value = null
  try {
    const { data, error } = await withTimeout(
      (signal) =>
        api.POST('/api/v1/pipelines/{pipeline_id}/snapshots/diff', {
          params: { path: { pipeline_id: props.pipelineId } },
          body: { snapshot_a_id: props.baseSnapshotId, snapshot_b_id: effectiveCompareId.value },
          signal,
        }),
      DIFF_REQUEST_TIMEOUT_MS,
      controller.signal,
    )
    // A newer pair (or an unmount) superseded this request — drop the result.
    if (token !== diffRequestToken) return
    if (error) {
      diff.value = null
      errorMessage.value = formatApiError(error)
      status.value = 'error'
      return
    }
    if (!data) {
      diff.value = null
      errorMessage.value = t('components.PipelineSnapshotCompare.error_no_data')
      status.value = 'error'
      return
    }
    diff.value = data
    status.value = 'ready'
  } catch (e: unknown) {
    if (token !== diffRequestToken) return
    diff.value = null
    errorMessage.value =
      e instanceof RequestTimeoutError
        ? t('components.PipelineSnapshotCompare.error_timeout')
        : formatApiError(e)
    status.value = 'error'
  } finally {
    if (diffAbort === controller) diffAbort = null
  }
}

/**
 * Retry the current pair. Alongside the diff, it re-resolves either side's
 * snapshot label so a version whose targeted lookup failed transiently is
 * re-fetched instead of being stuck on the fallback label.
 */
function retryCompare(): void {
  void ensureSnapshotLoaded(props.baseSnapshotId)
  void ensureSnapshotLoaded(effectiveCompareId.value)
  void loadDiff()
}

onMounted(() => {
  void loadFullCatalog()
})

onBeforeUnmount(() => {
  diffRequestToken += 1
  catalogToken += 1
  diffAbort?.abort()
  diffAbort = null
})

// Re-fetch whenever either side of the pair changes (the host updates the
// route query, which feeds these props back in — deep links reload correctly).
watch([() => props.baseSnapshotId, effectiveCompareId], () => {
  void ensureSnapshotLoaded(props.baseSnapshotId)
  void ensureSnapshotLoaded(effectiveCompareId.value)
  void loadDiff()
}, { immediate: true })
</script>
