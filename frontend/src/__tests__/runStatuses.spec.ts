import { describe, it, expect } from 'vitest'
import { TERMINAL_STATUSES, NON_TERMINAL_STATUSES, isTerminalStatus, isNonTerminalStatus } from '../constants/runStatuses'

describe('isTerminalStatus', () => {
  it('classifies budget_exceeded as terminal', () => {
    expect(isTerminalStatus('budget_exceeded')).toBe(true)
  })

  it('classifies every TERMINAL_STATUSES entry as terminal', () => {
    for (const status of TERMINAL_STATUSES) {
      expect(isTerminalStatus(status)).toBe(true)
    }
  })

  it('does not classify non-terminal statuses as terminal', () => {
    for (const status of NON_TERMINAL_STATUSES) {
      expect(isTerminalStatus(status)).toBe(false)
    }
  })
})

describe('isNonTerminalStatus', () => {
  it('does not classify budget_exceeded as non-terminal', () => {
    expect(isNonTerminalStatus('budget_exceeded')).toBe(false)
  })

  it('classifies every NON_TERMINAL_STATUSES entry as non-terminal', () => {
    for (const status of NON_TERMINAL_STATUSES) {
      expect(isNonTerminalStatus(status)).toBe(true)
    }
  })
})
