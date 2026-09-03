import { computed, toValue } from 'vue'
import type { MaybeRefOrGetter } from 'vue'
import { usePlanStore } from '../stores/planStore'
import { getAccessToken, isDemoSession } from '../lib/api/client'
import { decodeJwtPayload } from '../lib/jwt'
import type { NavVisibilityContext } from '../config/navigation'

/**
 * Field overrides for callers whose role/admin values arrive as props rather
 * than from the decoded access token (SidebarNav receives them from
 * AppLayout). Omitted fields fall back to the JWT/plan-store defaults.
 */
export interface NavVisibilityOverrides {
  isSystemAdmin?: boolean
  userRole?: string | null
  userPermissions?: string[]
}

/**
 * Builds the NavVisibilityContext used to gate navigation/search items,
 * sourced from the decoded access token (role, system-admin flag,
 * permissions) and the plan store (dev mode, tier info). qa iter 2: this is
 * now genuinely the SINGLE construction point — SidebarNav consumes it with
 * prop overrides instead of hand-building a second, drifting context, so a
 * new field (e.g. isDemoSession) is added exactly once. Pass overrides as a
 * plain object or a getter; a getter keeps prop overrides reactive.
 */
export function useNavVisibilityContext(
  overrides?: MaybeRefOrGetter<NavVisibilityOverrides | undefined>,
) {
  const planStore = usePlanStore()
  const jwtPayload = computed(() =>
    decodeJwtPayload(getAccessToken()) as Record<string, unknown> | null,
  )
  return computed<NavVisibilityContext>(() => {
    const o = toValue(overrides)
    return {
      isSystemAdmin: o?.isSystemAdmin ?? (jwtPayload.value?.is_system_admin === true),
      userRole:
        o?.userRole !== undefined
          ? o.userRole || null
          : (jwtPayload.value?.org_role as string | null) || null,
      userPermissions:
        o?.userPermissions ??
        (Array.isArray(jwtPayload.value?.permissions)
          ? (jwtPayload.value!.permissions as string[])
          : []),
      devMode: planStore.devMode,
      tierInfoLoaded: !!planStore.tierRanks && Object.keys(planStore.tierRanks).length > 0,
      isAtMinimumTier: (tier: string) => planStore.isAtMinimumTier(tier),
      // FAR-535: read once per context evaluation, not per nav item.
      isDemoSession: isDemoSession(),
    }
  })
}
