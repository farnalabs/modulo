export interface AutoLoginConfig {
  username: string
  password: string
  /** Org slug for the login request — binds the session to that org. */
  org_slug: string
}

/**
 * Default demo org slug — must stay in lockstep with the backend constant
 * `DEMO_ORG_SLUG` (backend/src/modulo/core/demo.py), which names the org the
 * demo seed creates and the org the login route binds to (FAR-865).
 */
const DEFAULT_DEMO_ORG_SLUG = 'demo'

export function getAutoLoginConfig(): AutoLoginConfig | undefined {
  const config = window.__MODULO_CONFIG__?.autoLogin
  if (
    !config
    || typeof config.username !== 'string'
    || typeof config.password !== 'string'
    || !config.username
    || !config.password
  ) {
    return undefined
  }
  const orgSlug = typeof config.orgSlug === 'string' && config.orgSlug
    ? config.orgSlug
    : DEFAULT_DEMO_ORG_SLUG
  return { username: config.username, password: config.password, org_slug: orgSlug }
}
