import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { api, getAccessToken } from '../lib/api/client'
import type { components } from '../lib/api/schema'
import { withTimeout } from '../lib/asyncUtils'
import { isFeatureRequiredProblem } from '../lib/api/formatError'
import { withTimeout as withRequestDeadline } from '../lib/requestTimeout'
import { useCurrentUser } from '../composables/useCurrentUser'
import { usePlanStore } from './planStore'
import { SYSTEM_CONFIG_MANAGE_PERMISSION } from '../composables/useLicenceAccess'
import i18n from '../i18n'

type SystemMapOverview = components['schemas']['SystemMapOverviewResponse']
type SystemMapList = components['schemas']['SystemMapListResponse']
type SystemMapCurrentRevision = components['schemas']['SystemMapCurrentRevisionResponse']
type SystemMapRevisionByTime = components['schemas']['SystemMapRevisionByTimeResponse']
type SystemMapRevisionHistory = components['schemas']['SystemMapRevisionHistoryResponse']
type SystemMapRevisionRestoreAsNewResponse = components['schemas']['SystemMapRevisionRestoreAsNewResponse']
export type SystemMapRevisionDiff = components['schemas']['SystemMapRevisionDiffResponse']

export type SystemMap = components['schemas']['SystemMapResponse']
export type SystemMapRevision = components['schemas']['SystemMapRevisionResponse']
export type SystemMapNode = components['schemas']['SystemMapNodeResponse']
export type SystemMapRelationship = components['schemas']['SystemMapRelationshipResponse']
export type SystemMapGraph = components['schemas']['SystemMapRevisionDetailResponse']
export type DiscoveryReport = components['schemas']['SystemMapDiscoveryReport']
export type AttributionBinding = components['schemas']['AttributionBindingResponse']
export type AttributionBindingCreate = components['schemas']['AttributionBindingCreate']

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
  // A 402 feature_required is an entitlement answer, not a transient failure:
  // it gets its own state so the views never offer a retry that cannot work.
  const unavailable = ref(false)
  const maps = ref<SystemMap[]>([])
  const total = ref(0)
  const page = ref(1)
  const pageSize = ref(20)
  const activeMap = ref<SystemMap | null>(null)
  const activeRevision = ref<SystemMapRevision | null>(null)
  const graph = ref<SystemMapGraph | null>(null)
  const revisions = ref<SystemMapRevision[]>([])
  const discovery = ref<DiscoveryReport | null>(null)
  const attributionBindings = ref<AttributionBinding[]>([])
  const overview = ref<SystemMapOverview | null>(null)
  const overviewLoading = ref(false)
  const overviewError = ref<string | null>(null)
  const overviewUnavailable = ref(false)
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
  const hasSystemMapsRead = computed(() => planStore.userPermissions.includes('system_maps.read'))
  const canProbeInternalFlags = computed(() => planStore.userPermissions.includes(SYSTEM_CONFIG_MANAGE_PERMISSION))
  const hasAccess = computed(() => systemMapsEnabled.value && (isOperator.value || isSystemAdmin.value) && hasSystemMapsAccess.value)
  const hasReadAccess = computed(() => systemMapsEnabled.value && hasSystemMapsRead.value)

  function reset(): void {
    accessGeneration += 1
    overviewGeneration += 1
    loaded.value = false
    loading.value = false
    error.value = null
    unavailable.value = false
    maps.value = []
    activeMap.value = null
    activeRevision.value = null
    graph.value = null
    revisions.value = []
    discovery.value = null
    attributionBindings.value = []
    overview.value = null
    overviewLoading.value = false
    overviewError.value = null
    overviewUnavailable.value = false
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
    overviewUnavailable.value = false
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
      const planRequest = planStore.loaded || planStore.currentTier !== 'community' ? Promise.resolve() : planStore.fetchPlan()
      await Promise.all([planRequest, planStore.fetchPermissions()])
      if (hasSystemMapsRead.value) systemMapsEnabled.value = true
      if (!isOperator.value && !isSystemAdmin.value) return
      if (!planStore.devMode || !hasSystemMapsAccess.value || !canProbeInternalFlags.value) return
      const response = await withTimeout(api.GET('/api/v1/admin/feature-flags'), 15000, 'Internal feature flags request')
      const flags = (response.data as InternalFeatureFlagsPayload | undefined)?.flags
      if (current() && Array.isArray(flags)) systemMapsEnabled.value = flags.some((flag) => flag.name === 'system_maps' && flag.currently_active) || hasReadAccess.value
    } catch (err: unknown) {
      if (current()) error.value = err instanceof Error ? err.message : tr('systemMaps.errors.feature_flags')
    } finally {
      if (current()) { accessLoaded.value = true; loading.value = false }
    }
  }

  /**
   * Page through the map list and collect only published maps.
   *
   * The caller may be superseded by a newer request (``stale``) or refused for
   * entitlement reasons (``feature_required``); both must stop pagination
   * without surfacing an error. Kept separate so ``fetchDefaultOverview`` owns
   * the settle/fetch flow while this function owns the pagination invariants.
   */
  async function collectPublishedMapItems(
    request: { generation: number; token: string | null },
  ): Promise<
    | { kind: 'ok'; items: SystemMapList['items'] }
    | { kind: 'stale' }
    | { kind: 'feature_required' }
  > {
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
      if (!isCurrentOverviewRequest(request.generation, request.token)) return { kind: 'stale' }
      if (response.error || !response.data) {
        if (isFeatureRequiredProblem(response.error, response.response)) return { kind: 'feature_required' }
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
    return { kind: 'ok', items: publishedItems }
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
      const outcome = await collectPublishedMapItems(request)
      if (outcome.kind === 'stale') return
      if (outcome.kind === 'feature_required') {
        overview.value = null
        overviewEmpty.value = false
        overviewError.value = null
        overviewUnavailable.value = true
        return
      }
      const map = outcome.items.find((item) => item.default_map) ?? outcome.items[0]
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
        if (isFeatureRequiredProblem(response.error, response.response)) {
          overview.value = null
          overviewEmpty.value = false
          overviewError.value = null
          overviewUnavailable.value = true
          return
        }
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

  /** Settle into the entitlement state: clear any loaded data and never retry. */
  function markUnavailable(): void {
    unavailable.value = true
    error.value = null
    maps.value = []
    total.value = 0
    activeMap.value = null
    activeRevision.value = null
    graph.value = null
  }

  async function fetchMaps(nextPage = page.value): Promise<void> {
    loading.value = true
    error.value = null
    unavailable.value = false
    try {
      const response = await api.GET('/api/v1/system-maps', { params: { query: { page: nextPage, page_size: pageSize.value } } })
      if (response.error) {
        if (isFeatureRequiredProblem(response.error, response.response)) {
          markUnavailable()
          loaded.value = true
          return
        }
        throw new Error(tr('systemMaps.errors.load_maps'))
      }
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
    unavailable.value = false
    try {
      const response = await api.GET('/api/v1/system-maps/{map_id}', { params: { path: { map_id: mapId } } })
      if (response.error || !response.data) {
        if (isFeatureRequiredProblem(response.error, response.response)) {
          markUnavailable()
          return
        }
        throw new Error(tr('systemMaps.errors.load_map'))
      }
      activeMap.value = response.data
      if (response.data.published_revision_id) await loadRevision(mapId, response.data.published_revision_id)
    } catch (err: unknown) {
      if (unavailable.value) return
      error.value = err instanceof Error ? err.message : tr('systemMaps.errors.load_map')
    }
    finally { loading.value = false }
  }

  async function loadRevision(mapId: string, revisionId: string): Promise<void> {
    const response = await api.GET('/api/v1/system-maps/{map_id}/revisions/{rev_id}', { params: { path: { map_id: mapId, rev_id: revisionId }, query: { include_graph: true } } })
    if (response.error || !response.data) {
      if (isFeatureRequiredProblem(response.error, response.response)) {
        markUnavailable()
        return
      }
      throw new Error(tr('systemMaps.errors.load_map_revision'))
    }
    const value = response.data as SystemMapGraph
    activeRevision.value = 'revision' in value ? value.revision : value as SystemMapRevision
    graph.value = 'revision' in value ? value : null
    if (activeRevision.value) {
      // A re-read must refresh the cached entry, not just append a missing one:
      // publish/archive reload the revision through here, and keeping the stale
      // object would leave the history sidebar showing the old status chip.
      const cached = revisions.value.findIndex((revision) => revision.id === activeRevision.value?.id)
      revisions.value = cached === -1
        ? [...revisions.value, activeRevision.value]
        : revisions.value.map((revision, index) => (index === cached ? activeRevision.value as SystemMapRevision : revision))
    }
  }

  async function loadRevisionHistory(mapId: string): Promise<void> {
    const response = await api.GET('/api/v1/system-maps/{map_id}/revisions/history', { params: { path: { map_id: mapId } } })
    if (response.error || !response.data) throw new Error(tr('systemMaps.errors.load_revision_history'))
    const payload = response.data as SystemMapRevisionHistory
    revisions.value = payload.revisions.map((entry) => entry.revision)
  }

  /**
   * Load the currently published revision (the "current" view).
   *
   * The version-aware endpoint returns revision metadata only; the graph is
   * read from the per-revision endpoint so every view (current, historical or
   * explicitly selected) renders through the same read path.
   */
  async function loadCurrentRevision(mapId: string): Promise<void> {
    const response = await api.GET('/api/v1/system-maps/{map_id}/revisions/current', { params: { path: { map_id: mapId } } })
    const revisionId = (response.data as SystemMapCurrentRevision | undefined)?.revision?.id
    if (response.error || !revisionId) {
      if (isFeatureRequiredProblem(response.error, response.response)) {
        markUnavailable()
        return
      }
      throw new Error(apiErrorMessage(response.error, tr('systemMaps.errors.load_current_revision')))
    }
    await loadRevision(mapId, revisionId)
  }

  /** Load the revision that was active at an ISO-8601 timestamp (historical view). */
  async function loadRevisionByTime(mapId: string, at: string): Promise<void> {
    const response = await api.GET('/api/v1/system-maps/{map_id}/revisions/by-time', { params: { path: { map_id: mapId }, query: { at } } })
    // The backend answers 404 when nothing was active at that instant (the
    // success payload always carries matched=true and a revision), so the
    // status - not a matched=false body - is the empty-result signal. Map it
    // onto the distinct message instead of a generic load failure.
    if (response.response.status === 404) {
      throw new Error(tr('systemMaps.errors.no_revision_at_time'))
    }
    if (response.error) {
      if (isFeatureRequiredProblem(response.error, response.response)) {
        markUnavailable()
        return
      }
      throw new Error(apiErrorMessage(response.error, tr('systemMaps.errors.load_revision_at_time')))
    }
    const payload = response.data as SystemMapRevisionByTime | undefined
    const revisionId = payload?.revision?.id
    if (!revisionId) {
      throw new Error(tr('systemMaps.errors.load_revision_at_time'))
    }
    await loadRevision(mapId, revisionId)
  }

  async function saveDraft(mapId: string, nodes: Array<{ node_key: string; node_type: string; display_name: string; config_json?: Record<string, unknown>; pipeline_id?: string | null; connector_instance_id?: string | null; owner_team_id?: string | null; eval_id?: string | null; position_x: string | number; position_y: string | number }>, relationships: Array<{ source_node_key: string; target_node_key: string; relationship_type: string; config_json?: Record<string, unknown> }>, notes: string): Promise<SystemMapRevision> {
    const response = await api.POST('/api/v1/system-maps/{map_id}/revisions', { params: { path: { map_id: mapId } }, body: { nodes, relationships, loop_configs: [], notes } })
    if (response.error || !response.data) throw new Error(tr('systemMaps.errors.save_draft_revision'))
    activeRevision.value = response.data
    revisions.value = [...revisions.value, response.data]
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

  async function restoreRevisionAsNew(mapId: string, revisionId: string): Promise<SystemMapRevision> {
    const response = await api.POST('/api/v1/system-maps/{map_id}/revisions/{rev_id}/restore-as-new', {
      params: { path: { map_id: mapId, rev_id: revisionId } },
      body: { notes: tr('systemMaps.restore_notes') },
    })
    if (response.error || !response.data) throw new Error(tr('systemMaps.errors.restore_revision'))
    const payload = response.data as SystemMapRevisionRestoreAsNewResponse
    await loadRevisionHistory(mapId)
    await loadRevision(mapId, payload.new_revision.id)
    return payload.new_revision
  }

  async function diffRevision(mapId: string, revisionId: string, againstRevisionId?: string): Promise<SystemMapRevisionDiff> {
    const response = await api.POST('/api/v1/system-maps/{map_id}/revisions/{rev_id}/diff', {
      params: { path: { map_id: mapId, rev_id: revisionId } },
      body: againstRevisionId ? { against_revision_id: againstRevisionId } : {},
    })
    if (response.error || !response.data) throw new Error(tr('systemMaps.errors.diff_revision'))
    return response.data as SystemMapRevisionDiff
  }

  async function loadAttributionBindings(mapId: string): Promise<void> {
    const response = await api.GET('/api/v1/system-maps/{map_id}/attribution-bindings', { params: { path: { map_id: mapId } } })
    if (response.error || !response.data) throw new Error(apiErrorMessage(response.error, tr('systemMaps.errors.load_attribution_bindings')))
    attributionBindings.value = response.data as AttributionBinding[]
  }

  async function createAttributionBinding(mapId: string, binding: AttributionBindingCreate): Promise<AttributionBinding> {
    const response = await api.POST('/api/v1/system-maps/{map_id}/attribution-bindings', { params: { path: { map_id: mapId } }, body: binding })
    if (response.error || !response.data) throw new Error(apiErrorMessage(response.error, tr('systemMaps.errors.create_attribution_binding')))
    const created = response.data as AttributionBinding
    attributionBindings.value = [...attributionBindings.value, created]
    return created
  }

  async function deleteAttributionBinding(mapId: string, bindingId: string): Promise<void> {
    const response = await api.DELETE('/api/v1/system-maps/{map_id}/attribution-bindings/{binding_id}', { params: { path: { map_id: mapId, binding_id: bindingId } } })
    if (response.error) throw new Error(apiErrorMessage(response.error, tr('systemMaps.errors.delete_attribution_binding')))
    attributionBindings.value = attributionBindings.value.filter((binding) => binding.id !== bindingId)
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

  function clearActive(): void { activeMap.value = null; activeRevision.value = null; graph.value = null; discovery.value = null; attributionBindings.value = [] }

  // Kept for the dashboard's preview card; discovery is now a real operation.
  function fetchDiscovery(): void { loaded.value = true }

  return { loaded, loading, error, unavailable, maps, total, page, pageSize, activeMap, activeRevision, graph, revisions, discovery, attributionBindings, overview, overviewLoading, overviewError, overviewUnavailable, overviewEmpty, accessLoaded, systemMapsEnabled, hasAccess, hasReadAccess, reset, fetchAccess, fetchOverview, fetchDefaultOverview, fetchMaps, createMap, deleteMap, loadMap, loadRevision, loadRevisionHistory, loadCurrentRevision, loadRevisionByTime, loadAttributionBindings, createAttributionBinding, deleteAttributionBinding, saveDraft, publish, archive, restoreRevisionAsNew, diffRevision, discover, clearActive, fetchDiscovery }
})
