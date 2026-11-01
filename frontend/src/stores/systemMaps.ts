import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { api, getAccessToken } from '../lib/api/client'
import { withTimeout } from '../lib/asyncUtils'
import { useCurrentUser } from '../composables/useCurrentUser'
import { usePlanStore } from './planStore'
import { SYSTEM_CONFIG_MANAGE_PERMISSION } from '../composables/useLicenceAccess'

interface InternalFeatureFlagsPayload {
  flags?: Array<{ name: string; currently_active: boolean }>
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
  const accessLoaded = ref(false)
  const systemMapsEnabled = ref(false)
  let accessPromise: Promise<void> | null = null
  let accessGeneration = 0
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
    loaded.value = false
    loading.value = false
    error.value = null
    discoveryAvailable.value = false
    draft.value = null
    accessLoaded.value = false
    systemMapsEnabled.value = false
    accessPromise = null
    accessTokenKey = getAccessToken()
  }

  function isCurrentAccessRequest(generation: number, token: string | null): boolean {
    return generation === accessGeneration && token === getAccessToken() && token === accessTokenKey
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
    hasDraft,
    accessLoaded,
    systemMapsEnabled,
    reset,
    fetchAccess,
    fetchDiscovery,
  }
})
