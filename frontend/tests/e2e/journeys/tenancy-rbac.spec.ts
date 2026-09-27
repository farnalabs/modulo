import { test, expect, loginAsAdmin, loginThroughUi } from '../setup/fixtures'
import {
  apiBaseFor,
  apiFetch,
  apiLogin,
  createJourneyUser,
  createPipeline,
  deactivateJourneyUser,
  uniqueName,
  type JourneyUser,
} from '../setup/realstack-api'
import type { BrowserContext, Page } from '@playwright/test'
import type { TestEnv } from '../setup/env'

/**
 * Real-stack RBAC/tenancy journeys (FAR-1242 batch 2).
 *
 * Runs against the REAL backend (staging/app); skips the local target. These
 * journeys prove the org-role boundary with a REAL second principal: a viewer
 * user is minted through the admin API, signs in through the real login UI
 * (rotating the forced first password), and then attempts mutations the
 * viewer role must refuse. No mock replaces the authz gate under test.
 *
 * Everything is self-cleaning: the viewer account is deactivated (per-org
 * membership tombstone) and the pipeline deleted in a finally block.
 */

/** Build a TestEnv-like object whose credentials carry the viewer's identity. */
function viewerEnv(env: TestEnv, email: string, password: string): TestEnv {
  return {
    ...env,
    credentials: {
      ...env.credentials,
      admin: { email, password },
    },
  }
}

/**
 * Rotate the forced first password through the full-screen gate, then sign in
 * again, returning the principal's access token minted by the real backend.
 */
async function rotateForcedPasswordAndSignIn(
  page: Page,
  env: TestEnv,
  initialPassword: string,
  newPassword: string,
): Promise<string> {
  await loginThroughUi(page, env)

  // The forced-rotation gate replaces the whole app surface for an
  // admin-minted credential. Prove the gate actually rendered.
  await expect(page.getByRole('heading', { name: /set a new password/i })).toBeVisible({ timeout: 30_000 })
  await page.getByTestId('change-password-current').fill(initialPassword)
  await page.getByTestId('change-password-new').fill(newPassword)
  await page.getByTestId('change-password-confirm').fill(newPassword)
  await page.getByTestId('change-password-submit').click()

  // The gate announces success, ends the rotated session, and routes to /login.
  await expect(page.getByText(/password changed/i)).toBeVisible({ timeout: 30_000 })
  await expect(page).toHaveURL(/\/login/, { timeout: 30_000 })

  // Sign in with the NEW credential and wait for the authenticated app.
  await loginThroughUi(page, viewerEnv(env, env.credentials.admin.email, newPassword))

  // The SPA persists the session token before routing; read it defensively.
  let token: string | null = null
  for (let attempt = 0; attempt < 10 && !token; attempt += 1) {
    token = await page.evaluate(() => localStorage.getItem('modulo_access_token'))
    if (!token) await new Promise((resolve) => setTimeout(resolve, 500))
  }
  if (!token) throw new Error('[realstack] viewer session did not mint an access token')
  return token
}

test.describe('Real-stack journeys: RBAC/tenancy boundary', { tag: '@regression' }, () => {
  test.beforeEach(async ({ page, env }) => {
    test.skip(env.name === 'local', 'Real-stack journey — run with E2E_TARGET=staging (no API mocks here)')
    await page.addInitScript(() => {
      localStorage.setItem('assistant-panel-state', 'closed')
    })
  })

  test('a viewer can read org pipelines but the backend refuses every mutation', { tag: '@regression' }, async ({ page, env }) => {
    const apiBase = apiBaseFor(env)
    const adminToken = await apiLogin(env)
    const initialPassword = `E2e-${crypto.randomUUID().slice(0, 8)}#x`
    const rotatedPassword = `Rotate-${crypto.randomUUID().slice(0, 8)}#x`
    const viewerEmail = `e2e-viewer-${crypto.randomUUID().slice(0, 12)}@journey.invalid`

    const pipelineName = uniqueName('E2E Journey RBAC Pipeline')
    const pipeline = await createPipeline(apiBase, adminToken, pipelineName)
    let viewer: JourneyUser | null = null
    let viewerContext: BrowserContext | null = null
    try {
      viewer = await createJourneyUser(apiBase, adminToken, {
        email: viewerEmail,
        display_name: 'E2E RBAC Viewer',
        password: initialPassword,
        org_role: 'viewer',
      })

      // The viewer signs in through the REAL login UI in a clean context
      // (never inheriting the admin session), rotating the forced credential.
      const browser = page.context().browser()
      if (!browser) throw new Error('[realstack] no browser instance available for a second context')
      viewerContext = await browser.newContext()
      const viewerPage = await viewerContext.newPage()
      const token = await rotateForcedPasswordAndSignIn(
        viewerPage,
        viewerEnv(env, viewerEmail, initialPassword),
        initialPassword,
        rotatedPassword,
      )

      // Can read: the viewer lists org pipelines and sees ours.
      const listRes = await apiFetch<{ items: Array<{ id: string }> }>(apiBase, token, 'GET', '/api/v1/pipelines?page_size=100')
      expect(listRes.status).toBe(200)
      expect(listRes.body?.items.some((p) => p.id === pipeline.id)).toBe(true)

      // Cannot create: pipeline.create requires operator.
      const createRes = await apiFetch(apiBase, token, 'POST', '/api/v1/pipelines', { name: uniqueName('E2E Hijack') })
      expect(createRes.status).toBe(403)
      expect(createRes.text).toContain("'pipeline.create'")

      // Cannot update: pipeline.update requires operator.
      const patchRes = await apiFetch(apiBase, token, 'PATCH', `/api/v1/pipelines/${pipeline.id}`, {
        name: uniqueName('E2E Hijack Rename'),
      })
      expect(patchRes.status).toBe(403)
      expect(patchRes.text).toContain("'pipeline.update'")

      // Cannot delete: pipeline.delete requires operator.
      const deleteRes = await apiFetch(apiBase, token, 'DELETE', `/api/v1/pipelines/${pipeline.id}`)
      expect(deleteRes.status).toBe(403)
      expect(deleteRes.text).toContain("'pipeline.delete'")

      // The refused mutations changed nothing: ours is untouched.
      const afterRes = await apiFetch<{ name: string }>(apiBase, adminToken, 'GET', `/api/v1/pipelines/${pipeline.id}`)
      expect(afterRes.status).toBe(200)
      expect(afterRes.body?.name).toBe(pipelineName)

      await viewerContext.close()
      viewerContext = null
    } finally {
      if (viewerContext) await viewerContext.close().catch(() => {})
      await deactivateJourneyUser(apiBase, adminToken, viewer?.id ?? '')
      await deletePipeline(apiBase, adminToken, pipeline.id)
    }
  })

  test('an unauthenticated request to a tenant endpoint is rejected with 401', { tag: '@regression' }, async ({ env }) => {
    const apiBase = apiBaseFor(env)
    // No Authorization header at all: the tenant gate must 401 (authentication
    // boundary), never fold to a permission-denied 403 or a silent success.
    const createRes = await fetch(`${apiBase}/api/v1/pipelines`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: uniqueName('E2E Unauth') }),
      signal: AbortSignal.timeout(10_000),
    })
    expect(createRes.status).toBe(401)

    const meRes = await fetch(`${apiBase}/api/v1/me`, { signal: AbortSignal.timeout(10_000) })
    expect(meRes.status).toBe(401)
  })
})
