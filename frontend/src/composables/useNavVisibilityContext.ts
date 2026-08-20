import { computed } from 'vue'
import { usePlanStore } from '../stores/planStore'
import { getAccessToken } from '../lib/api/client'
import { decodeJwtPayload } from '../lib/jwt'
import type { NavVisibilityContext } from '../config/navigation'

/**
 * Builds the NavVisibilityContext used to gate navigation/search items,
 * sourced from the decoded access token (role, system-admin flag,
 * permissions) and the plan store (dev mode, tier info). This is the single
 * construction point so every consumer gates identically.
 */
export function useNavVisibilityContext() {
  const planStore = usePlanStore()
  const jwtPayload = computed(() =>
    decodeJwtPayload(getAccessToken()) as Record<string, unknown> | null,
  )
  return computed<NavVisibilityContext>(() => ({
    isSystemAdmin: jwtPayload.value?.is_system_admin === true,
    userRole: (jwtPayload.value?.org_role as string | null) || null,
    userPermissions: Array.isArray(jwtPayload.value?.permissions)
      ? (jwtPayload.value!.permissions as string[])
      : [],
    devMode: planStore.devMode,
    tierInfoLoaded: !!planStore.tierRanks && Object.keys(planStore.tierRanks).length > 0,
    isAtMinimumTier: (tier: string) => planStore.isAtMinimumTier(tier),
  }))
}
