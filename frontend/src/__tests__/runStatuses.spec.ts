import { describe, it, expect } from 'vitest'
import { TERMINAL_STATUSES, NON_TERMINAL_STATUSES, isTerminalStatus, isNonTerminalStatus, HITL_ACTIONABLE_STATUSES, isHitlActionableStatus } from '../constants/runStatuses'

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

describe('isHitlActionableStatus', () => {
  it('classifies a claimed run as HITL-actionable (a claim is not a decision)', () => {
    expect(isHitlActionableStatus('claimed')).toBe(true)
  })

  it('classifies every HITL_ACTIONABLE_STATUSES entry as actionable', () => {
    for (const status of HITL_ACTIONABLE_STATUSES) {
      expect(isHitlActionableStatus(status)).toBe(true)
    }
  })

  it('does not classify terminal or non-HITL running statuses as actionable', () => {
    for (const status of [...TERMINAL_STATUSES, 'pending', 'running', 'unknown']) {
      expect(isHitlActionableStatus(status)).toBe(false)
    }
  })
})
