import { api } from './client'

/**
 * Database capacity modes.
 * - `fixed`    — a hard capacity limit is enforced; the percent-based alert
 *                levels (warn/critical/full) are meaningful.
 * - `elastic`  — storage grows on demand (advisory/cost note only); no banner.
 * - `disabled` — capacity tracking is disabled; no banner.
 */
export type DbCapacityMode = 'fixed' | 'elastic' | 'disabled'

/**
 * Database capacity alert levels (percent-derived when mode is `fixed`):
 * - `ok`       — below the warn threshold (<80%); no banner.
 * - `warn`     — >=80%; amber, clears storage soon.
 * - `critical` — >=90%; red, storage near the limit.
 * - `full`     — >=98%; red, new runs are disabled.
 */
export type DbCapacityAlertLevel = 'ok' | 'warn' | 'critical' | 'full'

export interface DbCapacityInfo {
  capacity_percent: number | null
  mode: DbCapacityMode | (string & {})
  alert_level: DbCapacityAlertLevel | (string & {})
  used_bytes: number
  capacity_bytes: number
}

type GetDbCapacity = (
  path: '/api/v1/admin/db-capacity',
  options: Record<string, never>,
) => Promise<{ data?: DbCapacityInfo; error?: unknown }>

/**
 * Fetch the organisation's database capacity. Returns the capacity object, or
 * `null` when the endpoint is unavailable or errors. The banner handles a
 * `null` result by rendering nothing — a failed poll must never surface as a
 * spurious error banner (the endpoint is admin-scoped and may be absent on
 * some deployments).
 */
export async function fetchDbCapacity(): Promise<DbCapacityInfo | null> {
  try {
    const getDbCapacity = api.GET as unknown as GetDbCapacity
    const resp = await getDbCapacity('/api/v1/admin/db-capacity', {})
    if (resp.error) return null
    return resp.data ?? null
  } catch {
    return null
  }
}
