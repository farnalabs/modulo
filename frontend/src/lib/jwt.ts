/**
 * Shared JWT payload decoder — the single source of truth for base64url
 * decoding of the access token's payload segment.
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
