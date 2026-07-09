import type { FullConfig } from '@playwright/test'
import { getTarget, getBaseUrl } from './env'

async function globalSetup(config: FullConfig) {
  const target = getTarget()
  const baseURL = getBaseUrl(target)

  if (target === 'local') {
    return
  }

  const healthUrl = `${baseURL.replace(/\/+$/, '')}/healthz/ready`

  try {
    const response = await fetch(healthUrl, { signal: AbortSignal.timeout(10000) })
    if (!response.ok) {
      throw new Error(`Health check returned ${response.status}: ${response.statusText}`)
    }
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err)
    throw new Error(
      `Target "${target}" (${baseURL}) is not reachable. Health check at ${healthUrl} failed: ${message}`,
    )
  }
}

export default globalSetup
