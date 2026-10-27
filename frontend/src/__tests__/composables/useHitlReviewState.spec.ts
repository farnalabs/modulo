import { describe, it, expect, beforeEach } from 'vitest'
import { useHitlReviewState, resetHitlReviewState } from '../../composables/useHitlReviewState'

describe('useHitlReviewState (FAR-860 selected option)', () => {
  beforeEach(() => {
    resetHitlReviewState()
  })

  it('persists the selected option per gate', () => {
    const first = useHitlReviewState('run-1', 'gate-1')
    first.selectedOption.value = 'ship'
    expect(first.selectedOption.value).toBe('ship')

    // The store is keyed by run:gate — a different gate starts unselected.
    const otherGate = useHitlReviewState('run-1', 'gate-2')
    expect(otherGate.selectedOption.value).toBeNull()
  })

  it('drops the selected option when the gate session is cleared', () => {
    const state = useHitlReviewState('run-1', 'gate-1')
    state.selectedOption.value = 'hold'
    expect(state.selectedOption.value).toBe('hold')

    state.clear()
    expect(state.selectedOption.value).toBeNull()
  })

  it('persists and clears claim, notes, and subject editing state per gate', () => {
    const state = useHitlReviewState('run-1', 'gate-1')
    expect(state.claimToken.value).toBeNull()
    expect(state.notes.value).toBe('')
    expect(state.editingSubject.value).toBe(false)
    expect(state.modifiedSubject.value).toBe('')

    state.setClaimToken('claim-1')
    state.notes.value = 'Needs a second look'
    state.setEditingSubject(true)
    state.setModifiedSubject('Updated subject')

    const restored = useHitlReviewState('run-1', 'gate-1')
    expect(restored.claimToken.value).toBe('claim-1')
    expect(restored.notes.value).toBe('Needs a second look')
    expect(restored.editingSubject.value).toBe(true)
    expect(restored.modifiedSubject.value).toBe('Updated subject')

    restored.editingSubject.value = false
    restored.modifiedSubject.value = 'Another subject'
    expect(state.editingSubject.value).toBe(false)
    expect(state.modifiedSubject.value).toBe('Another subject')
  })

  it('resets every gate selection on resetHitlReviewState', () => {
    const first = useHitlReviewState('run-1', 'gate-1')
    const second = useHitlReviewState('run-2', 'gate-1')
    first.selectedOption.value = 'ship'
    second.selectedOption.value = 'hold'

    resetHitlReviewState()
    expect(first.selectedOption.value).toBeNull()
    expect(second.selectedOption.value).toBeNull()
  })
})
