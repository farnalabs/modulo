import { computed, reactive } from 'vue'

export interface HitlGateSessionState {
  claimToken: string | null
  notes: string
  editingSubject: boolean
  modifiedSubject: string
  /** FAR-907: the selected option id for a `kind: choice` gate. */
  selectedOptionId: string | null
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
  gateStates.set(
    key,
    reactive<HitlGateSessionState>({ claimToken: null, notes: '', editingSubject: false, modifiedSubject: '', selectedOptionId: null }),
  )
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

  const editingSubject = computed<boolean>({
    get: () => gateStates.get(key)?.editingSubject ?? false,
    set: (value: boolean) => {
      ensureEntry(key).editingSubject = value
    },
  })

  const modifiedSubject = computed<string>({
    get: () => gateStates.get(key)?.modifiedSubject ?? '',
    set: (value: string) => {
      ensureEntry(key).modifiedSubject = value
    },
  })

  function setEditingSubject(value: boolean): void {
    ensureEntry(key).editingSubject = value
  }

  function setModifiedSubject(value: string): void {
    ensureEntry(key).modifiedSubject = value
  }

  /** FAR-907: the choice-gate answer selection survives the 30s auto-refresh. */
  const selectedOptionId = computed<string | null>({
    get: () => gateStates.get(key)?.selectedOptionId ?? null,
    set: (value: string | null) => {
      ensureEntry(key).selectedOptionId = value
    },
  })

  return {
    claimToken,
    notes,
    setClaimToken,
    editingSubject,
    modifiedSubject,
    setEditingSubject,
    setModifiedSubject,
    selectedOptionId,
    clear,
  }
}

/** Drop every persisted gate session — simulates a fresh browser session. */
export function resetHitlGateState(): void {
  gateStates.clear()
}
