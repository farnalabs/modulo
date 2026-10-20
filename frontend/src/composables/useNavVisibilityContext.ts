import { computed, toValue } from 'vue'
import type { MaybeRefOrGetter } from 'vue'
import { usePlanStore } from '../stores/planStore'
import { isDemoSession } from '../lib/api/client'
import { useCurrentUser } from './useCurrentUser'
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
 *
 * FAR-2020: an EMPTY permissions list is not an override. Callers used to hand
 * this context a JWT-derived array (the token mints no `permissions` claim, so
 * it was always `[]`), and `[] ?? x` is `[]` — which silently discarded the
 * endpoint-resolved list for every sidebar variant. Emptiness therefore means
 * "nothing to say", and the plan store's list wins; only a NON-EMPTY override
 * (or non-empty JWT claim) takes precedence.
 */
export function useNavVisibilityContext(
  overrides?: MaybeRefOrGetter<NavVisibilityOverrides | undefined>,
) {
  const planStore = usePlanStore()
  const { jwtPayload } = useCurrentUser()
  // FAR-2020: kick off the caller's permission read ONCE per session. The
  // access token carries no `permissions` claim, and `required_permissions`
  // gates would otherwise deny for every user (empty list). Deliberately
  // fire-and-forget: the store dedupes the request, swallows failures and
  // leaves the list empty (fail closed), so building a nav context never
  // blocks a render and never rejects.
  void planStore.fetchPermissions()
  return computed<NavVisibilityContext>(() => {
    const o = toValue(overrides)
    const claimPermissions = jwtPayload.value?.permissions
    const overridePermissions = o?.userPermissions
    // Precedence: non-empty override > non-empty token claim > store list.
    // An empty array at either higher layer means "no opinion" — never "deny
    // everything", which is the store's terminal fail-closed state, not an
    // override's job.
    let userPermissions: string[]
    if (overridePermissions && overridePermissions.length > 0) {
      userPermissions = overridePermissions
    } else if (Array.isArray(claimPermissions) && claimPermissions.length > 0) {
      userPermissions = claimPermissions as string[]
    } else {
      userPermissions = planStore.userPermissions
    }
    return {
      isSystemAdmin: o?.isSystemAdmin ?? (jwtPayload.value?.is_system_admin === true),
      userRole:
        o?.userRole !== undefined
          ? o.userRole || null
          : (jwtPayload.value?.org_role as string | null) || null,
      userPermissions,
      devMode: planStore.devMode,
      tierInfoLoaded: !!planStore.tierRanks && Object.keys(planStore.tierRanks).length > 0,
      isAtMinimumTier: (tier: string) => planStore.isAtMinimumTier(tier),
      // FAR-656: flag-gated nav items resolve through the plan store so the
      // sidebar stays consistent with the manifest feature_flag router guard.
      isFeatureEnabled: (flag: string) => planStore.featureEnabled(flag),
      // FAR-535: read once per context evaluation, not per nav item.
      isDemoSession: isDemoSession(),
    }
  })
}
