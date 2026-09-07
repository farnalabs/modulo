import { computed, reactive } from 'vue'

export interface HitlGateSessionState {
  claimToken: string | null
  notes: string
}

// Module-scoped on purpose (FAR-686): the HITL review page's 30s auto-refresh
// and its filter refetches flip the page-level `loading` flag, unmounting every
// HitlGateCard in the list. Per-gate claim tokens and review notes must outlive
// those component instances within the SPA session; a full page reload
// intentionally drops them — the card's re-claim path covers that case.
const gateStates = reactive(new Map<string, HitlGateSessionState>())

function ensureEntry(key: string): HitlGateSessionState {
  const existing = gateStates.get(key)
  if (existing) return existing
  gateStates.set(key, reactive<HitlGateSessionState>({ claimToken: null, notes: '' }))
  return gateStates.get(key) as HitlGateSessionState
}

export function useHitlGateState(runId: string, gateId: string) {
  const key = `${runId}:${gateId}`

  const claimToken = computed<string | null>(() => gateStates.get(key)?.claimToken ?? null)

  const notes = computed<string>({
    get: () => gateStates.get(key)?.notes ?? '',
    set: (value: string) => {
      ensureEntry(key).notes = value
    },
  })

  function setClaimToken(token: string): void {
    ensureEntry(key).claimToken = token
  }

  function clear(): void {
    gateStates.delete(key)
  }

  return { claimToken, notes, setClaimToken, clear }
}

/** Drop every persisted gate session — simulates a fresh browser session. */
export function resetHitlGateState(): void {
  gateStates.clear()
}
