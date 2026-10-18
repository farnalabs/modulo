/**
 * FAR-1893: ONE interpretation of the snapshot-diff response, shared by the
 * version timeline (`PipelineSnapshotTimeline.vue`) and the semantic-led
 * compare view (`PipelineSnapshotCompare.vue`).
 *
 * Both components read the SAME `POST /api/v1/pipelines/{pipeline_id}/snapshots/diff`
 * payload. Keeping the field names, the change-class enumeration (the six
 * added/removed/modified buckets), the semantic layer (breaking changes,
 * impacted nodes, port changes), the severity→label mapping and the
 * id-shortening rule in ONE place is what stops a backend field rename — or a
 * new change class — having to be handled twice, and stops the two surfaces
 * from presenting the same facts differently.
 *
 * This module is INTERPRETATION ONLY. It never renders anything: each surface
 * keeps its own layout (the compare view's semantic-led ordering and the
 * timeline's compact summary are real, separate requirements).
 *
 * The response shape is the one declared by the backend
 * `SnapshotDiffResponse` (`backend/src/modulo/api/routes/pipelines.py`) and
 * produced by `diff_snapshots`
 * (`backend/src/modulo/db/crud/pipeline_snapshot_versioning.py`). The semantic
 * `port_changes` entries carry a `kind` discriminator (`node_signature` vs
 * `edge_port`) since FAR-1894.
 */

import { shortId } from '../utils/format'

/**
 * The six structural change classes the endpoint reports. Enumerated once so
 * "is there anything structural?" and any future presentation cannot drift
 * from the response's actual keys.
 */
export const SNAPSHOT_CHANGE_CLASSES = [
  'nodes_added',
  'nodes_removed',
  'nodes_modified',
  'edges_added',
  'edges_removed',
  'edges_modified',
] as const

export type SnapshotChangeClass = (typeof SNAPSHOT_CHANGE_CLASSES)[number]

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
export interface SnapshotPortChange {
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

/** One entry in `semantic.breaking_changes`. */
export interface SnapshotBreakingChange {
  severity?: string
  node_id?: string
  direction?: string
  port?: string | null
  edge?: { source?: string | null; target?: string | null }
  reason?: string
}

/** The backend's semantic-diff + impact layer. */
export interface SnapshotSemanticLayer {
  port_changes?: SnapshotPortChange[]
  impacted_nodes?: string[]
  breaking_changes?: SnapshotBreakingChange[]
}

export interface SnapshotDiffGraph {
  nodes?: SnapshotDiffNode[]
  edges?: SnapshotDiffEdge[]
}

/** The full `POST /snapshots/diff` response body. */
export interface SnapshotDiffResponse {
  snapshot_a?: { version?: number; graph?: SnapshotDiffGraph }
  snapshot_b?: { version?: number; graph?: SnapshotDiffGraph }
  nodes_added?: SnapshotDiffNode[]
  nodes_removed?: SnapshotDiffNode[]
  nodes_modified?: SnapshotModifiedNode[]
  edges_added?: SnapshotDiffEdge[]
  edges_removed?: SnapshotDiffEdge[]
  edges_modified?: SnapshotModifiedEdge[]
  semantic?: SnapshotSemanticLayer
}

/** Distinct node ids on BOTH sides' frozen graphs. */
function commonNodeIds(diff: SnapshotDiffResponse | null | undefined): Set<string> {
  const before = new Set<string>()
  for (const node of diff?.snapshot_a?.graph?.nodes ?? []) {
    if (typeof node.id === 'string' && node.id !== '') before.add(node.id)
  }
  const common = new Set<string>()
  for (const node of diff?.snapshot_b?.graph?.nodes ?? []) {
    if (typeof node.id === 'string' && before.has(node.id)) common.add(node.id)
  }
  return common
}

/** True when ANY of the six structural change classes is non-empty. */
export function hasStructuralChanges(diff: SnapshotDiffResponse | null | undefined): boolean {
  return SNAPSHOT_CHANGE_CLASSES.some((changeClass) => (diff?.[changeClass]?.length ?? 0) > 0)
}

/** `semantic.breaking_changes`, or an empty list when the layer is absent. */
export function readBreakingChanges(diff: SnapshotDiffResponse | null | undefined): SnapshotBreakingChange[] {
  return diff?.semantic?.breaking_changes ?? []
}

/** `semantic.impacted_nodes`, or an empty list when the layer is absent. */
export function readImpactedNodes(diff: SnapshotDiffResponse | null | undefined): string[] {
  return diff?.semantic?.impacted_nodes ?? []
}

/**
 * Distinct nodes whose PORT SIGNATURE genuinely changed — i.e. a
 * `node_signature` entry naming a node present in BOTH frozen graphs.
 *
 * Two exclusions matter:
 *  - `edge_port` entries are an edge's port binding, not a node signature
 *    change; counting them would report a node whose ports did not change.
 *  - the node must exist in both graphs. A signature delta is only meaningful
 *    with a "before" and an "after" side, so a missing/partial graph proves
 *    nothing, and added-node artefacts (which FAR-1894 no longer emits) could
 *    never satisfy it. A legacy entry without `kind` is treated the same way.
 */
export function readNodeSignatureNodeIds(diff: SnapshotDiffResponse | null | undefined): string[] {
  const common = commonNodeIds(diff)
  const ids = new Set<string>()
  for (const change of diff?.semantic?.port_changes ?? []) {
    if (change.kind && change.kind !== 'node_signature') continue
    const id = change.node_id
    if (!id || !common.has(id)) continue
    ids.add(id)
  }
  return [...ids]
}

/** Nodes present in both versions with no changed field, in version-B order. */
export function readUnchangedNodeIds(diff: SnapshotDiffResponse | null | undefined): string[] {
  const common = commonNodeIds(diff)
  const modified = new Set((diff?.nodes_modified ?? []).map((entry) => entry.node_id))
  return (diff?.snapshot_b?.graph?.nodes ?? [])
    .map((node) => node.id)
    .filter((id): id is string => typeof id === 'string' && common.has(id) && !modified.has(id))
}

/**
 * severity → i18n label key, shared by both diff surfaces. Mirrors the
 * `pipelineVersion.ts` convention: a shared helper may own the mapping while
 * the keys stay in the namespace of the view that first defined them.
 */
export const SEVERITY_LABEL_KEYS: Readonly<Record<string, string>> = {
  block: 'components.PipelineSnapshotCompare.severity_block',
  warning: 'components.PipelineSnapshotCompare.severity_warning',
}

/**
 * Resolve a breaking-change severity to its display label. A known severity
 * uses its i18n key; an unknown one falls back to its RAW value so a future
 * backend severity is visible rather than silently relabelled; an absent one
 * falls back to the warning label (both surfaces treated it as a warning).
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
 * The app-wide id-shortening rule, re-exported so both diff surfaces shorten
 * node ids the same way as the rest of the product (`#` + first 8 chars) and
 * an em-dash for an absent id. The implementation stays in `utils/format.ts` —
 * one copy, not a third.
 */
export { shortId }
