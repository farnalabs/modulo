import { type FullConfig, chromium } from '@playwright/test'
import { getTarget, getBaseUrl, getTestEnv } from './env'
import { seedTargetEnvironment } from './seeder'

async function globalSetup(_config: FullConfig) {
  const target = getTarget()
  const baseURL = getBaseUrl(target)

  if (target === 'local') return

  const healthUrl = `${baseURL.replace(/\/+$/, '')}/healthz`

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
