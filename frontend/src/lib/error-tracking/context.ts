import { getActivePinia } from 'pinia'
import { usePlanStore } from '../../stores/planStore'
import { getAccessToken } from '../api/client'

export function gatherContext(): Record<string, unknown> {
  const ctx: Record<string, unknown> = {
    url: window.location.href,
    viewport: {
      width: window.innerWidth,
      height: window.innerHeight,
    },
    userAgent: navigator.userAgent,
  }

  try {
    const pinia = getActivePinia()
    if (pinia) {
      const plan = usePlanStore()
      if (plan.currentTier) ctx.tier = plan.currentTier
      if (plan.orgId) ctx.orgName = plan.orgId
    }
  } catch (e) {
    // Pinia not yet initialized — context gathering is best-effort
    console.warn('[error-tracking] pinia not initialized during context gather', e)
  }

  ctx.hasAuth = !!getAccessToken()

  return ctx
}
