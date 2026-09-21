export interface TestEnv {
  name: 'local' | 'staging' | 'app'
  // Multi-org instances (FAR-856/857) render a slug-entry step on /login
  // before the email/password form. The E2E admin always belongs to the
  // instance's first (default) org; override with E2E_ORG_SLUG when a target
  // uses a different slug. Single-org instances ignore this entirely.
  orgSlug: string
  credentials: {
    admin: { email: string; password: string }
    // Single-org credential form — LoginView's direct branch on /login.
    loginFormEmailSelector: string
    loginFormPasswordSelector: string
    loginFormSubmitSelector: string
    // Per-org credential form — OrgLoginView, reached after the slug step
    // navigates to /login/<slug>. Multi-org targets render this form, not
    // LoginView's, so completeLoginForm() resolves whichever appeared.
    orgLoginFormEmailSelector: string
    orgLoginFormPasswordSelector: string
    orgLoginFormSubmitSelector: string
    // Multi-org slug-entry step — LoginView's branch when more than one org
    // is login-active.
    orgSlugInputSelector: string
    orgSlugSubmitSelector: string
    // Static fallback login path (always /login). The runtime path is resolved
    // by resolveLoginPath() in login-path.ts, which queries login-context and
    // returns /login/<slug> only when the instance is genuinely multi-org
    // (E2E_ORG_SLUG is an override, ignored on single-org instances).
    // completeLoginForm() then handles whichever layout rendered — the slug
    // step on /login or the credential form directly.
    loginPath: string
  }
}

const TESTID_SELECTORS = {
  email: '[data-testid="login-email"]',
  password: '[data-testid="login-password"]',
  submit: '[data-testid="login-submit"]',
  orgEmail: '[data-testid="org-login-email"]',
  orgPassword: '[data-testid="org-login-password"]',
  orgSubmit: '[data-testid="org-login-submit"]',
  orgSlugInput: '[data-testid="login-org-slug"]',
  orgSlugSubmit: '[data-testid="login-org-entry-submit"]',
}

// The app's data-testids and the login path are identical across local,
// staging and app, so the nine credential-form fields are defined once and
// spread into each target's `credentials` instead of repeated verbatim.
type CredentialSelectors = Pick<
  TestEnv['credentials'],
  | 'loginFormEmailSelector'
  | 'loginFormPasswordSelector'
  | 'loginFormSubmitSelector'
  | 'orgLoginFormEmailSelector'
  | 'orgLoginFormPasswordSelector'
  | 'orgLoginFormSubmitSelector'
  | 'orgSlugInputSelector'
  | 'orgSlugSubmitSelector'
  | 'loginPath'
>

const SHARED_CREDENTIALS: CredentialSelectors = {
  loginFormEmailSelector: TESTID_SELECTORS.email,
  loginFormPasswordSelector: TESTID_SELECTORS.password,
  loginFormSubmitSelector: TESTID_SELECTORS.submit,
  orgLoginFormEmailSelector: TESTID_SELECTORS.orgEmail,
  orgLoginFormPasswordSelector: TESTID_SELECTORS.orgPassword,
  orgLoginFormSubmitSelector: TESTID_SELECTORS.orgSubmit,
  orgSlugInputSelector: TESTID_SELECTORS.orgSlugInput,
  orgSlugSubmitSelector: TESTID_SELECTORS.orgSlugSubmit,
  loginPath: '/login',
}

function getOrgSlug(): string {
  return process.env.E2E_ORG_SLUG || 'default'
}

const ENVS: Record<string, TestEnv> = {
  local: {
    name: 'local',
    orgSlug: getOrgSlug(),
    credentials: {
      admin: { email: 'admin@example.com', password: 'password123' },
      ...SHARED_CREDENTIALS,
    },
  },
  staging: {
    name: 'staging',
    orgSlug: getOrgSlug(),
    credentials: {
      // Staging uses the real admin account, provided via E2E_ADMIN_EMAIL /
      // E2E_ADMIN_PASSWORD (must match the deployment's MODULO_USERS).
      admin: { email: process.env.E2E_ADMIN_EMAIL, password: process.env.E2E_ADMIN_PASSWORD },
      ...SHARED_CREDENTIALS,
    },
  },
  app: {
    name: 'app',
    orgSlug: getOrgSlug(),
    credentials: {
      admin: { email: process.env.E2E_ADMIN_EMAIL, password: process.env.E2E_ADMIN_PASSWORD || 'admin123' },
      ...SHARED_CREDENTIALS,
    },
  },
}

export const BASE_URLS: Record<string, string> = {
  local: 'http://127.0.0.1:5173',
  staging: 'https://staging.modulo.run',
  app: 'https://app.modulo.run',
}

export function getTarget(): string {
  return (process.env.E2E_TARGET || 'local').toLowerCase()
}

export function getBaseUrl(target?: string): string {
  const t = target ?? getTarget()
  return process.env.E2E_BASE_URL || BASE_URLS[t] || BASE_URLS.local
}

export function getTestEnv(): TestEnv {
  const target = getTarget()
  if (!ENVS[target]) {
    console.warn(`[env] Unknown E2E_TARGET "${process.env.E2E_TARGET}", falling back to "local". Valid values: ${Object.keys(ENVS).join(', ')}`)
  }
  return ENVS[target] || ENVS.local
}
