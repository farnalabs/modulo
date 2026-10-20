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
 *
 * Draft-ness (`draft: true` — an in-progress editor auto-save) is a SEPARATE
 * axis from `created_kind`: a draft save carries `created_kind='edit'` and a
 * real `snapshot_version`, so it is not a version until it is committed. The
 * version resolvers here therefore require BOTH "substantive kind" and "not a
 * draft"; the timeline drops draft rows before grouping (see
 * PipelineSnapshotTimeline.vue) so a draft is neither a row nor a run
 * annotation.
 */

/**
 * The page size the version TIMELINE (and the `?version=` alias lookup) fetch
 * when reading `GET /pipelines/{id}/snapshots` (the endpoint caps at 100,
 * defaults to 20).
 *
 * The toolbar chip does NOT read this window. FAR-1895: it resolves the newest
 * substantive version from a SERVER-FILTERED query
 * (`page_size` ≥ 1 + `exclude_created_kind=<NON_VERSION_CREATED_KINDS>` +
 * `exclude_drafts=true`) so a run-heavy history can never starve it, however
 * long the run streak grows.
 *
 * The two surfaces therefore use DIFFERENT read paths, and this constant alone
 * does not keep them in step. They agree about the newest substantive version
 * by construction, not by sharing this number:
 *   * when the timeline's loaded page contains a substantive row, that row is
 *     the newest substantive version (the page is version-DESC, so no newer
 *     substantive row can sit above it);
 *   * when the loaded page contains NO substantive row (an all-run head longer
 *     than the window), the timeline falls back to the same resolved current
 *     version the chip shows, so it never fabricates an anchor from a run row.
 * Keep that invariant in mind when changing either read path — the shared
 * constant is not a parity guarantee.
 */
export const SNAPSHOT_PAGE_SIZE = 100

/**
 * The `created_kind` values that are NOT pipeline versions.
 *
 * This is the ONE definition of "not a version" by KIND. `isSubstantiveKind`
 * derives the client-side predicate from it, and the chip passes it as the
 * server filter (`exclude_created_kind`) — so the two can never drift. It is an
 * EXCLUSION list on purpose: the timeline surfaces any other non-empty kind
 * (including an unknown/future one), so an inclusion list would be a second
 * copy of the vocabulary that could silently hide a real version. Drafts are
 * excluded separately (they are `kind='edit'`), never by adding an entry here.
 */
export const NON_VERSION_CREATED_KINDS = ['run'] as const

/** created_kind → i18n label key, shared by the timeline and the chip. */
export const CREATED_KIND_LABEL_KEYS: Readonly<Record<string, string>> = {
  initial: 'components.PipelineVersionChip.kind_initial',
  edit: 'components.PipelineVersionChip.kind_edit',
  rollback: 'components.PipelineVersionChip.kind_rollback',
  run: 'components.PipelineVersionChip.kind_run',
}

/**
 * Substantive = a user-recognisable pipeline version KIND. A `run` snapshot is
 * NOT a version, and neither is an absent/empty kind (legacy rows serialise
 * created_kind as the server default `run`). Any other value — an unknown or
 * future kind — IS substantive so it stays visible.
 *
 * This predicate is about `created_kind` ONLY and deliberately ignores
 * `draft`: callers that split a history into versions and runs (the timeline)
 * must first drop draft rows, or a draft would be classified as a run and
 * annotated as one. Use {@link isVersionSnapshot} when you need "is this a
 * version a user should see as the current one?".
 */
export function isSubstantiveKind(createdKind: string | null | undefined): boolean {
  if (!createdKind) return false
  return !(NON_VERSION_CREATED_KINDS as readonly string[]).includes(createdKind)
}

/** A minimal structural shape both the timeline and the chip can satisfy. */
export interface CreatedKindLike {
  created_kind: string
  /** `true` = an in-progress editor auto-save; not yet a version. */
  draft?: boolean
}

/**
 * True when a snapshot is a version a user should recognise as the pipeline's
 * version: a substantive kind AND not an in-progress draft. This is the shared
 * definition "the current version" is resolved with.
 */
export function isVersionSnapshot(snapshot: CreatedKindLike): boolean {
  return snapshot.draft !== true && isSubstantiveKind(snapshot.created_kind)
}

/**
 * The newest version in a version-DESCENDING snapshot list, or null when the
 * pipeline has no version yet. This is the ONE client-side definition of "the
 * pipeline's current version", shared by the toolbar chip and the timeline.
 *
 * The chip applies it to the SERVER-FILTERED newest-version page (see
 * `NON_VERSION_CREATED_KINDS` + `exclude_drafts`), so the definition holds
 * without the version needing to fall inside a fixed-size window. It also
 * skips drafts client-side: the server filter is defence-in-depth, but a draft
 * row that slipped through must never be labelled the current version.
 */
export function currentSubstantiveVersion<T extends CreatedKindLike>(snapshots: readonly T[]): T | null {
  return snapshots.find(isVersionSnapshot) ?? null
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
