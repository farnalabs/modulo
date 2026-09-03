import { type FullConfig, chromium } from '@playwright/test'
import { getTarget, getBaseUrl, getTestEnv } from './env'
import { seedTargetEnvironment } from './seeder'

// A deploy rollout always restarts machines, so the public endpoint can take a
// couple of minutes to settle before /healthz answers. A single 10s attempt
// hard-aborted a deploy whose target was healthy moments later, so the probe
// polls across a settling window instead of failing on the first attempt.
const HEALTH_POLL_INTERVAL_MS = 10_000
const HEALTH_WINDOW_MS = 300_000

async function waitForTargetHealth(target: string, baseURL: string, healthUrl: string) {
  const start = Date.now()
  const deadline = start + HEALTH_WINDOW_MS
  let attempts = 0
  let lastMessage = 'no attempts completed'

  while (Date.now() < deadline) {
    attempts += 1
    try {
      const response = await fetch(healthUrl, { signal: AbortSignal.timeout(10000) })
      if (response.ok) {
        const elapsedSeconds = Math.round((Date.now() - start) / 1000)
        process.stdout.write(`[global-setup] Health check passed after ${attempts} attempt(s), ${elapsedSeconds}s.\n`)
        return
      }
      lastMessage = `Health check returned ${response.status}: ${response.statusText}`
    } catch (err) {
      lastMessage = err instanceof Error ? err.message : String(err)
    }
    if (Date.now() + HEALTH_POLL_INTERVAL_MS >= deadline) break
    await new Promise((resolve) => setTimeout(resolve, HEALTH_POLL_INTERVAL_MS))
  }

  throw new Error(
    `Target "${target}" (${baseURL}) is not reachable. Health check at ${healthUrl} failed after ${attempts} attempts over ${HEALTH_WINDOW_MS / 1000}s: ${lastMessage}`,
  )
}

async function globalSetup(_config: FullConfig) {
  const target = getTarget()
  const baseURL = getBaseUrl(target)

  if (target === 'local') return

  const healthUrl = `${baseURL.replace(/\/+$/, '')}/healthz`

  await waitForTargetHealth(target, baseURL, healthUrl)

  const env = getTestEnv()
  process.stdout.write(`[global-setup] Seeding data for ${target}...\n`)
  const ctx = await seedTargetEnvironment(env)
  process.stdout.write(`[global-setup] Seeder result: pipelineId=${ctx.pipelineId}, pipelineName=${ctx.pipelineName}\n`)

  process.stdout.write(`[global-setup] Logging in once for ${target}...\n`)
  const browser = await chromium.launch()
  const page = await browser.newPage()

  await page.goto(baseURL + '/login')
  await page.waitForSelector('button[type="submit"]', { timeout: 30000 })
  await page.fill(env.credentials.loginFormEmailSelector, env.credentials.admin.email)
  await page.fill(env.credentials.loginFormPasswordSelector, env.credentials.admin.password)
  await page.click('button[type="submit"]')
  await page.waitForURL(/^(?!.*\/login).*$/, { timeout: 60000 })

  await page.evaluate(() => {
    localStorage.setItem('remy-panel-state', 'closed')
    localStorage.removeItem('remy-panel-position')
    localStorage.removeItem('remy-panel-size')
  })

  await page.context().storageState({ path: 'storageState-staging.json' })
  await browser.close()
  process.stdout.write('[global-setup] Login complete, storageState saved.\n')
}

export default globalSetup
