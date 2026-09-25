// FAR-1237: the resolved feature-flag map is persisted to localStorage and
// read back SYNCHRONOUSLY when the plan store is created, so flag-driven
// decisions (notably the mobile nav layout: left rail vs legacy top-nav) are
// decidable at first paint instead of after the feature-flags request lands.
//
// Shared by planStore (read/write) and the e2e first-paint specs (seeding) so
// the key and schema version can never drift apart.

/**
 * localStorage key PREFIX for the persisted feature-flag map. The full key is
 * scoped by org id (see `flagCacheKey`) — the flag map is per-org, so a
 * browser that has used more than one org must never hydrate org A's resolved
 * chrome while signing into org B (FAR-1237 review finding: the cache was not
 * multi-org correct).
 */
export const FLAG_CACHE_PREFIX = 'modulo_feature_flags_cache_v1'

/** Bucket used when the current org cannot be resolved (e.g. an opaque token). */
export const FLAG_CACHE_UNKNOWN_ORG = 'unknown'

/** Schema version — bump when the cached shape changes; stale versions are ignored. */
export const FLAG_CACHE_VERSION = 1

export interface FlagCachePayload {
  v: number
  flags: Record<string, boolean>
}

/**
 * Build the localStorage key for one org's persisted flag map. `orgId` is the
 * `org_id` claim read synchronously from the access-token JWT at store
 * creation, so the right bucket is addressable before any network request.
 */
export function flagCacheKey(orgId: string | null | undefined): string {
  return `${FLAG_CACHE_PREFIX}:${orgId || FLAG_CACHE_UNKNOWN_ORG}`
}

/**
 * Parse a raw localStorage value into a flag map.
 * Returns null when absent, corrupt, version-mismatched, or not a flat
 * string→boolean record — a cache must never poison state it cannot prove.
 */
export function parseFlagCache(raw: string | null): Record<string, boolean> | null {
  if (!raw) return null
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null
    const { v, flags } = parsed as Partial<FlagCachePayload>
    if (v !== FLAG_CACHE_VERSION) return null
    if (!flags || typeof flags !== 'object' || Array.isArray(flags)) return null
    for (const value of Object.values(flags)) {
      if (typeof value !== 'boolean') return null
    }
    return flags as Record<string, boolean>
  } catch {
    return null
  }
}

/**
 * Serialize a flag map into the versioned cache payload. Rejects a map with
 * any non-boolean value, matching `parseFlagCache`'s strictness: a cache that
 * would not survive the read guard must never be written.
 */
export function serializeFlagCache(flags: Record<string, boolean>): string {
  for (const value of Object.values(flags)) {
    if (typeof value !== 'boolean') {
      throw new TypeError('flag cache values must be boolean')
    }
  }
  const payload: FlagCachePayload = { v: FLAG_CACHE_VERSION, flags }
  return JSON.stringify(payload)
}

/**
 * Remove every persisted flag-cache entry, across all org buckets. Called when
 * a session ends (logout, expiry, forced clear) so a shared device cannot show
 * the previous user's flag-resolved chrome to the next one (FAR-1237 review
 * finding: the cache survived logout).
 */
export function clearFlagCache(): void {
  try {
    if (typeof localStorage === 'undefined') return
    const stale: string[] = []
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i)
      if (key && key.startsWith(FLAG_CACHE_PREFIX)) stale.push(key)
    }
    for (const key of stale) localStorage.removeItem(key)
  } catch {
    // Storage blocked (private mode, disabled cookies) — nothing to clear.
  }
}
