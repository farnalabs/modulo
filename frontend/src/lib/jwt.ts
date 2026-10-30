/**
 * Shared base64url decode primitive — the single source of truth for
 * base64url decoding (the access token's payload segment, and any other
 * base64url segment such as a licence key's payload).
 *
 * FIX 6: three inline copies (SettingsTriggersView, router, AppLayout)
 * disagreed on padding handling — two padded, one did not. This version
 * handles BOTH padded and unpadded base64url (padding is recomputed from the
 * segment length), so every consumer behaves identically. Never throws:
 * returns null on any decode/parse failure.
 */

export function decodeBase64Url(s: string): string {
  s = s.replaceAll('-', '+').replaceAll('_', '/')
  const pad = s.length % 4
  if (pad) s += '='.repeat(4 - pad)
  return atob(s)
}

export function decodeJwtPayload(token: string | null | undefined): Record<string, unknown> | null {
  if (!token) return null
  try {
    return JSON.parse(decodeBase64Url(token.split('.')[1])) as Record<string, unknown>
  } catch {
    return null
  }
}

/**
 * The subject (`sub` claim) a token belongs to. `null` = no token / opaque
 * token. Used to tell a ROTATED token for the same person (a refresh — same
 * sub, keep the resolved state) from a different principal (reset), because a
 * same-SPA login can replace the token without ever passing through `null`.
 */
export function principalSubject(token: string | null | undefined): string | null {
  if (!token) return null
  const sub = decodeJwtPayload(token)?.sub
  return typeof sub === 'string' && sub.length > 0 ? sub : null
}
