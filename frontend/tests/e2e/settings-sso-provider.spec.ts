import { test, expect, loginAsAdmin } from './setup/fixtures'

/**
 * SSO provider config flow e2e.
 *
 * Staging safety: the test provider is seeded via the admin API with
 * `enabled=false` so it NEVER appears on the login page. This eliminates the
 * enabled-provider window entirely — no toggle-after-create needed.
 *
 * The #737 regression (domain not persisted when Submit is clicked without
 * pressing Enter) is exercised through the EDIT form: open the seeded
 * provider's edit form, switch to "Email domain allowlist" mode, type a
 * domain, and click Save without pressing Enter. The edit form uses the same
 * SsoProviderForm component and the same commitPendingDomain / onSubmitClick
 * logic as the create form, so this covers the regression.
 *
 * test.afterEach DELETEs the provider regardless of test outcome.
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
  test('seeded disabled provider persists domain via edit form without Enter', { tag: '@regression' }, async ({ page, env }) => {
    // Skip on local — needs the real backend to create SSO providers
    if (env.name === 'local') {
      test.skip()
      return
    }

    await loginAsAdmin(page, env)

    // ── Seed provider via API with enabled=false ──
    // This ensures the provider NEVER appears on the login page, eliminating
    // the enabled window that concurrent login-page specs would observe.
    const token = await page.evaluate(() => localStorage.getItem('modulo_access_token') ?? '')
    const base = page.url().replace(/\/settings\/sso$/, '').replace(/\/$/, '')
    const createResp = await page.request.post(`${base}/api/v1/admin/sso/providers`, {
      headers: { Authorization: `Bearer ${token}` },
      data: {
        provider_type: 'oidc',
        name: PROVIDER_NAME,
        client_id: 'e2e-test-client-id',
        client_secret: 'e2e-test-secret-value',
        enabled: false,
        auto_provision: false,
        default_role: 'runner',
        preset: 'custom',
        allowed_domains: [],
      },
    })
    expect(createResp.ok()).toBeTruthy()
    const created = await createResp.json()
    createdProviderId = created.id

    // ── Navigate to SSO settings and find the seeded provider ──
    await page.goto('/settings/sso')
    await expect(page.locator('h1')).toContainText(/SSO|Single Sign/i)
    const row = page.getByTestId('settings-sso-provider-row').filter({ hasText: PROVIDER_NAME })
    await expect(row).toBeVisible({ timeout: 15000 })

    // ── Open the provider's edit form ──
    await row.click()
    // The seeded provider has auto_provision=false and empty allowed_domains,
    // so the form defaults to "invitation" mode where the domain input is hidden.
    // Select "Email domain allowlist" to expose it.
    await page.getByTestId('sso-mode-domains').click()
    const domainInput = page.getByTestId('sso-domain-input')
    await expect(domainInput).toBeVisible()

    // Type a domain but do NOT press Enter — this is the #737 regression
    await domainInput.fill('e2e-test.example.com')

    // ── Submit WITHOUT pressing Enter in the domain field ──
    const submitBtn = page.locator('button').filter({ hasText: /Save/ }).first()
    await submitBtn.click()

    // ── Wait for the save to complete ──
    // The add-provider button is always visible, so it is NOT a signal that
    // the edit form closed. Wait for the form to detach, then reload so the
    // re-opened form is seeded from fresh server state: the view calls the
    // async loadProviders() refresh after closing the form, and clicking the
    // row before that lands seeds the form from the stale pre-save provider
    // (auto_provision=false, no domains), hiding the domain input.
    await expect(domainInput).toBeHidden({ timeout: 15000 })
    await page.goto('/settings/sso')
    await expect(row).toBeVisible({ timeout: 15000 })

    // ── Verify domain persisted — re-open edit form and check ──
    await row.click()
    await expect(domainInput).toBeVisible()
    const domainList = page.getByTestId('sso-domain-list')
    await expect(domainList).toContainText('e2e-test.example.com')
  })
})
