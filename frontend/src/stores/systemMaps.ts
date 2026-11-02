import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { api, getAccessToken } from '../lib/api/client'
import { withTimeout } from '../lib/asyncUtils'
import { withTimeout as withRequestDeadline } from '../lib/requestTimeout'
import { useCurrentUser } from '../composables/useCurrentUser'
import { usePlanStore } from './planStore'
import { SYSTEM_CONFIG_MANAGE_PERMISSION } from '../composables/useLicenceAccess'
import type { components } from '../lib/api/schema'

type SystemMapOverview = components['schemas']['SystemMapOverviewResponse']
type SystemMapList = components['schemas']['SystemMapListResponse']

interface InternalFeatureFlagsPayload {
  flags?: Array<{ name: string; currently_active: boolean }>
}

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

/**
 * System Maps is intentionally fail-closed while its discovery/review API is
 * not part of the current generated contract. The internal flag probe is
 * separate from the ordinary plan payload because the backend deliberately
 * omits this flag from ordinary callers.
 */
export const useSystemMapsStore = defineStore('systemMaps', () => {
  const loaded = ref(false)
  const loading = ref(false)
  const error = ref<string | null>(null)
  const discoveryAvailable = ref(false)
  const draft = ref<null>(null)
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
  // A subject is stable across token refreshes, including refreshes that carry
  // demoted roles or capabilities. Cache against the complete token value so
  // every access-token change revalidates the authorization decision.
  let accessTokenKey: string | null = null

  const planStore = usePlanStore()
  const { isOperator, isSystemAdmin } = useCurrentUser()
  const hasSystemMapsAccess = computed(() =>
    planStore.userPermissions.includes('system_maps_access'),
  )
  const canProbeInternalFlags = computed(
    () => planStore.userPermissions.includes(SYSTEM_CONFIG_MANAGE_PERMISSION),
  )
  const hasDraft = computed(() => draft.value !== null)

  function reset(): void {
    accessGeneration += 1
    overviewGeneration += 1
    loaded.value = false
    loading.value = false
    error.value = null
    discoveryAvailable.value = false
    draft.value = null
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

  /**
   * Resolve the internal flag only for an already-authorized internal caller.
   * Ordinary users never call the instance feature-flag endpoint. A failed or
   * unexpected response settles to disabled and never reuses a stale value.
   */
  async function fetchAccess(): Promise<void> {
    const currentAccessToken = getAccessToken()
    if (currentAccessToken !== accessTokenKey) reset()
    accessTokenKey = currentAccessToken
    if (accessLoaded.value) return
    if (accessPromise) return accessPromise

    const requestGeneration = accessGeneration
    const requestToken = currentAccessToken
    const requestPromise = resolveAccess(requestGeneration, requestToken)
    accessPromise = requestPromise
    try {
      await requestPromise
    } finally {
      if (accessPromise === requestPromise) accessPromise = null
    }
  }

  async function resolveAccess(requestGeneration: number, requestToken: string | null): Promise<void> {
    const isCurrent = (): boolean => isCurrentAccessRequest(requestGeneration, requestToken)
    loading.value = true
    error.value = null
    systemMapsEnabled.value = false
    try {
      // The role is available locally from the access token. System admins are
      // instance-scoped and may have no org_role at all, so their capability
      // must be accepted independently of the org operator role.
      if (!isOperator.value && !isSystemAdmin.value) return
      const planRequest =
        planStore.loaded || planStore.currentTier !== 'community'
          ? Promise.resolve()
          : planStore.fetchPlan()
      await Promise.all([planRequest, planStore.fetchPermissions()])
      if (!planStore.devMode || !hasSystemMapsAccess.value || !canProbeInternalFlags.value) return

      const response = await withTimeout(
        api.GET('/api/v1/admin/feature-flags'),
        15000,
        'Internal feature flags request',
      )
      const flags = (response.data as InternalFeatureFlagsPayload | undefined)?.flags
      if (isCurrent()) {
        systemMapsEnabled.value =
          Array.isArray(flags) &&
          flags.some((flag) => flag.name === 'system_maps' && flag.currently_active === true)
      }
    } catch (err: unknown) {
      if (isCurrent()) {
        error.value = err instanceof Error ? err.message : 'Internal feature flags request failed'
        systemMapsEnabled.value = false
      }
    } finally {
      if (isCurrent()) {
        accessLoaded.value = true
        loading.value = false
      }
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
      let page = 1
      let total = Number.POSITIVE_INFINITY

      while (publishedItems.length < total && page <= MAX_SYSTEM_MAP_PAGES) {
        const response = await withRequestDeadline(
          (signal) =>
            api.GET('/api/v1/system-maps', {
              params: { query: { page, page_size: SYSTEM_MAP_PAGE_SIZE } },
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
        total = pageData.total
        if (pageData.items.length === 0 || pageData.items.length < pageData.page_size) break
        page += 1
      }

      if (page > MAX_SYSTEM_MAP_PAGES) {
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

  /** No request is made until the discovery endpoint exists in schema.ts. */
  async function fetchDiscovery(): Promise<void> {
    loaded.value = true
    discoveryAvailable.value = false
    draft.value = null
  }

  return {
    loaded,
    loading,
    error,
    discoveryAvailable,
    draft,
    overview,
    overviewLoading,
    overviewError,
    overviewEmpty,
    hasDraft,
    accessLoaded,
    systemMapsEnabled,
    reset,
    fetchAccess,
    fetchOverview,
    fetchDefaultOverview,
    fetchDiscovery,
  }
})
