import { ref } from 'vue'
import { api } from './api/client'

// FAR-460: global "must change password" gate state.
//
// Set true when the login response or /me reports must_change_password=true;
// App.vue replaces the whole app surface with the forced change-password view
// until the user succeeds, so navigation is blocked by construction rather
// than by per-route guards. The backend treats this flag as UX-only, so every
// consumer is deliberately fail-open: anything other than strictly `true`
// (and any failure to sync) leaves the app unlocked.
const mustChangePassword = ref(false)

export function setMustChangePassword(value: boolean): void {
  mustChangePassword.value = value
}

export function useMustChangePassword(): typeof mustChangePassword {
  return mustChangePassword
}

// Single normalisation point for payload flags: only a JSON boolean `true`
// arms the gate. Unifies consumers that previously mixed `=== true` with
// typeof-checks (absent/null/string values all clear the gate).
export function applyMustChangePassword(value: unknown): void {
  mustChangePassword.value = value === true
}

// One-shot gate sync from /me using the typed openapi-fetch client. Called
// after any auth hand-off that did not itself report the flag (OIDC/SAML
// fragment callback, restored sessions), so a stale gate held by a previously
// logged-in account cannot survive a login by a different identity.
//
// Unlike a bare fetch, `api.GET` injects auth headers and transparently
// refreshes an expired access token (and redirects to login on hard failure),
// so a restored session with a stale token is re-synced correctly instead of
// silently clearing the gate until the next manual login.
//
// On ANY remaining failure — non-2xx that refresh could not resolve, network
// error, malformed body — the gate is cleared (fail-open).
export async function syncFromMe(): Promise<void> {
  try {
    const { data, error } = await api.GET('/api/v1/auth/me')
    if (error) {
      mustChangePassword.value = false
      return
    }
    applyMustChangePassword(data?.must_change_password)
  } catch {
    mustChangePassword.value = false
  }
}
