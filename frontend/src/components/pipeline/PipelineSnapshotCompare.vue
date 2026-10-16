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

    <div
      v-else-if="status === 'error'"
      class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive"
      role="alert"
      data-testid="pipeline-compare-error"
    >
      <p class="font-medium">{{ $t('components.PipelineSnapshotCompare.error_title') }}</p>
      <p class="mt-0.5">{{ errorMessage }}</p>
      <button
        type="button"
        class="mt-2 underline"
        data-testid="pipeline-compare-retry"
        @click="loadDiff"
      >
        {{ $t('components.PipelineSnapshotCompare.retry') }}
      </button>
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

          <details
            v-if="unchangedCount > 0"
            class="text-[11px] text-muted-foreground"
            data-testid="pipeline-compare-unchanged"
          >
            <summary class="cursor-pointer">{{ $t('components.PipelineSnapshotCompare.unchanged_summary', { count: unchangedCount }) }}</summary>
          </details>
        </template>
      </section>

      <!-- 3. RAW JSON, last and hidden by default. -->
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
        <pre
          v-if="showRaw"
          :id="rawRegionId"
          :aria-label="$t('components.PipelineSnapshotCompare.raw_region_label')"
          class="mt-2 max-h-96 overflow-auto rounded-lg border bg-muted/40 p-3 text-[11px]"
          data-testid="pipeline-compare-raw"
        >{{ rawJson }}</pre>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Select from '../shared/AppSelect.vue'
import { api } from '../../lib/api/client'
import { formatApiError } from '../../lib/api/formatError'
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

const rawRegionId = 'pipeline-compare-raw-region'
const rawJson = computed(() => JSON.stringify(diff.value ?? {}, null, 2))

// `compare` defaults to the latest version when the query carries no id.
const effectiveCompareId = computed(() => props.compareSnapshotId ?? props.latestSnapshotId ?? '')
const canCompare = computed(() => props.baseSnapshotId !== '' && effectiveCompareId.value !== '')

const snapshotOptions = computed(() =>
  props.snapshots.map((s) => ({
    value: s.id,
    label: `v${s.snapshot_version}${s.tag ? ` — ${s.tag}` : ''}`,
  })),
)

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

const portChangedNodeCount = computed(() => {
  const ids = new Set<string>()
  for (const change of portChanges.value) {
    if (change.node_id) ids.add(change.node_id)
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

/**
 * The count of nodes present in BOTH versions with no changed field. Grouped
 * work shows only what changed; the unchanged remainder is collapsed.
 */
const unchangedCount = computed(() => {
  const before = diff.value?.snapshot_a?.graph?.nodes ?? []
  const after = diff.value?.snapshot_b?.graph?.nodes ?? []
  const beforeIds = new Set(before.map((n) => n.id).filter((id): id is string => Boolean(id)))
  const modified = new Set((diff.value?.nodes_modified ?? []).map((m) => m.node_id))
  return after.filter((n) => n.id && beforeIds.has(n.id) && !modified.has(n.id)).length
})

const summaryLine = computed(() => {
  const parts = [
    t('components.PipelineSnapshotCompare.summary_nodes', {
      added: nodesAdded.value.length,
      removed: nodesRemoved.value.length,
      modified: nodesModified.value.length,
    }),
    t('components.PipelineSnapshotCompare.summary_edges', {
      added: edgesAdded.value.length,
      removed: edgesRemoved.value.length,
      modified: edgesModified.value.length,
    }),
  ]
  if (portChangedNodeCount.value > 0) {
    parts.push(t('components.PipelineSnapshotCompare.summary_ports', { count: portChangedNodeCount.value }))
  }
  parts.push(
    impactedNodes.value.length > 0
      ? t('components.PipelineSnapshotCompare.summary_impacted', { count: impactedNodes.value.length })
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
  const changes = fieldEntries(modified.changes).map(
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

function portsOf(changes?: Record<string, unknown>): PortChange[] {
  const raw = changes?.ports
  return Array.isArray(raw) ? (raw as PortChange[]) : []
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

async function loadDiff(): Promise<void> {
  if (!canCompare.value) {
    status.value = 'idle'
    diff.value = null
    return
  }
  status.value = 'loading'
  errorMessage.value = null
  try {
    const { data, error } = await api.POST('/api/v1/pipelines/{pipeline_id}/snapshots/diff', {
      params: { path: { pipeline_id: props.pipelineId } },
      body: { snapshot_a_id: props.baseSnapshotId, snapshot_b_id: effectiveCompareId.value },
    })
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
    diff.value = null
    errorMessage.value = formatApiError(e)
    status.value = 'error'
  }
}

// Re-fetch whenever either side of the pair changes (the host updates the
// route query, which feeds these props back in — deep links reload correctly).
watch([() => props.baseSnapshotId, effectiveCompareId], () => {
  void loadDiff()
}, { immediate: true })
</script>
