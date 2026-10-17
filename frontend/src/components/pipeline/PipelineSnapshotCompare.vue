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

    <p
      v-if="status === 'loading'"
      class="text-sm text-muted-foreground"
      role="status"
      data-testid="pipeline-compare-loading"
    >
      {{ $t('components.PipelineSnapshotCompare.loading') }}
    </p>

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
    <p
      v-else-if="isSelfCompare"
      class="text-sm text-muted-foreground"
      role="status"
      data-testid="pipeline-compare-self"
    >
      {{ $t('components.PipelineSnapshotCompare.self_compare') }}
    </p>

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
        <p
          class="rounded-lg border bg-muted/30 p-3 text-sm font-medium"
          role="status"
          data-testid="pipeline-compare-summary"
        >
          {{ summaryLine }}
        </p>

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
        <div :id="rawRegionId" role="region" :aria-label="$t('components.PipelineSnapshotCompare.raw_region_label')">
          <pre
            v-if="showRaw"
            class="mt-2 max-h-96 overflow-auto rounded-lg border bg-muted/40 p-3 text-[11px]"
            data-testid="pipeline-compare-raw"
          >{{ rawJson }}</pre>
        </div>
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
import { shortId } from '../../utils/format'
import { X as XIcon } from '@lucide/vue'

/** One selectable version in the pair pickers. */
interface CompareSnapshot {
  id: string
  snapshot_version: number
  tag: string | null
  created_at: string | null
}

interface GraphNode {
  id?: string
  label?: string | null
  node_type?: string
}

interface GraphEdge {
  source?: string
  target?: string
  source_node_id?: string
  target_node_id?: string
}

interface Graph {
  nodes?: GraphNode[]
  edges?: GraphEdge[]
}

interface PortChange {
  node_id?: string
  direction?: string
  port?: string | null
  change?: string
  old?: unknown
  new?: unknown
}

interface ModifiedNode {
  node_id?: string
  changes?: Record<string, unknown>
}

interface ModifiedEdge {
  edge?: { source?: string; target?: string }
  changes?: Record<string, unknown>
}

interface BreakingChange {
  severity?: string
  node_id?: string
  direction?: string
  port?: string | null
  edge?: { source?: string | null; target?: string | null }
  reason?: string
}

interface SemanticLayer {
  port_changes?: PortChange[]
  impacted_nodes?: string[]
  breaking_changes?: BreakingChange[]
}

interface DiffResponse {
  snapshot_a?: { version?: number; graph?: Graph }
  snapshot_b?: { version?: number; graph?: Graph }
  nodes_added?: GraphNode[]
  nodes_removed?: GraphNode[]
  nodes_modified?: ModifiedNode[]
  edges_added?: GraphEdge[]
  edges_removed?: GraphEdge[]
  edges_modified?: ModifiedEdge[]
  semantic?: SemanticLayer
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
const diff = ref<DiffResponse | null>(null)
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

async function loadFullCatalog(): Promise<void> {
  const token = ++catalogToken
  const collected: CompareSnapshot[] = []
  try {
    let page = 1
    let total: number | null = null
    let seen = 0
    while (page <= CATALOG_MAX_PAGES) {
      const { data, error } = await withTimeout((signal) =>
        api.GET('/api/v1/pipelines/{pipeline_id}/snapshots', {
          params: { path: { pipeline_id: props.pipelineId }, query: { page, page_size: CATALOG_PAGE_SIZE } },
          signal,
        }),
      )
      if (token !== catalogToken) return
      if (error || !data) break
      const items = data.items ?? []
      seen += items.length
      for (const raw of items) {
        const snap = readSnapshot(raw)
        if (snap && snap.snapshot_version > 0) collected.push(snap)
      }
      if (total === null && Number.isFinite(data.total)) total = data.total as number
      if (items.length === 0) break
      // The server's own total, or a short page when it is unknown, is the end.
      if (total !== null && seen >= total) break
      if (total === null && items.length < CATALOG_PAGE_SIZE) break
      page += 1
    }
    if (token !== catalogToken) return
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

const nodesAdded = computed(() => diff.value?.nodes_added ?? [])
const nodesRemoved = computed(() => diff.value?.nodes_removed ?? [])
const nodesModified = computed(() => diff.value?.nodes_modified ?? [])
const edgesAdded = computed(() => diff.value?.edges_added ?? [])
const edgesRemoved = computed(() => diff.value?.edges_removed ?? [])
const edgesModified = computed(() => diff.value?.edges_modified ?? [])

const semantic = computed<SemanticLayer>(() => diff.value?.semantic ?? {})
const breakingChanges = computed(() => semantic.value.breaking_changes ?? [])
const impactedNodes = computed(() => semantic.value.impacted_nodes ?? [])
const portChanges = computed(() => semantic.value.port_changes ?? [])

/** Node ids present in BOTH frozen graphs. */
const commonNodeIds = computed(() => {
  const before = new Set(
    (diff.value?.snapshot_a?.graph?.nodes ?? []).map((n) => n.id).filter((id): id is string => Boolean(id)),
  )
  return new Set(
    (diff.value?.snapshot_b?.graph?.nodes ?? [])
      .map((n) => n.id)
      .filter((id): id is string => typeof id === 'string' && before.has(id)),
  )
})

/**
 * Distinct nodes whose PORT SIGNATURE genuinely changed — i.e. a node present in
 * BOTH versions. The backend's semantic `port_changes` array also carries
 * ADDED-node port deltas (`diff_node_ports({}, node)`), whose `node_id` is the
 * literal string "None" and which are already reported under "nodes added"; a
 * node only in one version cannot have changed its signature, so those entries
 * are excluded here rather than inflated into a false headline.
 */
const portChangedNodeCount = computed(() => {
  const ids = new Set<string>()
  for (const change of portChanges.value) {
    const id = change.node_id
    if (!id || id === 'None') continue
    if (!commonNodeIds.value.has(id)) continue
    ids.add(id)
  }
  return ids.size
})

const hasStructuralChanges = computed(
  () =>
    nodesAdded.value.length > 0 ||
    nodesRemoved.value.length > 0 ||
    nodesModified.value.length > 0 ||
    edgesAdded.value.length > 0 ||
    edgesRemoved.value.length > 0 ||
    edgesModified.value.length > 0,
)

/** Nodes present in both versions with no changed field, in version-B order. */
const unchangedNodeIds = computed<string[]>(() => {
  const modified = new Set((diff.value?.nodes_modified ?? []).map((m) => m.node_id))
  return (diff.value?.snapshot_b?.graph?.nodes ?? [])
    .map((n) => n.id)
    .filter((id): id is string => typeof id === 'string' && commonNodeIds.value.has(id) && !modified.has(id))
})

const unchangedCount = computed(() => unchangedNodeIds.value.length)

function plural(key: string, count: number): string {
  return t(key, { count }, count)
}

const summaryLine = computed(() => {
  const parts = [
    plural('components.PipelineSnapshotCompare.summary_node_added', nodesAdded.value.length),
    plural('components.PipelineSnapshotCompare.summary_node_removed', nodesRemoved.value.length),
    plural('components.PipelineSnapshotCompare.summary_node_modified', nodesModified.value.length),
    plural('components.PipelineSnapshotCompare.summary_edge_added', edgesAdded.value.length),
    plural('components.PipelineSnapshotCompare.summary_edge_removed', edgesRemoved.value.length),
    plural('components.PipelineSnapshotCompare.summary_edge_modified', edgesModified.value.length),
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
  const add = (nodes?: GraphNode[]) => {
    for (const node of nodes ?? []) {
      if (node?.id) map[node.id] = node.label || shortId(node.id)
    }
  }
  add(diff.value?.snapshot_a?.graph?.nodes)
  add(diff.value?.snapshot_b?.graph?.nodes)
  return map
})

function nodeLabel(id?: string | null): string {
  if (!id) return t('components.PipelineSnapshotCompare.no_value')
  return nodeLabels.value[id] ?? shortId(id)
}

function nodeId(node: GraphNode, index: number): string {
  return node.id ?? `node-${index}`
}

function nodeText(node: GraphNode): string {
  if (!node.id) return t('components.PipelineSnapshotCompare.no_value')
  return nodeLabels.value[node.id] ?? node.label ?? shortId(node.id)
}

function nodeTypeOf(node: GraphNode): string {
  return node.node_type ?? ''
}

function edgeEndpoint(edge: { source?: string; target?: string; source_node_id?: string; target_node_id?: string }): string {
  const source = edge.source ?? edge.source_node_id
  const target = edge.target ?? edge.target_node_id
  return `${nodeLabel(source)} → ${nodeLabel(target)}`
}

function edgeText(edge: GraphEdge): string {
  return edgeEndpoint(edge)
}

function modifiedEdgeText(modified: ModifiedEdge): string {
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
function portsOf(changes?: Record<string, unknown>): PortChange[] {
  const raw = changes?.ports
  return Array.isArray(raw) ? (raw as PortChange[]) : []
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

function portChangeLabel(change: PortChange): string {
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
  return severity === 'block' ? 'font-medium text-destructive' : 'font-medium text-warning-text'
}

function severityLabel(severity?: string): string {
  if (severity === 'block') return t('components.PipelineSnapshotCompare.severity_block')
  if (severity === 'warning') return t('components.PipelineSnapshotCompare.severity_warning')
  return severity ?? t('components.PipelineSnapshotCompare.severity_warning')
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
    diff.value = data as unknown as DiffResponse
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
