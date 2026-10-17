/**
 * FAR-1817: ONE definition of what a pipeline "version" is, shared by the
 * version timeline (which rows it renders) and the toolbar version chip
 * (which snapshot it labels as the pipeline's current version). Keeping the
 * predicate and the created_kind→label mapping in one place is what stops the
 * two surfaces from disagreeing about the same user-visible version.
 *
 * `created_kind` is the server's closed vocabulary:
 *   - `initial`  — the version created when the pipeline was first saved
 *   - `edit`     — a committed save
 *   - `rollback` — the version a restore created
 *   - `run`      — a run's frozen copy of the graph (NOT a version)
 *
 * Any OTHER non-empty value is an unknown/future kind: it is treated as
 * SUBSTANTIVE and surfaced (labelled with its raw value) rather than being
 * silently folded into the run bucket, which would hide a real version.
 */

/**
 * The page size BOTH version surfaces fetch when reading
 * `GET /pipelines/{id}/snapshots` (the endpoint caps at 100, defaults to 20).
 *
 * It is ONE shared constant on purpose. The toolbar chip resolves the current
 * version from the editor's snapshot list and the timeline renders that same
 * history, so if the two fetched different windows they could disagree about a
 * version that genuinely exists: with the editor on the 20-row default, once
 * the newest 20 snapshots are all `run` kind (a scheduled pipeline creates one
 * per run) `currentSubstantiveVersion` returns null and the chip silently
 * vanishes while the 100-row timeline still shows the substantive version.
 * A single number cannot drift; two independent numbers can. (FAR-1816/1817)
 */
export const SNAPSHOT_PAGE_SIZE = 100

/** created_kind → i18n label key, shared by the timeline and the chip. */
export const CREATED_KIND_LABEL_KEYS: Readonly<Record<string, string>> = {
  initial: 'components.PipelineVersionChip.kind_initial',
  edit: 'components.PipelineVersionChip.kind_edit',
  rollback: 'components.PipelineVersionChip.kind_rollback',
  run: 'components.PipelineVersionChip.kind_run',
}

/**
 * Substantive = a user-recognisable pipeline version. A `run` snapshot is NOT
 * a version, and neither is an absent/empty kind (legacy rows serialise
 * created_kind as the server default `run`). Any other value — an unknown or
 * future kind — IS substantive so it stays visible.
 */
export function isSubstantiveKind(createdKind: string | null | undefined): boolean {
  if (!createdKind) return false
  return createdKind !== 'run'
}

/** A minimal structural shape both the timeline and the chip can satisfy. */
export interface CreatedKindLike {
  created_kind: string
}

/**
 * The newest substantive version in a version-DESCENDING snapshot list, or
 * null when the pipeline has no substantive version yet. This is the ONE
 * definition of "the pipeline's current version" shared by the toolbar chip
 * and the timeline.
 */
export function currentSubstantiveVersion<T extends CreatedKindLike>(snapshots: readonly T[]): T | null {
  return snapshots.find((s) => isSubstantiveKind(s.created_kind)) ?? null
}

/**
 * Resolve a created_kind to its display label. Known kinds use their i18n
 * key; an unknown kind falls back to its RAW value so it is never mislabelled
 * as a run; an empty kind falls back to the caller-supplied blank label.
 */
export function createdKindLabel(
  createdKind: string | null | undefined,
  t: (key: string) => string,
  blankLabel: string,
): string {
  const key = createdKind ? CREATED_KIND_LABEL_KEYS[createdKind] : undefined
  if (key) return t(key)
  return createdKind || blankLabel
}
