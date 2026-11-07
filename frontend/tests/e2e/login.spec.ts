import { test, expect, openLoginForm, completeLoginForm, setupLocalMockApi } from './setup/fixtures'

test.describe('Login Flow', () => {
  test('shows login form fields', { tag: "@regression" }, async ({ page, env }) => {
    await openLoginForm(page, env)

    await expect(page.locator('h1')).toBeVisible()

    const emailInput = page.locator('input[type="text"]')
    await expect(emailInput).toBeVisible()
    await expect(emailInput).toHaveAttribute('placeholder', /admin@example\.com/)

    const passwordInput = page.locator('input[type="password"]')
    await expect(passwordInput).toBeVisible()

    await expect(page.locator('button[type="submit"]')).toContainText('Sign in')
  })

  test('shows error on failed login', { tag: '@smoke' }, async ({ page, env }) => {
    if (env.name === 'local') {
      await page.route('**/api/v1/auth/login', async (route) => {
        await route.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ detail: 'Invalid credentials' }) })
      })
    }

    const form = await openLoginForm(page, env)

    await page.fill(form.email, 'wrong@example.com')
    await page.fill(form.password, 'thisiswrong')
    await page.click(form.submit)

    await expect(page.getByText(/Invalid credentials|Incorrect email or password/)).toBeVisible({ timeout: 15000 })
  })

  test('surfaces configured SSO providers as login buttons', { tag: '@regression' }, async ({ page }) => {
    await page.route('**/api/v1/auth/sso/providers', (route) => {
      void route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ oidc: [{ provider_id: 'google' }], saml: true }),
      })
    })

    await page.goto('/login')

    await expect(page.getByTestId('login-sso-section')).toBeVisible()
    await expect(page.getByTestId('login-sso-oidc-google')).toHaveAttribute(
      'href',
      '/api/v1/auth/oidc/google/login',
    )
    await expect(page.getByTestId('login-sso-saml')).toHaveAttribute('href', '/api/v1/auth/saml/login')
  })

  test('keeps password login when no SSO providers are configured', { tag: '@regression' }, async ({ page }) => {
    await page.route('**/api/v1/auth/sso/providers', (route) => {
      void route.fulfill({
        status: 402,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'This feature is not available on your plan' }),
      })
    })

    await page.goto('/login')

    await expect(page.getByTestId('login-sso-section')).toHaveCount(0)
    await expect(page.getByTestId('login-submit')).toBeVisible()
  })

  test('completes unauthenticated SSO login through the production callback route', { tag: '@regression' }, async ({ page, env, localSsoBackend }) => {
    test.skip(env.name !== 'local', 'Uses a deterministic local IdP seam; staging uses its real provider configuration')
    void localSsoBackend

    await setupLocalMockApi(page)
    await page.goto('/login')
    const ssoButton = page.getByTestId('login-sso-oidc-ci-idp')
    await expect(ssoButton).toBeVisible()
    await expect(page.evaluate(() => localStorage.getItem('modulo_access_token'))).resolves.toBeNull()

    await ssoButton.click()
    await expect(page.getByText('Continue with fixture IdP')).toBeVisible()
    const continuationUrl = new URL(page.url())
    expect(continuationUrl.searchParams.get('client_id')).toBe('ci-idp')
    expect(continuationUrl.searchParams.get('redirect_uri')).toContain('/api/v1/auth/oidc/ci-idp/callback')
    expect(continuationUrl.searchParams.get('state')).toBeTruthy()
    await expect(page.context().cookies()).resolves.toEqual(expect.arrayContaining([expect.objectContaining({ name: '__Host-modulo_oidc_flow' })]))
    await page.getByText('Continue with fixture IdP').click()

    await page.waitForURL(/\/auth\/callback$/)
    await expect(page.getByText('Signing in')).toBeVisible()
    await page.waitForURL((url) => !url.pathname.includes('/login') && url.pathname !== '/auth/callback')
    await expect(page.getByTestId('dashboard-title')).toContainText('Dashboard')
  })

  test('returns an actionable SSO failure to the unauthenticated login surface', { tag: '@regression' }, async ({ page, env, localSsoBackend }) => {
    test.skip(env.name !== 'local', 'Uses a deterministic local IdP seam; staging uses its real provider configuration')
    void localSsoBackend

    await setupLocalMockApi(page)
    await page.goto('/login')
    await page.getByTestId('login-sso-oidc-ci-idp').click()
    await expect(page.getByText('Continue with fixture IdP')).toBeVisible()
    const continuationUrl = new URL(page.url())
    const state = continuationUrl.searchParams.get('state')
    expect(state).toBeTruthy()
    await page.goto(`/api/v1/auth/oidc/ci-idp/callback?code=ci-invalid-code&state=${encodeURIComponent(state ?? '')}`)
    await page.waitForURL(/\/login\?sso_error=auth_failed$/)
    await expect(page.getByTestId('login-sso-error')).toBeVisible()
    await expect(page.getByTestId('login-submit')).toBeVisible()
  })

  test('redirects away from login on successful login', { tag: '@smoke' }, async ({ page, env }) => {
    if (env.name === 'local') {
      await page.route('**/api/v1/auth/login', async (route) => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            access_token: 'real-login-flow-token',
            refresh_token: 'real-login-flow-refresh-token',
            token_type: 'bearer',
          }),
        })
      })
      await page.route('**/api/v1/me/settings', async (route) => {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ locale: 'en-US' }) })
      })
    }

    const form = await openLoginForm(page, env)

    await page.fill(form.email, env.credentials.admin.email)
    await page.fill(form.password, env.credentials.admin.password)
    await page.click(form.submit)

    await page.waitForURL(/^(?!.*\/login).*$/, { timeout: 15000 })
    await expect(page.getByTestId('dashboard-title')).toContainText('Dashboard')
  })

  test('completes the multi-org slug step onto the org credential form', { tag: '@regression' }, async ({ page, env }) => {
    // Simulate a genuinely multi-org instance: /login renders the slug step,
    // and submitting it navigates to OrgLoginView whose credential inputs use
    // the `org-login-*` testids (not LoginView's `login-*`). Without the
    // org-view-aware selectors this is exactly the FAR-1114 scenario that used
    // to fire the misleading "org slug may be wrong" guard.
    await page.route('**/api/v1/auth/login-context', (route) =>
      void route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ multi_org: true }),
      }),
    )
    await page.route('**/api/v1/auth/org-login/**', (route) =>
      void route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ org: { name: 'Acme', slug: env.orgSlug }, providers: [], password_enabled: true, saml: false }),
      }),
    )

    await page.goto('/login')

    const form = await completeLoginForm(page, env)

    expect(form.email).toBe('[data-testid="org-login-email"]')
    expect(form.password).toBe('[data-testid="org-login-password"]')
    await expect(page.getByTestId('org-login-email')).toBeVisible()
    await expect(page.getByTestId('org-login-password')).toBeVisible()
  })
})
