// FAR-1237: the resolved feature-flag map is persisted to localStorage and
// read back SYNCHRONOUSLY when the plan store is created, so flag-driven
// decisions (notably the mobile nav layout: left rail vs legacy top-nav) are
// decidable at first paint instead of after the feature-flags request lands.
//
// Shared by planStore (read/write) and the e2e first-paint specs (seeding) so
// the key and schema version can never drift apart.

/** localStorage key for the persisted feature-flag map. */
export const FLAG_CACHE_KEY = 'modulo_feature_flags_cache_v1'

/** Schema version — bump when the cached shape changes; stale versions are ignored. */
export const FLAG_CACHE_VERSION = 1

export interface FlagCachePayload {
  v: number
  flags: Record<string, boolean>
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

/** Serialize a flag map into the versioned cache payload. */
export function serializeFlagCache(flags: Record<string, boolean>): string {
  const payload: FlagCachePayload = { v: FLAG_CACHE_VERSION, flags }
  return JSON.stringify(payload)
}
