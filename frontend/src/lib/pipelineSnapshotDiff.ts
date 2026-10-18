/**
 * FAR-1893: ONE interpretation of the snapshot-diff response, shared by the
 * version timeline (`PipelineSnapshotTimeline.vue`), the semantic-led compare
 * view (`PipelineSnapshotCompare.vue`) and the editor's restore dialog
 * (`PipelineEditorView.vue`).
 *
 * All three surfaces read the SAME
 * `POST /api/v1/pipelines/{pipeline_id}/snapshots/diff` payload. Keeping the
 * field names, the change-class enumeration (the added/removed/modified
 * buckets), the semantic layer (breaking changes, impacted nodes, port
 * changes), the severity→label rule and the id-shortening rule in ONE place is
 * what stops a backend field rename — or a new change class — having to be
 * handled three times, and stops the surfaces from presenting the same facts
 * differently.
 *
 * The response shape is BOUND to the generated OpenAPI type
 * (`components['schemas']['SnapshotDiffResponse']` in `api/schema.ts`), so a
 * backend rename is a `vue-tsc` failure here rather than a silently empty
 * render. The module normalises that loose generated shape (the generator
 * emits the arrays and the frozen sides as `{ [key: string]: unknown }`) into
 * the small view types below, shape-guarding every read so a malformed or
 * partial payload degrades instead of throwing.
 *
 * This module is INTERPRETATION ONLY. It never renders anything: each surface
 * keeps its own layout (the compare view's semantic-led ordering, the
 * timeline's compact summary and the restore dialog's list are real, separate
 * requirements). Severity labels resolve through a NEUTRAL namespace, so no
 * shared helper owns one view's copy.
 *
 * The semantic `port_changes` entries carry a `kind` discriminator
 * (`node_signature` vs `edge_port`) since FAR-1894.
 *
 * DELIBERATE USER-VISIBLE BEHAVIOUR CHANGE — not a pure refactor. Before this
 * module, the compare view counted ANY `semantic.port_changes` entry whose
 * `node_id` named a node present in both snapshots, which INCLUDED `edge_port`
 * entries; the "port signature changed on K nodes" headline could therefore
 * over-count. `readNodeSignatureNodeIds` below counts only `node_signature`
 * entries, so K can legitimately be SMALLER than the old view reported on the
 * same pair of versions. The new reading is the correct one (an edge's port
 * binding is not a node's port signature), and it is called out here because
 * it changes a number the user sees rather than merely moving code around.
 */

import type { components } from './api/schema'
import { shortId } from '../utils/format'

/** The generated `POST /snapshots/diff` response body. */
export type SnapshotDiffResponse = components['schemas']['SnapshotDiffResponse']

/**
 * Every reader accepts a partial payload: the semantic layer is optional in
 * the generated type, and tests/legacy callers may omit whole sections. A
 * MISSING section degrades to "nothing reported"; a malformed one is guarded
 * by the helpers below.
 */
export type SnapshotDiffPayload = Partial<SnapshotDiffResponse> | null | undefined

/**
 * The six structural change classes the endpoint reports. Enumerated once so
 * "is there anything structural?" and every surface's counts cannot drift from
 * the response's actual keys.
 *
 * `satisfies` (below) makes each entry a compile-time key of the GENERATED
 * response: renaming a class in the backend turns this list into a type error.
 */
type GeneratedChangeClass = {
  [K in keyof SnapshotDiffResponse]-?: SnapshotDiffResponse[K] extends readonly unknown[] ? K : never
}[keyof SnapshotDiffResponse]

// `satisfies` proves every enumerated class exists in the GENERATED response:
// a backend rename turns this list into a type error.
export const SNAPSHOT_CHANGE_CLASSES = [
  'nodes_added',
  'nodes_removed',
  'nodes_modified',
  'edges_added',
  'edges_removed',
  'edges_modified',
] as const satisfies readonly GeneratedChangeClass[]

type SameUnion<A, B> = [A] extends [B] ? ([B] extends [A] ? true : false) : false
type AssertTrue<T extends true> = T

/**
 * Compile-time proof that the enumeration is EXHAUSTIVE: adding a seventh
 * array-valued bucket to the generated response makes this alias fail its
 * `true` constraint, so the new class cannot ship unhandled. The runtime
 * reader still counts an unrecognised class (see `readChangeClassCounts`), so
 * a backend that deploys ahead of this bundle is not silently ignored.
 */
export type SnapshotChangeClassExhaustiveness = AssertTrue<
  SameUnion<GeneratedChangeClass, (typeof SNAPSHOT_CHANGE_CLASSES)[number]>
>

/** A node as carried by `nodes_added` / `nodes_removed` (and the frozen graphs). */
export interface SnapshotDiffNode {
  id?: string
  label?: string | null
  node_type?: string
}

/** An edge as carried by `edges_added` / `edges_removed` (and the frozen graphs). */
export interface SnapshotDiffEdge {
  id?: string
  source?: string
  target?: string
  source_node_id?: string
  target_node_id?: string
  edge_type?: string
}

/** A per-node entry in `nodes_modified`; `changes` is keyed by field name. */
export interface SnapshotModifiedNode {
  node_id?: string
  changes?: Record<string, unknown>
}

/** A per-edge entry in `edges_modified`. */
export interface SnapshotModifiedEdge {
  edge?: { source?: string; target?: string }
  changes?: Record<string, unknown>
}

/**
 * One entry in `semantic.port_changes`.
 *
 * `kind` distinguishes a genuine port-signature delta on a node present in
 * BOTH snapshots (`node_signature`) from an edge's port binding (`edge_port`).
 * It is optional here only so a legacy/partial payload without the
 * discriminator is still readable; the backend always writes it (FAR-1894).
 */
export interface SemanticPortChange {
  /**
   * Discriminator: `node_signature` (a node present in both snapshots whose
   * port signature changed) or `edge_port` (an edge's port binding). Typed as
   * a plain string so an unknown/future value is readable and excluded rather
   * than silently mistyped.
   */
  kind?: string
  node_id?: string
  direction?: string
  port?: string | null
  change?: string
  old?: unknown
  new?: unknown
  edge?: { source?: string | null; target?: string | null }
}

/**
 * One entry in a node's `changes.ports` delta. This is a DIFFERENT shape from
 * `SemanticPortChange`: it has no `kind`/`edge`/`node_id` discriminator — the
 * direction, port and operation are the whole record.
 */
export interface NodePortDelta {
  direction?: string
  port?: string | null
  change?: string
}

/** One entry in `semantic.breaking_changes`. */
export interface SnapshotBreakingChange {
  severity?: string
  node_id?: string
  direction?: string
  port?: string | null
  edge?: { source?: string | null; target?: string | null }
  reason?: string
}

/** The backend's semantic-diff + impact layer, normalised. */
export interface SnapshotSemanticLayer {
  portChanges: SemanticPortChange[]
  impactedNodes: string[]
  breakingChanges: SnapshotBreakingChange[]
}

/** The change entries, grouped under semantic camelCase names. */
export interface SnapshotChangeClasses {
  nodesAdded: SnapshotDiffNode[]
  nodesRemoved: SnapshotDiffNode[]
  nodesModified: SnapshotModifiedNode[]
  edgesAdded: SnapshotDiffEdge[]
  edgesRemoved: SnapshotDiffEdge[]
  edgesModified: SnapshotModifiedEdge[]
}

/**
 * Per-class counts plus presence.
 *
 * `hasChanges` is true when ANY change bucket — the six known classes OR an
 * unrecognised/new one the backend added — carries entries. A new class must
 * surface as a change; it must never be silently ignored.
 */
export interface SnapshotChangeClassCounts {
  nodesAdded: number
  nodesRemoved: number
  nodesModified: number
  edgesAdded: number
  edgesRemoved: number
  edgesModified: number
  /** Buckets the frontend does not know about yet, keyed by response field. */
  unrecognised: Record<string, number>
  /** True when any known OR unrecognised class is non-empty. */
  hasChanges: boolean
}

function asRecordArray(value: unknown): Record<string, unknown>[] {
  if (!Array.isArray(value)) return []
  return value.filter(
    (entry): entry is Record<string, unknown> =>
      typeof entry === 'object' && entry !== null && !Array.isArray(entry),
  )
}

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  const out: string[] = []
  for (const entry of value) {
    // The old compare view coerced elements with `String(id)`. Preserve that
    // tolerance for a number id, but drop shapes that cannot be a node id
    // instead of rendering "[object Object]".
    if (typeof entry === 'string') out.push(entry)
    else if (typeof entry === 'number' && Number.isFinite(entry)) out.push(String(entry))
  }
  return out
}

/** Normalised change entries for the six known structural classes. */
export function readChangeClasses(diff: SnapshotDiffPayload): SnapshotChangeClasses {
  return {
    nodesAdded: asRecordArray(diff?.nodes_added) as SnapshotDiffNode[],
    nodesRemoved: asRecordArray(diff?.nodes_removed) as SnapshotDiffNode[],
    nodesModified: asRecordArray(diff?.nodes_modified) as SnapshotModifiedNode[],
    edgesAdded: asRecordArray(diff?.edges_added) as SnapshotDiffEdge[],
    edgesRemoved: asRecordArray(diff?.edges_removed) as SnapshotDiffEdge[],
    edgesModified: asRecordArray(diff?.edges_modified) as SnapshotModifiedEdge[],
  }
}

/**
 * The ONE source of change-class counts/presence. No surface reads the raw
 * response's class keys itself.
 */
export function readChangeClassCounts(diff: SnapshotDiffPayload): SnapshotChangeClassCounts {
  const classes = readChangeClasses(diff)
  const counts = {
    nodesAdded: classes.nodesAdded.length,
    nodesRemoved: classes.nodesRemoved.length,
    nodesModified: classes.nodesModified.length,
    edgesAdded: classes.edgesAdded.length,
    edgesRemoved: classes.edgesRemoved.length,
    edgesModified: classes.edgesModified.length,
  }
  const unrecognised: Record<string, number> = {}
  let hasChanges = Object.values(counts).some((count) => count > 0)
  // Any OTHER top-level array is a change bucket this bundle does not know
  // about (a class added by a newer backend). Count it so the diff never
  // claims "no changes" while a bucket is non-empty.
  const known = new Set<string>(SNAPSHOT_CHANGE_CLASSES)
  for (const [key, value] of Object.entries(diff ?? {})) {
    if (known.has(key) || !Array.isArray(value) || value.length === 0) continue
    unrecognised[key] = value.length
    hasChanges = true
  }
  return { ...counts, unrecognised, hasChanges }
}

/**
 * True when the diff reports ANY change — including a class this bundle does
 * not recognise. Named "structural" because both current call sites use it for
 * the structural section; it is deliberately a superset of the six known keys.
 */
export function hasStructuralChanges(diff: SnapshotDiffPayload): boolean {
  return readChangeClassCounts(diff).hasChanges
}

/** The normalised semantic layer, defaulting every section to empty. */
export function readSemanticLayer(diff: SnapshotDiffPayload): SnapshotSemanticLayer {
  const semantic = diff?.semantic
  const layer = typeof semantic === 'object' && semantic !== null ? semantic : {}
  return {
    portChanges: asRecordArray(layer.port_changes) as SemanticPortChange[],
    impactedNodes: asStringArray(layer.impacted_nodes),
    breakingChanges: asRecordArray(layer.breaking_changes) as SnapshotBreakingChange[],
  }
}

/** `semantic.breaking_changes`, or an empty list when the layer is absent. */
export function readBreakingChanges(diff: SnapshotDiffPayload): SnapshotBreakingChange[] {
  return readSemanticLayer(diff).breakingChanges
}

/** `semantic.impacted_nodes`, or an empty list when the layer is absent. */
export function readImpactedNodes(diff: SnapshotDiffPayload): string[] {
  return readSemanticLayer(diff).impactedNodes
}

function graphNodesOf(side: unknown): SnapshotDiffNode[] {
  if (typeof side !== 'object' || side === null) return []
  const graph = (side as { graph?: unknown }).graph
  if (typeof graph !== 'object' || graph === null) return []
  return asRecordArray((graph as { nodes?: unknown }).nodes) as SnapshotDiffNode[]
}

/**
 * Nodes carried by one side's FROZEN graph (`a` = the base/older side,
 * `b` = the newer side), shape-guarded. A surface that needs a label for an
 * id reads them through here rather than reaching into the raw response.
 */
export function readFrozenGraphNodes(
  diff: SnapshotDiffPayload,
  side: 'a' | 'b',
): SnapshotDiffNode[] {
  return graphNodesOf(side === 'a' ? diff?.snapshot_a : diff?.snapshot_b)
}

/** Distinct node ids on BOTH sides' frozen graphs. */
function commonNodeIds(diff: SnapshotDiffPayload): Set<string> {
  const before = new Set<string>()
  for (const node of graphNodesOf(diff?.snapshot_a)) {
    if (typeof node.id === 'string' && node.id !== '') before.add(node.id)
  }
  const common = new Set<string>()
  for (const node of graphNodesOf(diff?.snapshot_b)) {
    if (typeof node.id === 'string' && before.has(node.id)) common.add(node.id)
  }
  return common
}

/**
 * Distinct nodes whose PORT SIGNATURE genuinely changed — i.e. a
 * `node_signature` entry naming a node present in BOTH frozen graphs.
 *
 * Two exclusions matter:
 *  - `edge_port` entries are an edge's port binding, not a node signature
 *    change; counting them would report a node whose ports did not change.
 *    THIS IS A DELIBERATE USER-VISIBLE CHANGE from the pre-FAR-1893 compare
 *    view, which counted them as long as the named node existed in both
 *    snapshots: on the same version pair the "port signature changed on K
 *    nodes" headline can now be smaller, and that is the correct result.
 *  - the node must exist in both graphs. A signature delta is only meaningful
 *    with a "before" and an "after" side, so a missing/partial graph proves
 *    nothing, and added-node artefacts (which FAR-1894 no longer emits) could
 *    never satisfy it. A legacy entry without `kind` is treated the same way.
 */
export function readNodeSignatureNodeIds(diff: SnapshotDiffPayload): string[] {
  const common = commonNodeIds(diff)
  const ids = new Set<string>()
  for (const change of readSemanticLayer(diff).portChanges) {
    if (change.kind && change.kind !== 'node_signature') continue
    const id = change.node_id
    if (typeof id !== 'string' || id === '' || !common.has(id)) continue
    ids.add(id)
  }
  return [...ids]
}

/** Nodes present in both versions with no changed field, in version-B order. */
export function readUnchangedNodeIds(diff: SnapshotDiffPayload): string[] {
  const common = commonNodeIds(diff)
  const modified = new Set<string>()
  for (const entry of asRecordArray(diff?.nodes_modified)) {
    if (typeof entry.node_id === 'string') modified.add(entry.node_id)
  }
  return graphNodesOf(diff?.snapshot_b)
    .map((node) => node.id)
    .filter((id): id is string => typeof id === 'string' && common.has(id) && !modified.has(id))
}

/**
 * severity → i18n label key, shared by every diff surface.
 *
 * NEUTRAL namespace by design: this module must not own the copy namespace of
 * one consumer (the compare view), or every surface that reuses the rule would
 * render out of that view's keys. The keys live under
 * `components.SnapshotDiffSeverity`, which has no view of its own.
 */
const SEVERITY_LABEL_KEYS: Readonly<Record<string, string>> = {
  block: 'components.SnapshotDiffSeverity.block',
  warning: 'components.SnapshotDiffSeverity.warning',
}

/**
 * Resolve a breaking-change severity to its display label. A known severity
 * uses its shared i18n key; an unknown one falls back to its RAW value so a
 * future backend severity is visible rather than silently relabelled.
 *
 * An absent OR empty severity falls back to the warning label. Decision: both
 * mean "the payload did not report a severity" (a legacy/partial entry), and a
 * blank label followed by "— reason" reads as a formatting bug; the warning
 * default is the honest presentation of a missing severity.
 */
export function severityLabel(
  severity: string | null | undefined,
  t: (key: string) => string,
): string {
  if (!severity) return t(SEVERITY_LABEL_KEYS.warning)
  const key = SEVERITY_LABEL_KEYS[severity]
  return key ? t(key) : severity
}

/** `block` is the only severity that is a hard failure (destructive tone). */
export function isBlockingSeverity(severity: string | null | undefined): boolean {
  return severity === 'block'
}

/**
 * The app-wide id-shortening rule, re-exported so every diff surface shortens
 * node ids the same way as the rest of the product (`#` + first 8 chars) and
 * an em-dash for an absent id. The implementation stays in `utils/format.ts` —
 * one copy, not a third.
 */
export { shortId }
