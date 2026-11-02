import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { api, getAccessToken } from '../lib/api/client'
import type { components } from '../lib/api/schema'
import { withTimeout } from '../lib/asyncUtils'
import { useCurrentUser } from '../composables/useCurrentUser'
import { usePlanStore } from './planStore'
import { SYSTEM_CONFIG_MANAGE_PERMISSION } from '../composables/useLicenceAccess'

export type SystemMap = components['schemas']['SystemMapResponse']
export type SystemMapRevision = components['schemas']['SystemMapRevisionResponse']
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
  const revisions = ref<SystemMapRevision[]>([])
  const discovery = ref<DiscoveryReport | null>(null)
  const accessLoaded = ref(false)
  const systemMapsEnabled = ref(false)
  let accessPromise: Promise<void> | null = null
  let accessGeneration = 0
  let accessTokenKey: string | null = null

  const planStore = usePlanStore()
  const { isOperator, isSystemAdmin } = useCurrentUser()
  const hasSystemMapsAccess = computed(() => planStore.userPermissions.includes('system_maps_access'))
  const canProbeInternalFlags = computed(() => planStore.userPermissions.includes(SYSTEM_CONFIG_MANAGE_PERMISSION))
  const hasAccess = computed(() => systemMapsEnabled.value && (isOperator.value || isSystemAdmin.value) && hasSystemMapsAccess.value)

  function reset(): void {
    accessGeneration += 1
    loaded.value = false
    loading.value = false
    error.value = null
    maps.value = []
    activeMap.value = null
    activeRevision.value = null
    graph.value = null
    revisions.value = []
    discovery.value = null
    accessLoaded.value = false
    systemMapsEnabled.value = false
    accessPromise = null
    accessTokenKey = getAccessToken()
  }

  function isCurrentAccessRequest(generation: number, token: string | null): boolean {
    return generation === accessGeneration && token === getAccessToken() && token === accessTokenKey
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
      if (current()) error.value = err instanceof Error ? err.message : 'Internal feature flags request failed'
    } finally {
      if (current()) { accessLoaded.value = true; loading.value = false }
    }
  }

  async function fetchMaps(nextPage = page.value): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const response = await api.GET('/api/v1/system-maps', { params: { query: { page: nextPage, page_size: pageSize.value } } })
      if (response.error) throw new Error('Could not load system maps')
      const payload = response.data
      maps.value = payload?.items ?? []
      total.value = payload?.total ?? 0
      page.value = payload?.page ?? nextPage
      loaded.value = true
    } catch (err: unknown) { error.value = err instanceof Error ? err.message : 'Could not load system maps' }
    finally { loading.value = false }
  }

  async function createMap(name: string, description = ''): Promise<SystemMap | null> {
    const response = await api.POST('/api/v1/system-maps', { body: { name, description, visibility: 'org', default_map: false } })
    if (response.error || !response.data) throw new Error('Could not create system map')
    await fetchMaps(1)
    return response.data
  }

  async function deleteMap(mapId: string): Promise<void> {
    const response = await api.DELETE('/api/v1/system-maps/{map_id}', { params: { path: { map_id: mapId } } })
    if (response.error) throw new Error('Could not delete system map')
    if (activeMap.value?.id === mapId) clearActive()
    await fetchMaps()
  }

  async function loadMap(mapId: string): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const response = await api.GET('/api/v1/system-maps/{map_id}', { params: { path: { map_id: mapId } } })
      if (response.error || !response.data) throw new Error('Could not load system map')
      activeMap.value = response.data
      if (response.data.published_revision_id) await loadRevision(mapId, response.data.published_revision_id)
    } catch (err: unknown) { error.value = err instanceof Error ? err.message : 'Could not load system map' }
    finally { loading.value = false }
  }

  async function loadRevision(mapId: string, revisionId: string): Promise<void> {
    const response = await api.GET('/api/v1/system-maps/{map_id}/revisions/{rev_id}', { params: { path: { map_id: mapId, rev_id: revisionId }, query: { include_graph: true } } })
    if (response.error || !response.data) throw new Error('Could not load map revision')
    const value = response.data as SystemMapGraph
    activeRevision.value = 'revision' in value ? value.revision : value as SystemMapRevision
    graph.value = 'revision' in value ? value : null
    if (activeRevision.value && !revisions.value.some((revision) => revision.id === activeRevision.value?.id)) revisions.value = [...revisions.value, activeRevision.value]
  }

  async function saveDraft(mapId: string, nodes: Array<{ node_key: string; node_type: string; display_name: string; config_json?: Record<string, unknown>; pipeline_id?: string | null; connector_instance_id?: string | null; owner_team_id?: string | null; eval_id?: string | null; position_x: string | number; position_y: string | number }>, relationships: Array<{ source_node_key: string; target_node_key: string; relationship_type: string; config_json?: Record<string, unknown> }>, notes: string): Promise<SystemMapRevision> {
    const response = await api.POST('/api/v1/system-maps/{map_id}/revisions', { params: { path: { map_id: mapId } }, body: { nodes, relationships, loop_configs: [], notes } })
    if (response.error || !response.data) throw new Error('Could not save draft revision')
    activeRevision.value = response.data
    revisions.value = [...revisions.value, response.data]
    await loadRevision(mapId, response.data.id)
    return response.data
  }

  async function publish(mapId: string, revisionId: string): Promise<void> {
    const response = await api.POST('/api/v1/system-maps/{map_id}/revisions/{rev_id}/publish', { params: { path: { map_id: mapId, rev_id: revisionId } } })
    if (response.error) throw new Error('Could not publish revision')
    await loadMap(mapId)
  }

  async function archive(mapId: string, revisionId: string): Promise<void> {
    const response = await api.POST('/api/v1/system-maps/{map_id}/revisions/{rev_id}/archive', { params: { path: { map_id: mapId, rev_id: revisionId } } })
    if (response.error) throw new Error('Could not archive revision')
    await loadMap(mapId)
  }

  async function discover(name = 'Discovered System Map', description?: string): Promise<DiscoveryReport> {
    const response = await api.POST('/api/v1/system-maps/discover', { body: { name, description } })
    if (response.error || !response.data) throw new Error('Could not discover system map')
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
        error.value = err instanceof Error ? err.message : 'Could not load the discovered draft'
      }
    }
    return response.data
  }

  function clearActive(): void { activeMap.value = null; activeRevision.value = null; graph.value = null; discovery.value = null }

  // Kept for the dashboard's preview card; discovery is now a real operation.
  async function fetchDiscovery(): Promise<void> { loaded.value = true }

  return { loaded, loading, error, maps, total, page, pageSize, activeMap, activeRevision, graph, revisions, discovery, accessLoaded, systemMapsEnabled, hasAccess, reset, fetchAccess, fetchMaps, createMap, deleteMap, loadMap, loadRevision, saveDraft, publish, archive, discover, clearActive, fetchDiscovery }
})
