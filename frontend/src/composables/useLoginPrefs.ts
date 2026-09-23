/**
 * localStorage-backed login preferences: remember the last organisation slug
 * a user entered and the last login method they successfully used.
 *
 * Single construction point for every login-preference storage read/write.
 * ALL access degrades silently when localStorage is unavailable or throws
 * (private mode, quota exceeded, disabled storage, SSR): a storage failure
 * must never break the login flow — helpers return null / no-op instead.
 */

const LAST_ORG_KEY = 'modulo_login_last_org'
const LAST_METHOD_KEY = 'modulo_login_last_method'

function read(key: string): string | null {
  try {
    return window.localStorage.getItem(key)
  } catch {
    // Storage unavailable — treat as "no preference stored".
    return null
  }
}

function write(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value)
  } catch (err) {
    // Storage unavailable — the preference is simply not remembered.
    console.warn('[login-prefs] failed to persist login preference', err)
  }
}

function remove(key: string): void {
  try {
    window.localStorage.removeItem(key)
  } catch (err) {
    // Storage unavailable — nothing to clear.
    console.warn('[login-prefs] failed to clear login preference', err)
  }
}

export interface LoginPrefs {
  /** Last organisation slug the user entered/signed into, or null. */
  getLastOrgSlug: () => string | null
  /** Persist the organisation slug the user is proceeding with. */
  setLastOrgSlug: (slug: string) => void
  /** Forget the stored organisation slug (e.g. "change organization"). */
  clearLastOrgSlug: () => void
  /** Last successful login method ('password' or an SSO provider_id), or null. */
  getLastMethod: () => string | null
  /** Persist the login method that just succeeded. */
  setLastMethod: (method: string) => void
}

export function useLoginPrefs(): LoginPrefs {
  return {
    getLastOrgSlug: () => {
      const value = read(LAST_ORG_KEY)
      return value && value.trim() ? value.trim() : null
    },
    setLastOrgSlug: (slug: string) => {
      const trimmed = slug.trim()
      if (trimmed) write(LAST_ORG_KEY, trimmed)
    },
    clearLastOrgSlug: () => {
      remove(LAST_ORG_KEY)
    },
    getLastMethod: () => {
      const value = read(LAST_METHOD_KEY)
      return value && value.trim() ? value.trim() : null
    },
    setLastMethod: (method: string) => {
      const trimmed = method.trim()
      if (trimmed) write(LAST_METHOD_KEY, trimmed)
    },
  }
}
