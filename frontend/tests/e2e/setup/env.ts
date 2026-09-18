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
    // Where the email/password form lives. An instance with more than one
    // login-active org renders an org-slug entry step at /login instead of the
    // form, so a per-org path (/login/<slug>) must be used there. Set
    // E2E_ORG_SLUG to the E2E admin's org slug on multi-org targets; single-org
    // targets keep /login, which auto-skips straight to the direct form.
    loginPath: string
  }
}

const FORM_SELECTORS = {
  email: 'input[type="text"]',
  password: 'input[type="password"]',
}

function getOrgSlug(): string {
  return process.env.E2E_ORG_SLUG || 'default'
}

const ORG_SLUG = process.env.E2E_ORG_SLUG?.trim()
const LOGIN_PATH = ORG_SLUG ? `/login/${encodeURIComponent(ORG_SLUG)}` : '/login'

const ENVS: Record<string, TestEnv> = {
  local: {
    name: 'local',
    orgSlug: getOrgSlug(),
    credentials: {
      admin: { email: 'admin@example.com', password: 'password123' },
      loginFormEmailSelector: FORM_SELECTORS.email,
      loginFormPasswordSelector: FORM_SELECTORS.password,
      loginPath: LOGIN_PATH,
    },
  },
  staging: {
    name: 'staging',
    orgSlug: getOrgSlug(),
    credentials: {
      // Staging uses the real admin account, provided via E2E_ADMIN_EMAIL /
      // E2E_ADMIN_PASSWORD (must match the deployment's MODULO_USERS).
      admin: { email: process.env.E2E_ADMIN_EMAIL, password: process.env.E2E_ADMIN_PASSWORD },
      loginFormEmailSelector: FORM_SELECTORS.email,
      loginFormPasswordSelector: FORM_SELECTORS.password,
      loginPath: LOGIN_PATH,
    },
  },
  app: {
    name: 'app',
    orgSlug: getOrgSlug(),
    credentials: {
      admin: { email: process.env.E2E_ADMIN_EMAIL, password: process.env.E2E_ADMIN_PASSWORD || 'admin123' },
      loginFormEmailSelector: FORM_SELECTORS.email,
      loginFormPasswordSelector: FORM_SELECTORS.password,
      loginPath: LOGIN_PATH,
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
