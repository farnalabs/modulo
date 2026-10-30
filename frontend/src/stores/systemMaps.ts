import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { api, getAccessToken } from '../lib/api/client'
import { decodeJwtPayload } from '../lib/jwt'
import { withTimeout } from '../lib/asyncUtils'
import { useCurrentUser } from '../composables/useCurrentUser'
import { usePlanStore } from './planStore'

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
  let accessPrincipalSub: string | null = null

  const planStore = usePlanStore()
  const { isOperator } = useCurrentUser()
  const hasSystemMapsAccess = computed(() =>
    planStore.userPermissions.includes('system_maps_access'),
  )
  const hasDraft = computed(() => draft.value !== null)

  function currentPrincipalSub(): string | null {
    const token = getAccessToken()
    if (!token) return null
    const sub = decodeJwtPayload(token)?.sub
    return typeof sub === 'string' && sub.length > 0 ? sub : null
  }

  function reset(): void {
    loaded.value = false
    loading.value = false
    error.value = null
    discoveryAvailable.value = false
    draft.value = null
    accessLoaded.value = false
    systemMapsEnabled.value = false
    accessPromise = null
    accessPrincipalSub = currentPrincipalSub()
  }

  /**
   * Resolve the internal flag only for an already-authorized internal caller.
   * Ordinary users never call the instance feature-flag endpoint. A failed or
   * unexpected response settles to disabled and never reuses a stale value.
   */
  async function fetchAccess(): Promise<void> {
    const principalSub = currentPrincipalSub()
    if (principalSub !== accessPrincipalSub) reset()
    accessPrincipalSub = principalSub
    if (accessLoaded.value) return
    if (accessPromise) return accessPromise

    accessPromise = resolveAccess()
    try {
      await accessPromise
    } finally {
      accessPromise = null
    }
  }

  async function resolveAccess(): Promise<void> {
    loading.value = true
    error.value = null
    systemMapsEnabled.value = false
    try {
      // The role is available locally from the access token. Do not even fetch
      // the permissions catalogue for ordinary users; the internal probe is
      // intentionally unreachable to them.
      if (!isOperator.value) return
      const planRequest =
        planStore.loaded || planStore.currentTier !== 'community'
          ? Promise.resolve()
          : planStore.fetchPlan()
      await Promise.all([planRequest, planStore.fetchPermissions()])
      if (!planStore.devMode || !hasSystemMapsAccess.value) return

      const response = await withTimeout(
        api.GET('/api/v1/admin/feature-flags'),
        15000,
        'Internal feature flags request',
      )
      const flags = (response.data as InternalFeatureFlagsPayload | undefined)?.flags
      systemMapsEnabled.value =
        Array.isArray(flags) &&
        flags.some((flag) => flag.name === 'system_maps' && flag.currently_active === true)
    } catch (err: unknown) {
      error.value = err instanceof Error ? err.message : 'Internal feature flags request failed'
      systemMapsEnabled.value = false
    } finally {
      accessLoaded.value = true
      loading.value = false
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
