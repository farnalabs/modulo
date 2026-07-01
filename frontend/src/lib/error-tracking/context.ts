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
      ctx.tier = plan.currentTier
      ctx.orgName = plan.orgName
    }
  } catch {
    // Pinia not yet initialized
  }

  ctx.hasAuth = !!getAccessToken()

  return ctx
}
