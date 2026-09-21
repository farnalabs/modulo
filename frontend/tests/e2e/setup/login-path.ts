/**
 * Dynamic login-path resolver for E2E tests.
 *
 * Fetches /api/v1/auth/login-context once (module-level cache) and resolves the
 * correct login URL based on the instance's multi-org state:
 *
 * - E2E_ORG_SLUG override set AND multi_org === true  → /login/<slug>
 * - E2E_ORG_SLUG override set AND single-org           → /login (ignore override)
 * - No override                                        → /login
 *
 * This fixes FAR-1123: deploy.yml unconditionally sets E2E_ORG_SLUG=default,
 * which caused /login/default on single-org staging — a route that never renders
 * the email form, burning 90 minutes on selector timeouts.
 */

interface LoginContextResponse {
  multi_org: boolean
  /** Other fields (orgs list, etc.) are ignored. */
}

let cachedContext: LoginContextResponse | null = null
let cachedBaseURL: string | null = null
let fetchFailed = false

/**
 * Resolve the login path for the given base URL.
 *
 * Results are cached per baseURL so the login-context endpoint is called at
 * most once per test run — not per test.
 */
export async function resolveLoginPath(baseURL: string): Promise<string> {
  const overrideSlug = process.env.E2E_ORG_SLUG?.trim()

  if (!overrideSlug) return '/login'

  // Fetch login-context if not cached for this baseURL
  if (cachedContext === null || cachedBaseURL !== baseURL) {
    fetchFailed = false
    try {
      const resp = await fetch(`${baseURL}/api/v1/auth/login-context`, {
        signal: AbortSignal.timeout(5000),
      })
      if (resp.ok) {
        cachedContext = await resp.json()
        cachedBaseURL = baseURL
      } else {
        fetchFailed = true
      }
    } catch {
      // Network error — mark as failed
      fetchFailed = true
    }
  }

  if (cachedContext?.multi_org) {
    return `/login/${encodeURIComponent(overrideSlug)}`
  }

  // Fetch failed with no cached result: fall back to /login.
  // /login is safe on both single-org (renders email form directly) and
  // multi-org (renders slug selector, which completeLoginForm handles).
  // Using /login/<slug> here would be a dead page on single-org instances
  // (the common case on staging), recreating the 90-min crawl FAR-1123 fixes.
  if (fetchFailed && cachedContext === null) {
    return '/login'
  }

  // Single-org: ignore the override (fixes FAR-1123)
  return '/login'
}

/** Reset the cache (for unit testing). */
export function resetLoginContextCache(): void {
  cachedContext = null
  cachedBaseURL = null
  fetchFailed = false
}
