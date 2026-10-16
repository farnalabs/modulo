// FAR-1791: shared SSO callback-error vocabulary + normaliser.
//
// The backend appends `?sso_error=<code>` to the login URL when a browser-
// facing SSO callback (OIDC callback / SAML ACS) fails. Both LoginView.vue and
// OrgLoginView.vue must interpret that value identically, so the vocabulary and
// the normaliser live here in ONE place rather than being duplicated verbatim
// in each view.

/**
 * The whitelisted `sso_error` codes the backend may emit.
 *
 * Mirrors `modulo.auth.sso`'s `SSO_ERROR_*` constants. Kept as a `const` tuple
 * so `SsoErrorCode` is derived from — and can never drift from — the list.
 */
export const SSO_ERROR_CODES = ['not_invited', 'auth_failed', 'provider_unavailable'] as const

export type SsoErrorCode = (typeof SSO_ERROR_CODES)[number]

/**
 * Normalise the `sso_error` query value to a known code.
 *
 * Returns `null` when absent (no alert). An UNKNOWN/garbled value falls back to
 * the generic `auth_failed` copy. The raw value is never returned, rendered or
 * echoed back — the caller only ever gets a value from the fixed vocabulary.
 */
export function normaliseSsoErrorCode(raw: unknown): SsoErrorCode | null {
  if (raw === undefined || raw === null || raw === '') return null
  const value = Array.isArray(raw) ? raw[0] : raw
  if (typeof value === 'string' && (SSO_ERROR_CODES as readonly string[]).includes(value)) {
    return value as SsoErrorCode
  }
  return 'auth_failed'
}

/**
 * i18n key for the alert copy of a normalised code, or `null` when there is no
 * error to show.
 */
export function ssoErrorI18nKey(code: SsoErrorCode | null): string | null {
  return code ? `auth.ssoError.${code}` : null
}
