export interface TestEnv {
  name: 'local' | 'staging' | 'app'
  // Multi-org instances (FAR-856/857) render a slug-entry step on /login
  // before the email/password form. The E2E admin always belongs to the
  // instance's first (default) org; override with E2E_ORG_SLUG when a target
  // uses a different slug. Single-org instances ignore this entirely.
  orgSlug: string
  credentials: {
    admin: { email: string; password: string }
    loginFormEmailSelector: string
    loginFormPasswordSelector: string
    orgSlugInputSelector: string
    orgSlugSubmitSelector: string
    // Always /login.  On multi-org instances the slug step is completed by
    // completeLoginForm(); on single-org instances the credential form renders
    // directly.  Never navigate to /login/<slug> from E2E — the slug step
    // must be handled programmatically so the same code path covers both.
    loginPath: string
  }
}

const TESTID_SELECTORS = {
  email: '[data-testid="login-email"]',
  password: '[data-testid="login-password"]',
  orgSlugInput: '[data-testid="login-org-slug"]',
  orgSlugSubmit: '[data-testid="login-org-entry-submit"]',
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
      loginFormEmailSelector: TESTID_SELECTORS.email,
      loginFormPasswordSelector: TESTID_SELECTORS.password,
      orgSlugInputSelector: TESTID_SELECTORS.orgSlugInput,
      orgSlugSubmitSelector: TESTID_SELECTORS.orgSlugSubmit,
      loginPath: '/login',
    },
  },
  staging: {
    name: 'staging',
    orgSlug: getOrgSlug(),
    credentials: {
      // Staging uses the real admin account, provided via E2E_ADMIN_EMAIL /
      // E2E_ADMIN_PASSWORD (must match the deployment's MODULO_USERS).
      admin: { email: process.env.E2E_ADMIN_EMAIL, password: process.env.E2E_ADMIN_PASSWORD },
      loginFormEmailSelector: TESTID_SELECTORS.email,
      loginFormPasswordSelector: TESTID_SELECTORS.password,
      orgSlugInputSelector: TESTID_SELECTORS.orgSlugInput,
      orgSlugSubmitSelector: TESTID_SELECTORS.orgSlugSubmit,
      loginPath: '/login',
    },
  },
  app: {
    name: 'app',
    orgSlug: getOrgSlug(),
    credentials: {
      admin: { email: process.env.E2E_ADMIN_EMAIL, password: process.env.E2E_ADMIN_PASSWORD || 'admin123' },
      loginFormEmailSelector: TESTID_SELECTORS.email,
      loginFormPasswordSelector: TESTID_SELECTORS.password,
      orgSlugInputSelector: TESTID_SELECTORS.orgSlugInput,
      orgSlugSubmitSelector: TESTID_SELECTORS.orgSlugSubmit,
      loginPath: '/login',
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
