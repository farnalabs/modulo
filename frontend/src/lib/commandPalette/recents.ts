/**
 * FAR-53 (Cmd+K Phase 2): "recently visited" source for the command palette.
 *
 * A tiny localStorage-backed most-recently-visited list, recorded by
 * `CommandPalette.vue` on every route change. It feeds the palette's recents
 * provider so a user can jump back to the last few pages they were on.
 *
 * Storage access degrades silently (private mode, quota, storage disabled):
 * a storage failure must never break navigation or the palette — same
 * contract as `useLoginPrefs`.
 */
import { computed, ref, type ComputedRef } from 'vue'

const STORAGE_KEY = 'modulo_cmdk_recent_visits'
const MAX_VISITS = 8

export interface RecentVisit {
  /** Route full path (includes query/hash) the user visited. */
  path: string
  /** Human-readable label resolved at visit time. */
  label: string
  /** Icon name (an `SvgIcon` name) to render next to the entry. */
  icon: string
  /** Epoch ms of the visit — newest first ordering is maintained in `record`. */
  visitedAt: number
}

function isRecentVisit(value: unknown): value is RecentVisit {
  if (typeof value !== 'object' || value === null) return false
  const v = value as Record<string, unknown>
  return (
    typeof v.path === 'string'
    && v.path.length > 0
    && typeof v.label === 'string'
    && v.label.length > 0
  )
}

function readStored(): RecentVisit[] {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.filter(isRecentVisit).slice(0, MAX_VISITS)
  } catch {
    // Storage unavailable or corrupt — treat as "no recents".
    return []
  }
}

function persist(value: RecentVisit[]): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(value))
  } catch (err) {
    console.warn('[command-palette] failed to persist recent visits', err)
  }
}

// Module-scope reactive state: every palette instance (there is exactly one)
// and the recents provider read the same list.
const visits = ref<RecentVisit[]>(readStored())

/** Current recents, newest first. */
export function getRecentVisits(): RecentVisit[] {
  return visits.value
}

/**
 * Record a visit, moving it to the front and de-duplicating by path. Blank
 * labels/paths are ignored so the list can never hold un-navigable junk.
 */
export function recordRecentVisit(visit: { path: string; label: string; icon?: string }): void {
  const path = visit.path.trim()
  const label = visit.label.trim()
  if (!path || !label) return
  const next: RecentVisit[] = [
    { path, label, icon: visit.icon || 'File', visitedAt: Date.now() },
    ...visits.value.filter((v) => v.path !== path),
  ]
  visits.value = next.slice(0, MAX_VISITS)
  persist(visits.value)
}

/** Forget every recorded visit (test isolation / explicit reset). */
export function clearRecentVisits(): void {
  visits.value = []
  try {
    window.localStorage.removeItem(STORAGE_KEY)
  } catch (err) {
    console.warn('[command-palette] failed to clear recent visits', err)
  }
}

export interface UseRecentVisits {
  visits: ComputedRef<RecentVisit[]>
  record: (visit: { path: string; label: string; icon?: string }) => void
  clear: () => void
}

/** Composable wrapper for component use. */
export function useRecentVisits(): UseRecentVisits {
  return {
    visits: computed(() => visits.value),
    record: recordRecentVisit,
    clear: clearRecentVisits,
  }
}
