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

<<<<<<< HEAD
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
    const authorizeUrl = new URL(new URL(page.url()).searchParams.get('redirect_uri') ?? '')
    await page.getByText('Continue with fixture IdP').click()
    expect(authorizeUrl.searchParams.get('client_id')).toBe('ci-idp')
    expect(authorizeUrl.searchParams.get('redirect_uri')).toContain('/api/v1/auth/oidc/ci-idp/callback')
    expect(authorizeUrl.searchParams.get('state')).toBeTruthy()
    await expect(page.context().cookies()).resolves.toEqual(expect.arrayContaining([expect.objectContaining({ name: '__Host-modulo_oidc_flow' })]))

    await page.waitForURL(/\/auth\/callback$/)
=======
  test('completes unauthenticated SSO login through a deterministic IdP callback seam', { tag: '@regression' }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'Uses a deterministic local IdP seam; staging uses its real provider configuration')

    await setupLocalMockApi(page)
    await page.route('**/api/v1/auth/login-context', (route) =>
      void route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ multi_org: false, org: { name: 'Acme', slug: 'default' } }),
      }),
    )
    await page.route('**/api/v1/auth/sso/providers', (route) =>
      void route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ oidc: [{ provider_id: 'ci-idp', display_name: 'CI Identity Provider', preset: 'custom' }], saml: false }),
      }),
    )
    await page.goto('/login')
    const ssoButton = page.getByTestId('login-sso-oidc-ci-idp')
    await expect(ssoButton).toBeVisible()

    await page.route('**/api/v1/auth/oidc/ci-idp/login', async (route) => {
      expect(route.request().method()).toBe('GET')
      expect(route.request().headers().authorization).toBeUndefined()
      await route.fulfill({
        status: 307,
        headers: {
          location: '/test-idp/authorize?client_id=ci-idp&redirect_uri=%2Fapi%2Fv1%2Fauth%2Foidc%2Fci-idp%2Fcallback&state=ci-state',
          'set-cookie': '__Host-modulo_oidc_flow=ci-flow; Path=/; HttpOnly; SameSite=Lax; Secure',
        },
      })
    })
    await page.route('**/test-idp/authorize**', async (route) => {
      const requestUrl = new URL(route.request().url())
      expect(requestUrl.searchParams.get('client_id')).toBe('ci-idp')
      expect(requestUrl.searchParams.get('redirect_uri')).toBe('/api/v1/auth/oidc/ci-idp/callback')
      expect(requestUrl.searchParams.get('state')).toBe('ci-state')
      await route.fulfill({ status: 200, contentType: 'text/html', body: 'CI IdP authorized' })
    })
    await page.route('**/api/v1/auth/oidc/ci-idp/callback**', async (route) => {
      const request = route.request()
      expect(request.method()).toBe('GET')
      expect(request.headers().authorization).toBeUndefined()
      expect(request.headers().cookie).toContain('__Host-modulo_oidc_flow=ci-flow')
      const callbackUrl = new URL(request.url())
      expect(callbackUrl.searchParams.get('code')).toBe('ci-auth-code')
      expect(callbackUrl.searchParams.get('state')).toBe('ci-state')
      await route.fulfill({
        status: 307,
        headers: {
          location: '/auth/callback#access_token=ci-sso-access-token',
          'set-cookie': 'modulo_refresh=ci-refresh-token; Path=/; HttpOnly; SameSite=Lax',
        },
      })
    })

    await ssoButton.click()
    await page.waitForURL(/\/test-idp\/authorize/)
<<<<<<< HEAD
    await expect(page.getByText('CI IdP ready')).toBeVisible()
    await page.goto('/auth/callback#access_token=ci-sso-access-token')
    await expect(page).toHaveURL(/\/auth\/callback$/)
>>>>>>> 825d19d61 (test: cover unauthenticated SSO login journey)
=======
    const callbackRequest = page.waitForRequest('**/api/v1/auth/oidc/ci-idp/callback**')
    await page.goto('/api/v1/auth/oidc/ci-idp/callback?code=ci-auth-code&state=ci-state')
    await callbackRequest
    await page.waitForURL(/\/auth\/callback$/)
>>>>>>> 4ebbbe539 (test: exercise backend SSO callback redirects)
    await expect(page.getByText('Signing in')).toBeVisible()
    await page.waitForURL((url) => !url.pathname.includes('/login') && url.pathname !== '/auth/callback')
    await expect(page.getByTestId('dashboard-title')).toContainText('Dashboard')
  })

<<<<<<< HEAD
  test('returns an actionable SSO failure to the unauthenticated login surface', { tag: '@regression' }, async ({ page, env, localSsoBackend }) => {
    test.skip(env.name !== 'local', 'Uses a deterministic local IdP seam; staging uses its real provider configuration')
    void localSsoBackend

    await setupLocalMockApi(page)
    await page.goto('/login')
    await page.getByTestId('login-sso-oidc-ci-idp').click()
    await expect(page.getByText('Continue with fixture IdP')).toBeVisible()
    const authorizeUrl = new URL(new URL(page.url()).searchParams.get('redirect_uri') ?? '')
    const state = authorizeUrl.searchParams.get('state')
    expect(state).toBeTruthy()
    await page.goto(`/api/v1/auth/oidc/ci-idp/callback?code=ci-invalid-code&state=${encodeURIComponent(state ?? '')}`)
    await page.waitForURL(/\/login\?sso_error=auth_failed$/)
=======
  test('returns an actionable SSO failure to the unauthenticated login surface', { tag: '@regression' }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'Uses a deterministic local IdP seam; staging uses its real provider configuration')

    await setupLocalMockApi(page)
    await page.route('**/api/v1/auth/login-context', (route) =>
      void route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ multi_org: false, org: { name: 'Acme', slug: 'default' } }),
      }),
    )
    await page.route('**/api/v1/auth/sso/providers', (route) =>
      void route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ oidc: [{ provider_id: 'ci-idp', display_name: 'CI Identity Provider', preset: 'custom' }], saml: false }),
      }),
    )
    await page.route('**/api/v1/auth/oidc/ci-idp/login', async (route) => {
      expect(route.request().method()).toBe('GET')
      expect(route.request().headers().authorization).toBeUndefined()
      await route.fulfill({
        status: 307,
        headers: {
          location: '/test-idp/authorize?client_id=ci-idp&redirect_uri=%2Fapi%2Fv1%2Fauth%2Foidc%2Fci-idp%2Fcallback&state=ci-state',
          'set-cookie': '__Host-modulo_oidc_flow=ci-flow; Path=/; HttpOnly; SameSite=Lax; Secure',
        },
      })
    })
    await page.route('**/test-idp/authorize**', async (route) => {
      const requestUrl = new URL(route.request().url())
      expect(requestUrl.searchParams.get('redirect_uri')).toBe('/api/v1/auth/oidc/ci-idp/callback')
      expect(requestUrl.searchParams.get('state')).toBe('ci-state')
      await route.fulfill({ status: 200, contentType: 'text/html', body: 'CI IdP authorized' })
    })
    await page.route('**/api/v1/auth/oidc/ci-idp/callback**', async (route) => {
      const request = route.request()
      expect(request.method()).toBe('GET')
      expect(request.headers().authorization).toBeUndefined()
      expect(request.headers().cookie).toContain('__Host-modulo_oidc_flow=ci-flow')
      const callbackUrl = new URL(request.url())
      expect(callbackUrl.searchParams.get('code')).toBe('ci-auth-code')
      expect(callbackUrl.searchParams.get('state')).toBe('ci-state')
      await route.fulfill({
        status: 303,
        headers: { location: '/login?sso_error=auth_failed' },
      })
    })

    await page.goto('/login')
    await page.getByTestId('login-sso-oidc-ci-idp').click()
    await page.waitForURL(/\/test-idp\/authorize/)
<<<<<<< HEAD
    await expect(page.getByText('CI IdP denied')).toBeVisible()
    await page.goto('/login?sso_error=auth_failed')
>>>>>>> 825d19d61 (test: cover unauthenticated SSO login journey)
=======
    const callbackRequest = page.waitForRequest('**/api/v1/auth/oidc/ci-idp/callback**')
    await page.goto('/api/v1/auth/oidc/ci-idp/callback?code=ci-auth-code&state=ci-state')
    await callbackRequest
    await page.waitForURL(/\/login\?sso_error=auth_failed$/)
>>>>>>> 4ebbbe539 (test: exercise backend SSO callback redirects)
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
