import { computed } from 'vue'
import { usePlanStore } from '../stores/planStore'

/**
 * The permission `PATCH /api/v1/errors/{error_id}` enforces (FAR-2373): every
 * error-record write — a status transition and a reassignment alike — is
 * admin-only, while the reads stay `errors.resolve` (viewer).
 *
 * This is the single source of the error detail screen's write capability, so
 * the disabled controls and the assignee picker cannot drift from the endpoint
 * they call (the same shape as `useLicenceAccess`).
 */
export const ERROR_MANAGE_PERMISSION = 'errors.manage'

/**
 * Error-record administration capability, derived from the caller-scoped
 * permission list (`GET /api/v1/auth/permissions`) rather than from a
 * hand-maintained role check.
 *
 * The list is resolved server-side from the permission registry — the same
 * registry `require_permission` compares against — so "can this principal
 * change an error record" has exactly one definition. The store dedupes the
 * request, and leaves the list empty on failure (fail closed): a pending or
 * failed permissions read denies the write controls rather than flashing them
 * enabled and 403-ing on click.
 */
export function useErrorAdminActions() {
  const planStore = usePlanStore()
  void planStore.fetchPermissions()

  const canManageErrors = computed(() =>
    planStore.userPermissions.includes(ERROR_MANAGE_PERMISSION),
  )

  return { canManageErrors }
}
