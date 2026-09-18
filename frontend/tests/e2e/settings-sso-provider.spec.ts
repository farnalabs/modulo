import { test, expect, loginAsAdmin } from './setup/fixtures'

/**
 * SSO provider config flow e2e.
 *
 * Staging safety:
 * 1. The provider is created via the UI (which sends enabled:true), but
 *    immediately toggled OFF via the API the moment we have its ID — before
 *    any domain-verification assertions. This keeps the enabled window to
 *    the absolute minimum (a single API round-trip after creation).
 * 2. test.afterEach DELETEs the provider regardless of test outcome.
 *    Even if cleanup fails, the provider is disabled so it does not break
 *    unrelated login-page specs.
 */

const PROVIDER_NAME = `E2E SSO ${Date.now()}`

let createdProviderId: string | null = null

test.afterEach(async ({ page, env }) => {
  // Guarantee cleanup even on failure — delete the provider we created.
  if (!createdProviderId || env.name === 'local') return
  try {
    const token = await page.evaluate(() => localStorage.getItem('modulo_access_token') ?? '')
    const base = page.url().replace(/\/settings\/sso$/, '').replace(/\/$/, '')
    await page.request.delete(`${base}/api/v1/admin/sso/providers/${createdProviderId}`, {
      headers: { Authorization: `Bearer ${token}` },
    })
  } catch {
    // Best-effort cleanup — provider is disabled so login page is unaffected.
    void 0
  }
  createdProviderId = null
})

test.describe('Settings SSO Provider Config', { tag: '@regression' }, () => {
  test('creates provider with domain allowlist — domain persists without Enter', { tag: '@regression' }, async ({ page, env }) => {
    // Skip on local — needs the real backend to create SSO providers
    if (env.name === 'local') {
      test.skip()
      return
    }

    await loginAsAdmin(page, env)
    await page.goto('/settings/sso')
    await expect(page.locator('h1')).toContainText(/SSO|Single Sign/i)

    // ── Open add-provider form ──
    await page.getByTestId('settings-sso-add-provider').click()
    const formHeading = page.locator('h2').filter({ hasText: /New|Add Provider|SSO Provider/i })
    await expect(formHeading).toBeVisible()

    // ── Fill form ──
    const nameInput = page.locator('#ssoproviderform-field-9')
    await expect(nameInput).toBeVisible()
    await nameInput.fill(PROVIDER_NAME)

    // Select Google preset (if visible — native presets may not load)
    const googlePreset = page.getByTestId('sso-preset-google')
    if (await googlePreset.isVisible().catch(() => false)) {
      await googlePreset.click()
    }

    // Client ID + Secret
    await page.locator('#ssoproviderform-field-8').fill('e2e-test-client-id')
    await page.locator('#ssoproviderform-field-7').fill('e2e-test-secret-value')

    // ── Select "Email domain allowlist" provisioning mode ──
    await page.getByTestId('sso-mode-domains').click()
    const domainInput = page.getByTestId('sso-domain-input')
    await expect(domainInput).toBeVisible()

    // Type a domain but do NOT press Enter — this is the #737 regression
    await domainInput.fill('e2e-test.example.com')

    // ── Submit WITHOUT pressing Enter in the domain field ──
    const submitBtn = page.locator('button').filter({ hasText: /Create|Save/ }).first()
    await submitBtn.click()

    // ── Wait for form to close (provider created, back on list) ──
    await expect(page.getByTestId('settings-sso-add-provider')).toBeVisible({ timeout: 15000 })

    // ── Staging safety: fetch provider + disable IMMEDIATELY ──
    // The UI creates with enabled:true; we disable it the moment we have the
    // ID so the login page never surfaces this provider for concurrent specs.
    const token = await page.evaluate(() => localStorage.getItem('modulo_access_token') ?? '')
    const base = page.url().replace(/\/settings\/sso$/, '').replace(/\/$/, '')
    const listResp = await page.request.get(`${base}/api/v1/admin/sso/providers`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    const listBody = await listResp.json()
    const providers = Array.isArray(listBody) ? listBody : (listBody?.items ?? [])
    const created = providers.find((p: any) => p.name === PROVIDER_NAME)
    expect(created).toBeTruthy()
    createdProviderId = created!.id

    if (created!.enabled) {
      await page.request.put(`${base}/api/v1/admin/sso/providers/${createdProviderId}/toggle`, {
        headers: { Authorization: `Bearer ${token}` },
      })
    }

    // ── Verify provider row visible (now disabled — safe for login page) ──
    const row = page.getByTestId('settings-sso-provider-row').filter({ hasText: PROVIDER_NAME })
    await expect(row).toBeVisible({ timeout: 15000 })

    // ── Verify domain persisted (the #737 regression) ──
    // Open the provider's edit form by clicking the row
    await row.click()
    const domainList = page.getByTestId('sso-domain-list')
    await expect(domainList).toContainText('e2e-test.example.com')
  })
})
