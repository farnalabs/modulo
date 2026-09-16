import { describe, it, expect, beforeEach } from 'vitest'
import { useHitlGateState, resetHitlGateState } from '../../composables/useHitlGateState'

describe('useHitlGateState (FAR-860 selected option)', () => {
  beforeEach(() => {
    resetHitlGateState()
  })

  it('persists the selected option per gate', () => {
    const first = useHitlGateState('run-1', 'gate-1')
    first.selectedOption.value = 'ship'
    expect(first.selectedOption.value).toBe('ship')

    // The store is keyed by run:gate — a different gate starts unselected.
    const otherGate = useHitlGateState('run-1', 'gate-2')
    expect(otherGate.selectedOption.value).toBeNull()
  })

  it('drops the selected option when the gate session is cleared', () => {
    const state = useHitlGateState('run-1', 'gate-1')
    state.selectedOption.value = 'hold'
    expect(state.selectedOption.value).toBe('hold')

    state.clear()
    expect(state.selectedOption.value).toBeNull()
  })

  it('resets every gate selection on resetHitlGateState', () => {
    const first = useHitlGateState('run-1', 'gate-1')
    const second = useHitlGateState('run-2', 'gate-1')
    first.selectedOption.value = 'ship'
    second.selectedOption.value = 'hold'

    resetHitlGateState()
    expect(first.selectedOption.value).toBeNull()
    expect(second.selectedOption.value).toBeNull()
  })
})
