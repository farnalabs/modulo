import { test, expect, loginAsAdmin } from './setup/fixtures'

/**
 * SSO domain-allowlist + invitation interaction e2e (FAR-1023).
 *
 * Exercises the combination where:
 *   1. An SSO provider is configured in "Email domain allowlist" mode with
 *      allowed_domains = ['allowlisted.example.com'] and default_role = 'viewer'.
 *   2. An admin invites a user whose domain is NOT allowlisted
 *      (invited@otherdomain.com) with org_role = 'operator'.
 *   3. The invited user accepts the invitation via the Accept Invite UI.
 *
 * Assertions:
 *   - The resulting membership carries the invitation's role (operator),
 *     NOT the provider's default role (viewer).
 *   - The invitation is consumed exactly once (no longer in the pending list).
 *
 * Staging safety:
 *   - The SSO provider is seeded DISABLED via the API so it NEVER appears on
 *     the login page. It is deleted unconditionally in afterEach.
 *   - The invited user is deactivated via the API in afterEach.
 *   - The invitation is consumed by the test body; if the test fails before
 *     acceptance, afterEach revokes it.
 *
 * Cannot run locally — needs the real backend to create SSO providers,
 * invitations, and the accept-invite flow. Validated by the Staging E2E CI job.
 */

const UNIQUE = Date.now()
const PROVIDER_NAME = `E2E Allowlist ${UNIQUE}`
const INVITED_EMAIL = `invited-${UNIQUE}@otherdomain.com`
const INVITED_PASSWORD = 'TestPass123!'
const INVITED_ROLE = 'operator'
const PROVIDER_DEFAULT_ROLE = 'viewer'
const ALLOWED_DOMAIN = 'allowlisted.example.com'

let createdProviderId: string | null = null
let createdInvitationId: string | null = null
let createdUserId: string | null = null

// ── Unconditional cleanup ────────────────────────────────────────
// Runs AFTER every test regardless of pass/fail. Guarantees:
//   1. No enabled SSO provider left behind (disabled + deleted).
//   2. No live invitation left behind (revoked if still pending).
//   3. No created user left behind (deactivated).
test.afterEach(async ({ page, env }) => {
  if (env.name === 'local') return

  // Navigate to a known authenticated page so localStorage has a fresh token.
  // The page may be on /accept-invite (unauthenticated) after the invite flow.
  await loginAsAdmin(page, env)

  const token = await page.evaluate(() => localStorage.getItem('modulo_access_token') ?? '')
  const base = page.url().replace(/\/admin\/users$/, '').replace(/\/$/, '')
  const headers = { Authorization: `Bearer ${token}` }

  // 1. Deactivate invited user (if acceptance succeeded and we have their ID)
  if (createdUserId) {
    try {
      await page.request.post(`${base}/api/v1/admin/users/${createdUserId}/deactivate`, { headers })
    } catch { /* best-effort */ }
    createdUserId = null
  }

  // 2. Revoke invitation (if still pending — acceptance may have consumed it)
  if (createdInvitationId) {
    try {
      await page.request.delete(`${base}/api/v1/admin/users/invitations/${createdInvitationId}`, { headers })
    } catch { /* best-effort — may already be consumed */ }
    createdInvitationId = null
  }

  // 3. Delete the SSO provider (always — disabled, so login page unaffected)
  if (createdProviderId) {
    try {
      await page.request.delete(`${base}/api/v1/admin/sso/providers/${createdProviderId}`, { headers })
    } catch { /* best-effort */ }
    createdProviderId = null
  }
})

test.describe('SSO Allowlist + Invitation', { tag: '@regression' }, () => {
  test(
    'invitation role wins over provider default for non-allowlisted domain',
    { tag: '@regression' },
    async ({ page, env }) => {
      // Skip on local — needs the real backend
      if (env.name === 'local') {
        test.skip()
        return
      }

      // ── Step 1: Login as admin ──
      await loginAsAdmin(page, env)

      const token = await page.evaluate(() => localStorage.getItem('modulo_access_token') ?? '')
      const base = page.url().replace(/\/admin\/users$/, '').replace(/\/$/, '')
      const headers = { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }

      // ── Step 2: Seed SSO provider via API (DISABLED, domain-allowlist mode) ──
      //   auto_provision: true  → mode 2 (domain allowlist)
      //   allowed_domains: [ALLOWED_DOMAIN]
      //   default_role: 'viewer'  → the role we expect to be overridden
      //   enabled: false  → NEVER appears on the login page
      const createProvResp = await page.request.post(`${base}/api/v1/admin/sso/providers`, {
        headers,
        data: {
          provider_type: 'oidc',
          name: PROVIDER_NAME,
          client_id: 'e2e-test-client-id',
          client_secret: 'e2e-test-secret-value',
          enabled: false,
          auto_provision: true,
          default_role: PROVIDER_DEFAULT_ROLE,
          preset: 'custom',
          allowed_domains: [ALLOWED_DOMAIN],
        },
      })
      expect(createProvResp.ok()).toBeTruthy()
      const provider = await createProvResp.json()
      createdProviderId = provider.id

      // Verify the provider was created with the expected config
      expect(provider.auto_provision).toBe(true)
      expect(provider.default_role).toBe(PROVIDER_DEFAULT_ROLE)
      expect(provider.allowed_domains).toContain(ALLOWED_DOMAIN)
      expect(provider.enabled).toBe(false)

      // ── Step 3: Create invitation for a non-allowlisted domain ──
      //   The email domain (otherdomain.com) is NOT in allowed_domains.
      //   The invitation role (operator) differs from the provider default (viewer).
      const inviteResp = await page.request.post(`${base}/api/v1/admin/users/invite`, {
        headers,
        data: {
          email: INVITED_EMAIL,
          display_name: 'E2E Invited User',
          org_role: INVITED_ROLE,
        },
      })
      expect(inviteResp.ok()).toBeTruthy()
      const inviteData = await inviteResp.json()
      createdInvitationId = inviteData.id
      expect(inviteData.invite_url).toBeTruthy()

      // ── Step 4: Accept the invitation via the UI ──
      //   Extract the token from the invite URL fragment and navigate to
      //   /accept-invite#token=... — the AcceptInviteView reads it client-side.
      const inviteUrl = new URL(inviteData.invite_url)
      const tokenFromUrl = inviteUrl.hash.replace('#token=', '')
      expect(tokenFromUrl).toBeTruthy()

      await page.goto(`/accept-invite#token=${tokenFromUrl}`)

      // Wait for the form to appear (token was parsed, no "missing token" error)
      await expect(page.getByTestId('accept-invite-missing-token')).not.toBeVisible()
      const passwordInput = page.getByTestId('accept-invite-password')
      await expect(passwordInput).toBeVisible({ timeout: 15000 })

      // Fill password + confirm and submit
      await passwordInput.fill(INVITED_PASSWORD)
      await page.getByTestId('accept-invite-confirm').fill(INVITED_PASSWORD)
      await page.getByTestId('accept-invite-submit').click()

      // Wait for success message
      await expect(page.getByTestId('accept-invite-success')).toBeVisible({ timeout: 15000 })

      // ── Step 5: Verify the invitation is consumed ──
      //   Re-login as admin and check the pending invitations list.
      await loginAsAdmin(page, env)
      const tokenAfter = await page.evaluate(() => localStorage.getItem('modulo_access_token') ?? '')

      const invitationsResp = await page.request.get(
        `${base}/api/v1/admin/users/invitations?page=1&page_size=100`,
        { headers: { Authorization: `Bearer ${tokenAfter}` } },
      )
      expect(invitationsResp.ok()).toBeTruthy()
      const invitationsData = await invitationsResp.json()

      // The invitation we created must NOT appear in the pending list
      const stillPending = invitationsData.items.find(
        (inv: { id: string }) => inv.id === createdInvitationId,
      )
      expect(stillPending).toBeUndefined()
      // Invitation was consumed — clear the ID so afterEach doesn't try to revoke
      createdInvitationId = null

      // ── Step 6: Verify the user's role is the invitation's role, not the provider default ──
      //   Query the admin users API to find the invited user and check their role.
      //   Using the API is more reliable than parsing the Select UI component.
      const usersResp = await page.request.get(
        `${base}/api/v1/admin/users?page=1&page_size=100&search=${encodeURIComponent(INVITED_EMAIL)}`,
        { headers: { Authorization: `Bearer ${tokenAfter}` } },
      )
      expect(usersResp.ok()).toBeTruthy()
      const usersData = await usersResp.json()

      const invitedUser = usersData.items.find(
        (u: { email: string }) => u.email === INVITED_EMAIL,
      )
      expect(invitedUser).toBeTruthy()
      // Store the user ID for cleanup
      createdUserId = invitedUser.id

      // CRITICAL ASSERTION: the membership role must be the invitation's role
      // (operator), NOT the provider's default role (viewer).
      expect(invitedUser.org_role).toBe(INVITED_ROLE)
      expect(invitedUser.org_role).not.toBe(PROVIDER_DEFAULT_ROLE)
    },
  )
})
