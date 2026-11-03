import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { api, getAccessToken } from '../lib/api/client'
import type { components } from '../lib/api/schema'
import { withTimeout } from '../lib/asyncUtils'
import { withTimeout as withRequestDeadline } from '../lib/requestTimeout'
import { useCurrentUser } from '../composables/useCurrentUser'
import { usePlanStore } from './planStore'
import { SYSTEM_CONFIG_MANAGE_PERMISSION } from '../composables/useLicenceAccess'
import i18n from '../i18n'

type SystemMapOverview = components['schemas']['SystemMapOverviewResponse']
type SystemMapList = components['schemas']['SystemMapListResponse']

export type SystemMap = components['schemas']['SystemMapResponse']
export type SystemMapRevision = components['schemas']['SystemMapRevisionResponse']
export type SystemMapRevisionHistoryEntry = components['schemas']['SystemMapRevisionHistoryEntry']
export type SystemMapRevisionDiff = components['schemas']['SystemMapRevisionDiffResponse']
export type SystemMapNode = components['schemas']['SystemMapNodeResponse']
export type SystemMapRelationship = components['schemas']['SystemMapRelationshipResponse']
export type SystemMapGraph = components['schemas']['SystemMapRevisionDetailResponse']
export type DiscoveryReport = components['schemas']['SystemMapDiscoveryReport']

export interface DiscoverySelection {
  acceptedNodes: number[]
  acceptedEdges: number[]
  rejectedNodes: number[]
  rejectedEdges: number[]
}

// These vocabularies MUST mirror the backend CHECK constraints in
// backend/src/modulo/api/schemas/system_maps.py (VALID_NODE_TYPES / VALID_REL_TYPES).
// A mismatch makes every authored payload fail validation with a 422.
export const NODE_TYPES = ['pipeline', 'agent', 'connector', 'improvement_loop', 'data_store', 'external_system'] as const
export const RELATIONSHIP_TYPES = ['flow', 'data', 'trigger', 'loop_back'] as const
export type NodeType = (typeof NODE_TYPES)[number]
export type RelationshipType = (typeof RELATIONSHIP_TYPES)[number]

interface InternalFeatureFlagsPayload { flags?: Array<{ name: string; currently_active: boolean }> }

/** The overview aggregate is a read model; a stalled request must not hang it. */
const OVERVIEW_REQUEST_TIMEOUT_MS = 15000
const SYSTEM_MAP_PAGE_SIZE = 20
const MAX_SYSTEM_MAP_PAGES = 1000

/**
 * The history endpoint returns the WHOLE revision list in one response (it has
 * no page/page_size query parameters), so the sidebar pages client-side over
 * that complete set. Ten rows keeps the panel scrollable without hiding the
 * shape of the history.
 */
export const REVISION_HISTORY_PAGE_SIZE = 10

/**
 * A by-time 404 is an EMPTY state, not a failure: the map simply had no
 * published revision at that instant (typically a timestamp before the first
 * publish). Detect it from the HTTP status, or from the problem payload when a
 * test double supplies only the error body.
 */
function isNotFoundResult(error: unknown, response: Response | undefined): boolean {
  if (response && typeof response.status === 'number') return response.status === 404
  if (error && typeof error === 'object') {
    const candidate = error as { status?: unknown; code?: unknown; type?: unknown }
    if (candidate.status === 404) return true
    if (candidate.code === 'not_found') return true
    if (typeof candidate.type === 'string' && candidate.type.endsWith(':not_found')) return true
  }
  return false
}

/** Prefer the detail the API returned over a generic message. */
function apiErrorMessage(error: unknown, fallback: string): string {
  if (error && typeof error === 'object') {
    const detail = (error as { detail?: unknown }).detail
    if (typeof detail === 'string' && detail.length > 0) return detail
  }
  return fallback
}

function thrownMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback
}

// The store is not a component, so it cannot call useI18n(). Use the shared
// i18n instance directly so error strings stay translatable and consistent.
function tr(key: string): string { return i18n.global.t(key) }

export const useSystemMapsStore = defineStore('systemMaps', () => {
  const loaded = ref(false)
  const loading = ref(false)
  const error = ref<string | null>(null)
  const maps = ref<SystemMap[]>([])
  const total = ref(0)
  const page = ref(1)
  const pageSize = ref(20)
  const activeMap = ref<SystemMap | null>(null)
  const activeRevision = ref<SystemMapRevision | null>(null)
  const graph = ref<SystemMapGraph | null>(null)
  // The revision history the API reports (published, draft and archived), each
  // with effective time range and the is_current marker. The sidebar renders
  // THIS — never a list accumulated from whatever this session happened to load.
  const revisionHistory = ref<SystemMapRevisionHistoryEntry[]>([])
  const revisionHistoryLoaded = ref(false)
  const revisionHistoryLoading = ref(false)
  const revisionHistoryError = ref<string | null>(null)
  const revisionHistoryPage = ref(1)
  const revisionHistoryPageSize = ref(REVISION_HISTORY_PAGE_SIZE)
  // /revisions/current — the revision published right now (null when the map
  // has never been published, which is an empty state rather than an error).
  const currentRevision = ref<SystemMapRevision | null>(null)
  const currentRevisionLoading = ref(false)
  const currentRevisionError = ref<string | null>(null)
  // /revisions/by-time — the revision that was active at a chosen instant.
  const byTimeRevision = ref<SystemMapRevision | null>(null)
  const byTimeRequestedAt = ref<string | null>(null)
  const byTimeLoading = ref(false)
  const byTimeError = ref<string | null>(null)
  const byTimeUnavailable = ref(false)
  // /revisions/{rev_id}/diff — a comparison between two concrete revisions.
  const revisionDiff = ref<SystemMapRevisionDiff | null>(null)
  const revisionDiffLoading = ref(false)
  const revisionDiffError = ref<string | null>(null)
  // /revisions/{rev_id}/restore-as-new — creates a draft, never publishes.
  const restoreLoading = ref(false)
  const restoreError = ref<string | null>(null)
  const restoredRevision = ref<SystemMapRevision | null>(null)
  const discovery = ref<DiscoveryReport | null>(null)
  const overview = ref<SystemMapOverview | null>(null)
  const overviewLoading = ref(false)
  const overviewError = ref<string | null>(null)
  // Distinct from an error: discovery has published nothing yet, which is an
  // empty state rather than a failed request.
  const overviewEmpty = ref(false)
  const accessLoaded = ref(false)
  const systemMapsEnabled = ref(false)
  let accessPromise: Promise<void> | null = null
  let accessGeneration = 0
  // Bumped by every overview fetch AND by reset(), so a superseded request can
  // never repopulate state (a stale principal's response after a logout, or an
  // older fetch resolving after a newer one).
  let overviewGeneration = 0
  let accessTokenKey: string | null = null

  const planStore = usePlanStore()
  const { isOperator, isSystemAdmin } = useCurrentUser()
  const hasSystemMapsAccess = computed(() => planStore.userPermissions.includes('system_maps_access'))
  const canProbeInternalFlags = computed(() => planStore.userPermissions.includes(SYSTEM_CONFIG_MANAGE_PERMISSION))
  const hasAccess = computed(() => systemMapsEnabled.value && (isOperator.value || isSystemAdmin.value) && hasSystemMapsAccess.value)

  /** History in the order operators read it: newest revision first. */
  const revisionHistoryNewestFirst = computed(() =>
    [...revisionHistory.value].sort((a, b) => b.revision.revision_number - a.revision.revision_number),
  )
  const revisionHistoryTotal = computed(() => revisionHistoryNewestFirst.value.length)
  const revisionHistoryPageCount = computed(() =>
    Math.max(1, Math.ceil(revisionHistoryTotal.value / revisionHistoryPageSize.value)),
  )
  /** The slice of history the current page shows — the sidebar renders this. */
  const revisionHistoryPageRevisions = computed<SystemMapRevision[]>(() => {
    const start = (revisionHistoryPage.value - 1) * revisionHistoryPageSize.value
    return revisionHistoryNewestFirst.value
      .slice(start, start + revisionHistoryPageSize.value)
      .map((entry) => entry.revision)
  })
  /** The revision published right now, per the history endpoint's is_current marker. */
  const currentRevisionId = computed<string | null>(() => {
    const marker = revisionHistory.value.find((entry) => entry.is_current)
    return marker?.revision.id ?? currentRevision.value?.id ?? null
  })

  function reset(): void {
    accessGeneration += 1
    overviewGeneration += 1
    loaded.value = false
    loading.value = false
    error.value = null
    maps.value = []
    activeMap.value = null
    activeRevision.value = null
    graph.value = null
    revisionHistory.value = []
    revisionHistoryLoaded.value = false
    revisionHistoryLoading.value = false
    revisionHistoryError.value = null
    revisionHistoryPage.value = 1
    revisionHistoryPageSize.value = REVISION_HISTORY_PAGE_SIZE
    currentRevision.value = null
    currentRevisionLoading.value = false
    currentRevisionError.value = null
    byTimeRevision.value = null
    byTimeRequestedAt.value = null
    byTimeLoading.value = false
    byTimeError.value = null
    byTimeUnavailable.value = false
    revisionDiff.value = null
    revisionDiffLoading.value = false
    revisionDiffError.value = null
    restoreLoading.value = false
    restoreError.value = null
    restoredRevision.value = null
    discovery.value = null
    overview.value = null
    overviewLoading.value = false
    overviewError.value = null
    overviewEmpty.value = false
    accessLoaded.value = false
    systemMapsEnabled.value = false
    accessPromise = null
    accessTokenKey = getAccessToken()
  }

  function isCurrentAccessRequest(generation: number, token: string | null): boolean {
    return generation === accessGeneration && token === getAccessToken() && token === accessTokenKey
  }

  /** Start an overview request and capture the principal that owns its result. */
  function beginOverviewRequest(): { generation: number; token: string | null } {
    overviewGeneration += 1
    overviewLoading.value = true
    overviewError.value = null
    overviewEmpty.value = false
    return { generation: overviewGeneration, token: getAccessToken() }
  }

  function isCurrentOverviewRequest(generation: number, token: string | null): boolean {
    return generation === overviewGeneration && token === getAccessToken()
  }

  async function fetchAccess(): Promise<void> {
    const token = getAccessToken()
    if (token !== accessTokenKey) reset()
    accessTokenKey = token
    if (accessLoaded.value) return
    if (accessPromise) return accessPromise
    const generation = accessGeneration
    const request = resolveAccess(generation, token)
    accessPromise = request
    try { await request } finally { if (accessPromise === request) accessPromise = null }
  }

  async function resolveAccess(generation: number, token: string | null): Promise<void> {
    const current = () => isCurrentAccessRequest(generation, token)
    loading.value = true
    error.value = null
    systemMapsEnabled.value = false
    try {
      if (!isOperator.value && !isSystemAdmin.value) return
      const planRequest = planStore.loaded || planStore.currentTier !== 'community' ? Promise.resolve() : planStore.fetchPlan()
      await Promise.all([planRequest, planStore.fetchPermissions()])
      if (!planStore.devMode || !hasSystemMapsAccess.value || !canProbeInternalFlags.value) return
      const response = await withTimeout(api.GET('/api/v1/admin/feature-flags'), 15000, 'Internal feature flags request')
      const flags = (response.data as InternalFeatureFlagsPayload | undefined)?.flags
      if (current()) systemMapsEnabled.value = Array.isArray(flags) && flags.some((flag) => flag.name === 'system_maps' && flag.currently_active)
    } catch (err: unknown) {
      if (current()) error.value = err instanceof Error ? err.message : tr('systemMaps.errors.feature_flags')
    } finally {
      if (current()) { accessLoaded.value = true; loading.value = false }
    }
  }

  /**
   * Load the overview for the caller's default map.
   *
   * Only a map with a published revision can be aggregated, so drafts are never
   * selected: a published default wins, then any published map. No published
   * map settles to the empty state; a failed request is an error.
   */
  async function fetchDefaultOverview(): Promise<void> {
    const request = beginOverviewRequest()
    try {
      const publishedItems: SystemMapList['items'] = []
      let nextPage = 1
      let listTotal = Number.POSITIVE_INFINITY

      while (publishedItems.length < listTotal && nextPage <= MAX_SYSTEM_MAP_PAGES) {
        const response = await withRequestDeadline(
          (signal) =>
            api.GET('/api/v1/system-maps', {
              params: { query: { page: nextPage, page_size: SYSTEM_MAP_PAGE_SIZE } },
              signal,
            }),
          OVERVIEW_REQUEST_TIMEOUT_MS,
        )
        if (!isCurrentOverviewRequest(request.generation, request.token)) return
        if (response.error || !response.data) {
          throw new Error(apiErrorMessage(response.error, 'System map list request failed'))
        }

        const pageData = response.data as SystemMapList
        publishedItems.push(...pageData.items.filter((item) => item.published_revision_id != null))
        listTotal = pageData.total
        if (pageData.items.length === 0 || pageData.items.length < pageData.page_size) break
        nextPage += 1
      }

      if (nextPage > MAX_SYSTEM_MAP_PAGES) {
        throw new Error('System map list pagination exceeded its safety limit')
      }
      const map = publishedItems.find((item) => item.default_map) ?? publishedItems[0]
      if (!map) {
        overview.value = null
        overviewEmpty.value = true
        return
      }
      await fetchOverview(map.id)
    } catch (err: unknown) {
      if (!isCurrentOverviewRequest(request.generation, request.token)) return
      overview.value = null
      overviewEmpty.value = false
      overviewError.value = thrownMessage(err, 'System map list request failed')
    } finally {
      if (isCurrentOverviewRequest(request.generation, request.token)) overviewLoading.value = false
    }
  }

  /** Load the overview aggregate for one published map. */
  async function fetchOverview(mapId: string): Promise<void> {
    const request = beginOverviewRequest()
    try {
      const response = await withRequestDeadline(
        (signal) =>
          api.GET('/api/v1/system-maps/{map_id}/overview', {
            params: { path: { map_id: mapId } },
            signal,
          }),
        OVERVIEW_REQUEST_TIMEOUT_MS,
      )
      if (!isCurrentOverviewRequest(request.generation, request.token)) return
      if (response.error || !response.data) {
        throw new Error(apiErrorMessage(response.error, 'System overview request failed'))
      }
      overview.value = response.data
      overviewEmpty.value = false
      overviewError.value = null
    } catch (err: unknown) {
      if (!isCurrentOverviewRequest(request.generation, request.token)) return
      overview.value = null
      overviewEmpty.value = false
      overviewError.value = thrownMessage(err, 'System overview request failed')
    } finally {
      if (isCurrentOverviewRequest(request.generation, request.token)) overviewLoading.value = false
    }
  }

  async function fetchMaps(nextPage = page.value): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const response = await api.GET('/api/v1/system-maps', { params: { query: { page: nextPage, page_size: pageSize.value } } })
      if (response.error) throw new Error(tr('systemMaps.errors.load_maps'))
      const payload = response.data
      maps.value = payload?.items ?? []
      total.value = payload?.total ?? 0
      page.value = payload?.page ?? nextPage
      loaded.value = true
    } catch (err: unknown) { error.value = err instanceof Error ? err.message : tr('systemMaps.errors.load_maps') }
    finally { loading.value = false }
  }

  async function createMap(name: string, description = ''): Promise<SystemMap | null> {
    const response = await api.POST('/api/v1/system-maps', { body: { name, description, visibility: 'org', default_map: false } })
    if (response.error || !response.data) throw new Error(tr('systemMaps.errors.create_map'))
    await fetchMaps(1)
    return response.data
  }

  async function deleteMap(mapId: string): Promise<void> {
    const response = await api.DELETE('/api/v1/system-maps/{map_id}', { params: { path: { map_id: mapId } } })
    if (response.error) throw new Error(tr('systemMaps.errors.delete_map'))
    if (activeMap.value?.id === mapId) clearActive()
    await fetchMaps()
  }

  async function loadMap(mapId: string): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const response = await api.GET('/api/v1/system-maps/{map_id}', { params: { path: { map_id: mapId } } })
      if (response.error || !response.data) throw new Error(tr('systemMaps.errors.load_map'))
      activeMap.value = response.data
      if (response.data.published_revision_id) await loadRevision(mapId, response.data.published_revision_id)
    } catch (err: unknown) { error.value = err instanceof Error ? err.message : tr('systemMaps.errors.load_map') }
    finally { loading.value = false }
  }

  async function loadRevision(mapId: string, revisionId: string): Promise<void> {
    const response = await api.GET('/api/v1/system-maps/{map_id}/revisions/{rev_id}', { params: { path: { map_id: mapId, rev_id: revisionId }, query: { include_graph: true } } })
    if (response.error || !response.data) throw new Error(tr('systemMaps.errors.load_map_revision'))
    const value = response.data as SystemMapGraph
    activeRevision.value = 'revision' in value ? value.revision : value as SystemMapRevision
    graph.value = 'revision' in value ? value : null
  }

  function setRevisionHistoryPage(nextPage: number): void {
    const clamped = Math.min(Math.max(1, Math.trunc(nextPage) || 1), revisionHistoryPageCount.value)
    revisionHistoryPage.value = clamped
  }

  /**
   * Load the map's whole revision history (published, draft and archived) with
   * its effective time ranges and the is_current marker. A failure is a real
   * error — it must never read as "this map has no revisions".
   */
  async function fetchRevisionHistory(mapId: string): Promise<void> {
    revisionHistoryLoading.value = true
    revisionHistoryError.value = null
    try {
      const response = await api.GET('/api/v1/system-maps/{map_id}/revisions/history', { params: { path: { map_id: mapId } } })
      if (response.error || !response.data) {
        throw new Error(apiErrorMessage(response.error, tr('systemMaps.errors.load_revision_history')))
      }
      revisionHistory.value = response.data.revisions ?? []
      revisionHistoryLoaded.value = true
      setRevisionHistoryPage(revisionHistoryPage.value)
    } catch (err: unknown) {
      revisionHistoryError.value = thrownMessage(err, tr('systemMaps.errors.load_revision_history'))
    } finally {
      revisionHistoryLoading.value = false
    }
  }

  /**
   * Load the revision published right now. A 404 means the map has never been
   * published — an empty state, not a failed request.
   */
  async function fetchCurrentRevision(mapId: string): Promise<SystemMapRevision | null> {
    currentRevisionLoading.value = true
    currentRevisionError.value = null
    try {
      const response = await api.GET('/api/v1/system-maps/{map_id}/revisions/current', { params: { path: { map_id: mapId } } })
      if (response.error || !response.data) {
        if (isNotFoundResult(response.error, response.response)) {
          currentRevision.value = null
          return null
        }
        throw new Error(apiErrorMessage(response.error, tr('systemMaps.errors.load_current_revision')))
      }
      currentRevision.value = response.data.revision
      return currentRevision.value
    } catch (err: unknown) {
      currentRevision.value = null
      currentRevisionError.value = thrownMessage(err, tr('systemMaps.errors.load_current_revision'))
      return null
    } finally {
      currentRevisionLoading.value = false
    }
  }

  /**
   * Load the revision that was active at `at` (ISO8601). A 404 means nothing
   * was published at that instant, which the UI renders as an honest empty
   * state rather than an error.
   */
  async function fetchRevisionByTime(mapId: string, at: string): Promise<SystemMapRevision | null> {
    byTimeLoading.value = true
    byTimeError.value = null
    byTimeUnavailable.value = false
    byTimeRequestedAt.value = at
    byTimeRevision.value = null
    try {
      const response = await api.GET('/api/v1/system-maps/{map_id}/revisions/by-time', {
        params: { path: { map_id: mapId }, query: { at } },
      })
      if (response.error || !response.data) {
        if (isNotFoundResult(response.error, response.response)) {
          byTimeUnavailable.value = true
          return null
        }
        throw new Error(apiErrorMessage(response.error, tr('systemMaps.errors.load_revision_by_time')))
      }
      byTimeRevision.value = response.data.revision
      if (response.data.requested_at) byTimeRequestedAt.value = response.data.requested_at
      return byTimeRevision.value
    } catch (err: unknown) {
      byTimeError.value = thrownMessage(err, tr('systemMaps.errors.load_revision_by_time'))
      return null
    } finally {
      byTimeLoading.value = false
    }
  }

  /** Clear the restore outcome before a new restore attempt or panel open. */
  function clearRestoreState(): void {
    restoreError.value = null
    restoredRevision.value = null
  }

  function clearRestoreError(): void {
    restoreError.value = null
  }

  /** Forget a point-in-time selection (returning to the live published view). */
  function clearByTimeRevision(): void {
    byTimeRevision.value = null
    byTimeRequestedAt.value = null
    byTimeError.value = null
    byTimeUnavailable.value = false
  }

  /**
   * Diff two revisions. `baseRevisionId` is the "before" side (the endpoint's
   * path revision); `targetRevisionId` is the "after" side. Omitting the target
   * asks the backend to compare against the current draft.
   */
  async function diffRevisions(mapId: string, baseRevisionId: string, targetRevisionId?: string | null): Promise<SystemMapRevisionDiff | null> {
    revisionDiffLoading.value = true
    revisionDiffError.value = null
    revisionDiff.value = null
    try {
      const response = await api.POST('/api/v1/system-maps/{map_id}/revisions/{rev_id}/diff', {
        params: { path: { map_id: mapId, rev_id: baseRevisionId } },
        body: { against_revision_id: targetRevisionId ?? null },
      })
      if (response.error || !response.data) {
        throw new Error(apiErrorMessage(response.error, tr('systemMaps.errors.diff_revisions')))
      }
      revisionDiff.value = response.data
      return revisionDiff.value
    } catch (err: unknown) {
      revisionDiffError.value = thrownMessage(err, tr('systemMaps.errors.diff_revisions'))
      return null
    } finally {
      revisionDiffLoading.value = false
    }
  }

  /**
   * Create a NEW DRAFT from an existing published/archived revision. This never
   * publishes: the returned revision is a draft the operator must review and
   * publish explicitly. Throws on failure so the caller can surface it inline.
   */
  async function restoreRevisionAsNew(mapId: string, revisionId: string): Promise<SystemMapRevision> {
    restoreLoading.value = true
    restoreError.value = null
    restoredRevision.value = null
    try {
      const response = await api.POST('/api/v1/system-maps/{map_id}/revisions/{rev_id}/restore-as-new', {
        params: { path: { map_id: mapId, rev_id: revisionId } },
      })
      if (response.error || !response.data) {
        throw new Error(apiErrorMessage(response.error, tr('systemMaps.errors.restore_revision')))
      }
      restoredRevision.value = response.data.new_revision
      await fetchRevisionHistory(mapId)
      return restoredRevision.value
    } catch (err: unknown) {
      restoreError.value = thrownMessage(err, tr('systemMaps.errors.restore_revision'))
      throw err instanceof Error ? err : new Error(restoreError.value)
    } finally {
      restoreLoading.value = false
    }
  }

  /**
   * Load (and display) the revision published right now.
   *
   * The version-aware endpoint returns revision metadata only; the graph is
   * read from the per-revision endpoint so every view (current, historical or
   * explicitly selected) renders through the same read path.
   */
  async function loadCurrentRevision(mapId: string): Promise<void> {
    const revision = await fetchCurrentRevision(mapId)
    if (!revision) throw new Error(currentRevisionError.value ?? tr('systemMaps.errors.load_current_revision'))
    await loadRevision(mapId, revision.id)
  }

  /** Load (and display) the revision that was active at an ISO-8601 timestamp. */
  async function loadRevisionByTime(mapId: string, at: string): Promise<void> {
    const revision = await fetchRevisionByTime(mapId, at)
    if (revision) {
      await loadRevision(mapId, revision.id)
      return
    }
    // 404 => nothing was active at that instant. The state records it so the
    // caller can render an empty state, and the throw keeps the distinct
    // message reachable for callers that only consume the error.
    if (byTimeUnavailable.value) throw new Error(tr('systemMaps.errors.no_revision_at_time'))
    if (byTimeError.value) throw new Error(byTimeError.value)
    throw new Error(tr('systemMaps.errors.load_revision_at_time'))
  }

  async function saveDraft(mapId: string, nodes: Array<{ node_key: string; node_type: string; display_name: string; config_json?: Record<string, unknown>; pipeline_id?: string | null; connector_instance_id?: string | null; owner_team_id?: string | null; eval_id?: string | null; position_x: string | number; position_y: string | number }>, relationships: Array<{ source_node_key: string; target_node_key: string; relationship_type: string; config_json?: Record<string, unknown> }>, notes: string): Promise<SystemMapRevision> {
    const response = await api.POST('/api/v1/system-maps/{map_id}/revisions', { params: { path: { map_id: mapId } }, body: { nodes, relationships, loop_configs: [], notes } })
    if (response.error || !response.data) throw new Error(tr('systemMaps.errors.save_draft_revision'))
    activeRevision.value = response.data
    await loadRevision(mapId, response.data.id)
    return response.data
  }

  async function publish(mapId: string, revisionId: string): Promise<void> {
    const response = await api.POST('/api/v1/system-maps/{map_id}/revisions/{rev_id}/publish', { params: { path: { map_id: mapId, rev_id: revisionId } } })
    if (response.error) throw new Error(tr('systemMaps.errors.publish_revision'))
    await loadMap(mapId)
  }

  async function archive(mapId: string, revisionId: string): Promise<void> {
    const response = await api.POST('/api/v1/system-maps/{map_id}/revisions/{rev_id}/archive', { params: { path: { map_id: mapId, rev_id: revisionId } } })
    if (response.error) throw new Error(tr('systemMaps.errors.archive_revision'))
    await loadMap(mapId)
  }

  async function discover(name = 'Discovered System Map', description?: string): Promise<DiscoveryReport> {
    const response = await api.POST('/api/v1/system-maps/discover', { body: { name, description } })
    if (response.error || !response.data) throw new Error(tr('systemMaps.errors.discover_map'))
    discovery.value = response.data
    await loadMap(response.data.draft_map_id)
    // The map read model only exposes the published revision, so load the
    // freshly-created draft explicitly; otherwise a discovery result would
    // land on the empty-state screen until the draft was saved again.
    if (response.data.draft_revision_id) {
      try {
        await loadRevision(response.data.draft_map_id, response.data.draft_revision_id)
      } catch (err: unknown) {
        // Discovery succeeded; surface the draft-fetch failure without discarding the report.
        error.value = err instanceof Error ? err.message : tr('systemMaps.errors.load_discovered_draft')
      }
    }
    return response.data
  }

  function clearActive(): void {
    activeMap.value = null
    activeRevision.value = null
    graph.value = null
    discovery.value = null
    clearByTimeRevision()
    revisionDiff.value = null
    revisionDiffError.value = null
    restoreError.value = null
    restoredRevision.value = null
  }

  // Kept for the dashboard's preview card; discovery is now a real operation.
  async function fetchDiscovery(): Promise<void> { loaded.value = true }

  return {
    loaded, loading, error, maps, total, page, pageSize, activeMap, activeRevision, graph,
    revisionHistory, revisionHistoryLoaded, revisionHistoryLoading, revisionHistoryError,
    revisionHistoryPage, revisionHistoryPageSize, revisionHistoryTotal, revisionHistoryPageCount,
    revisionHistoryPageRevisions, currentRevisionId,
    currentRevision, currentRevisionLoading, currentRevisionError,
    byTimeRevision, byTimeRequestedAt, byTimeLoading, byTimeError, byTimeUnavailable,
    revisionDiff, revisionDiffLoading, revisionDiffError,
    restoreLoading, restoreError, restoredRevision,
    discovery, overview, overviewLoading, overviewError, overviewEmpty, accessLoaded, systemMapsEnabled, hasAccess,
    reset, fetchAccess, fetchOverview, fetchDefaultOverview, fetchMaps, createMap, deleteMap, loadMap, loadRevision,
    loadCurrentRevision, loadRevisionByTime,
    fetchRevisionHistory, setRevisionHistoryPage, fetchCurrentRevision, fetchRevisionByTime, clearByTimeRevision,
    diffRevisions, restoreRevisionAsNew, clearRestoreState, clearRestoreError,
    saveDraft, publish, archive, discover, clearActive, fetchDiscovery,
  }
})
