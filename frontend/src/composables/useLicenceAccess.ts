import { computed } from 'vue'
import { usePlanStore } from '../stores/planStore'

/**
 * The permission the licence READ enforces
 * (`GET /api/v1/admin/license`), and the one the org-scoped write endpoints
 * enforce (`PUT`/`DELETE /api/v1/admin/orgs/{org_id}/license`).
 *
 * This is the single source of the licence screen's audience. The router
 * (manifest `required_permissions`), the sidebar, the shell's renewal notice
 * and the licence page all derive from it, so a new surface cannot reintroduce
 * a parallel role check that drifts from the endpoint (FAR-2104).
 */
export const ORG_LICENSE_MANAGE_PERMISSION = 'org.license.manage'

/**
 * The strict system-admin permission on the system-wide licence routes
 * (`POST /api/v1/admin/license` and `/issue`, guarded by
 * `require_system_permission`). An org admin does not hold it, so the page
 * shows the instance-wide controls only when this resolves true.
 */
export const SYSTEM_CONFIG_MANAGE_PERMISSION = 'system.config.manage'

/**
 * Licence-administration capability, derived from the caller-scoped
 * permission list (`GET /api/v1/auth/permissions`) rather than from a
 * hand-maintained role check.
 *
 * The list is resolved server-side from the permission registry — the same
 * registry `require_permission` compares against — so "can this principal
 * manage the licence" has exactly one definition. The store dedupes the
 * request, and leaves the list empty on failure (fail closed).
 */
export function useLicenceAccess() {
  const planStore = usePlanStore()
  void planStore.fetchPermissions()

  const canManageOrgLicence = computed(() =>
    planStore.userPermissions.includes(ORG_LICENSE_MANAGE_PERMISSION),
  )
  const canManageSystemLicence = computed(() =>
    planStore.userPermissions.includes(SYSTEM_CONFIG_MANAGE_PERMISSION),
  )

  return { canManageOrgLicence, canManageSystemLicence }
}
