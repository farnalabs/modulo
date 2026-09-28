import { computed, reactive } from 'vue'

export interface HitlReviewSessionState {
  claimToken: string | null
  notes: string
  editingSubject: boolean
  modifiedSubject: string
  /** FAR-860: the option_id selected by the reviewer for kind:choice gates. */
  selectedOption: string | null
}

// Module-scoped on purpose (FAR-686): the HITL review page's 30s auto-refresh
// and its filter refetches flip the page-level `loading` flag, unmounting every
// HitlReviewCard in the list. Per-gate claim tokens and review notes must outlive
// those component instances within the SPA session; a full page reload
// intentionally drops them — the card's re-claim path covers that case.
const reviewStates = reactive(new Map<string, HitlReviewSessionState>())

function ensureEntry(key: string): HitlReviewSessionState {
  const existing = reviewStates.get(key)
  if (existing) return existing
  reviewStates.set(key, reactive<HitlReviewSessionState>({ claimToken: null, notes: '', editingSubject: false, modifiedSubject: '', selectedOption: null }))
  return reviewStates.get(key) as HitlReviewSessionState
}

export function useHitlReviewState(runId: string, gateId: string) {
  const key = `${runId}:${gateId}`

  const claimToken = computed<string | null>(() => reviewStates.get(key)?.claimToken ?? null)

  const notes = computed<string>({
    get: () => reviewStates.get(key)?.notes ?? '',
    set: (value: string) => {
      ensureEntry(key).notes = value
    },
  })

  function setClaimToken(token: string): void {
    ensureEntry(key).claimToken = token
  }

  function clear(): void {
    reviewStates.delete(key)
  }

  const editingSubject = computed<boolean>({
    get: () => reviewStates.get(key)?.editingSubject ?? false,
    set: (value: boolean) => {
      ensureEntry(key).editingSubject = value
    },
  })

  const modifiedSubject = computed<string>({
    get: () => reviewStates.get(key)?.modifiedSubject ?? '',
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

  const selectedOption = computed<string | null>({
    get: () => reviewStates.get(key)?.selectedOption ?? null,
    set: (value: string | null) => {
      ensureEntry(key).selectedOption = value
    },
  })

  return { claimToken, notes, setClaimToken, editingSubject, modifiedSubject, setEditingSubject, setModifiedSubject, selectedOption, clear }
}

/** Drop every persisted gate session — simulates a fresh browser session. */
export function resetHitlReviewState(): void {
  reviewStates.clear()
}
