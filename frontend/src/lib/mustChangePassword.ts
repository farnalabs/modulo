import { ref } from 'vue'
import { getAuthHeaders } from './api/client'

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

// One-shot gate sync from /me using the stored access token. Called after any
// auth hand-off that did not itself report the flag (OIDC/SAML fragment
// callback, restored sessions), so a stale gate held by a previously
// logged-in account cannot survive a login by a different identity. On ANY
// failure — non-2xx, network error, malformed body — the gate is cleared
// (fail-open).
export async function syncFromMe(): Promise<void> {
  try {
    const res = await fetch('/api/v1/auth/me', { headers: getAuthHeaders() })
    if (!res.ok) {
      mustChangePassword.value = false
      return
    }
    const data = await res.json()
    applyMustChangePassword(data?.must_change_password)
  } catch {
    mustChangePassword.value = false
  }
}
