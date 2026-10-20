/**
 * Shared random-identifier helper (FAR-2070).
 *
 * Internal, non-security-critical IDs across the SPA all need the same
 * precedence chain: `crypto.randomUUID()` when available, then CSPRNG bytes
 * (`crypto.getRandomValues`), and only as a last resort a timestamp/Math.random
 * pair for environments with no CSPRNG at all. Keeping that chain in one place
 * stops the per-module copies from drifting apart.
 *
 * @param prefix - optional namespace prefix, e.g. `tab`
 */
export function randomId(prefix?: string): string {
  const cryptoApi = globalThis.crypto
  let id: string
  if (cryptoApi && typeof cryptoApi.randomUUID === 'function') {
    id = cryptoApi.randomUUID()
  } else if (cryptoApi && typeof cryptoApi.getRandomValues === 'function') {
    const bytes = cryptoApi.getRandomValues(new Uint8Array(16))
    id = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
  } else {
    id = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}` // NOSONAR: only when no CSPRNG exists
  }
  return prefix ? `${prefix}-${id}` : id
}
