import type { FullConfig } from '@playwright/test'

const BASE_URLS: Record<string, string> = {
  local: 'http://127.0.0.1:5173',
  staging: 'https://staging.modulo.run',
  app: 'https://app.modulo.run',
}

async function globalSetup(config: FullConfig) {
  const target = (process.env.E2E_TARGET || 'local').toLowerCase()
  const baseURL = process.env.E2E_BASE_URL || BASE_URLS[target] || BASE_URLS.local

  if (target === 'local') {
    return
  }

  const healthUrl = `${baseURL.replace(/\/+$/, '')}/healthz/ready`
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), 10000)

  try {
    const response = await fetch(healthUrl, { signal: controller.signal })
    if (!response.ok) {
      throw new Error(`Health check returned ${response.status}: ${response.statusText}`)
    }
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err)
    throw new Error(
      `Target "${target}" (${baseURL}) is not reachable. Health check at ${healthUrl} failed: ${message}`,
    )
  } finally {
    clearTimeout(timeout)
  }
}

export default globalSetup
