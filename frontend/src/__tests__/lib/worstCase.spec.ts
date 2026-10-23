import { describe, it, expect } from 'vitest'
import {
  attemptElapsedRows,
  formatCompactDuration,
  parseAttemptNumber,
  retryAttemptsForEvent,
  worstCaseForEvent,
} from '../../lib/worstCase'

// FAR-2072 — the timeout ceiling is `timeout_seconds x attempts`, where the
// attempt count comes from the pipeline's ACTUAL retry policy. These tests pin
// the four policy shapes the ticket calls out (no retries, one retryable event,
// several retryable events, and a policy that does not retry `timeout`).
describe('retryAttemptsForEvent', () => {
  it('returns one attempt when there is no policy at all', () => {
    expect(retryAttemptsForEvent(null, 'timeout')).toBe(1)
    expect(retryAttemptsForEvent(undefined, 'timeout')).toBe(1)
    expect(retryAttemptsForEvent({}, 'timeout')).toBe(1)
  })

  it('returns one attempt when the retry budget is zero', () => {
    expect(retryAttemptsForEvent({ on: ['timeout'], max_retries: 0 }, 'timeout')).toBe(1)
  })

  it('adds exactly one retry for a single retryable event', () => {
    expect(retryAttemptsForEvent({ on: ['timeout'], max_retries: 1 }, 'timeout')).toBe(2)
  })

  it('adds every configured retry for several retryable events', () => {
    const policy = { on: ['timeout', 'failure'], max_retries: 3 }
    expect(retryAttemptsForEvent(policy, 'timeout')).toBe(4)
    expect(retryAttemptsForEvent(policy, 'failure')).toBe(4)
    expect(retryAttemptsForEvent(policy, 'stall')).toBe(1)
  })

  it('treats an absent or null `on` as all retryable events', () => {
    expect(retryAttemptsForEvent({ max_retries: 2 }, 'timeout')).toBe(3)
    expect(retryAttemptsForEvent({ on: null, max_retries: 2 }, 'timeout')).toBe(3)
  })

  it('does not multiply a timeout when the policy retries only failure', () => {
    // Branch Fixer's real shape: retries a plain failure, never a timeout.
    const policy = { on: ['failure'], max_retries: 3 }
    expect(retryAttemptsForEvent(policy, 'timeout')).toBe(1)
    expect(worstCaseForEvent(policy, 55 * 60)).toEqual({
      attempts: 1,
      timeoutSeconds: 3300,
      totalSeconds: 3300,
    })
  })

  it('fails closed on an explicit empty or malformed event list', () => {
    expect(retryAttemptsForEvent({ on: [], max_retries: 3 }, 'timeout')).toBe(1)
    expect(retryAttemptsForEvent({ on: 'timeout', max_retries: 3 }, 'timeout')).toBe(1)
  })

  it('fails closed on an out-of-range or non-integer budget', () => {
    expect(retryAttemptsForEvent({ max_retries: 6 }, 'timeout')).toBe(1)
    expect(retryAttemptsForEvent({ max_retries: 1.5 }, 'timeout')).toBe(1)
    expect(retryAttemptsForEvent({ max_retries: true }, 'timeout')).toBe(1)
    expect(retryAttemptsForEvent({ max_retries: '2' }, 'timeout')).toBe(1)
  })
})

describe('worstCaseForEvent', () => {
  it('multiplies timeout by the policy-derived attempt count', () => {
    expect(worstCaseForEvent({ on: ['timeout'], max_retries: 2 }, 55 * 60)).toEqual({
      attempts: 3,
      timeoutSeconds: 3300,
      totalSeconds: 9900,
    })
  })

  it('returns null when the timeout is missing or non-positive', () => {
    expect(worstCaseForEvent({ on: ['timeout'], max_retries: 2 }, null)).toBeNull()
    expect(worstCaseForEvent({ on: ['timeout'], max_retries: 2 }, 0)).toBeNull()
    expect(worstCaseForEvent({ on: ['timeout'], max_retries: 2 }, -5)).toBeNull()
  })
})

describe('formatCompactDuration', () => {
  it('formats the ticket example and common durations', () => {
    expect(formatCompactDuration(55 * 60)).toBe('55m')
    expect(formatCompactDuration(165 * 60)).toBe('2h45m')
    expect(formatCompactDuration(90)).toBe('1m30s')
    expect(formatCompactDuration(45)).toBe('45s')
    expect(formatCompactDuration(7200)).toBe('2h')
  })

  it('keeps the seconds tail when an hour duration has one', () => {
    expect(formatCompactDuration(9930)).toBe('2h45m30s')
    expect(formatCompactDuration(7230)).toBe('2h30s')
  })
})

describe('attemptElapsedRows', () => {
  const start = '2026-10-21T10:00:00Z'
  const dispatched = '2026-10-21T10:55:00Z'
  const completed = '2026-10-21T11:50:00Z'

  it('reports a single attempt as started -> completed', () => {
    const rows = attemptElapsedRows({ attempts: 1, startedAt: start, completedAt: completed })
    expect(rows).toEqual([{ attempt: 1, seconds: 6600 }])
  })

  it('uses dispatched_at to split the first and latest attempt', () => {
    const rows = attemptElapsedRows({
      attempts: 2,
      startedAt: start,
      dispatchedAt: dispatched,
      completedAt: completed,
    })
    expect(rows).toEqual([
      { attempt: 1, seconds: 3300 },
      { attempt: 2, seconds: 3300 },
    ])
  })

  it('leaves intermediate attempts unrecorded rather than guessing', () => {
    const rows = attemptElapsedRows({
      attempts: 3,
      startedAt: start,
      dispatchedAt: dispatched,
      completedAt: completed,
    })
    expect(rows).toEqual([
      { attempt: 1, seconds: 3300 },
      { attempt: 2, seconds: null },
      { attempt: 3, seconds: 3300 },
    ])
  })

  it('uses the supplied clock for a still-running latest attempt', () => {
    const now = Date.parse('2026-10-21T11:05:00Z')
    const rows = attemptElapsedRows({ attempts: 1, startedAt: start, now })
    expect(rows).toEqual([{ attempt: 1, seconds: 3900 }])
  })

  it('accepts epoch-millisecond instants as well as ISO strings', () => {
    const startedAt = Date.parse(start)
    const completedAt = startedAt + 90_000
    const rows = attemptElapsedRows({ attempts: 1, startedAt, completedAt })
    expect(rows).toEqual([{ attempt: 1, seconds: 90 }])
  })

  it('leaves an unparseable instant unrecorded rather than rendering NaN', () => {
    const rows = attemptElapsedRows({
      attempts: 1,
      startedAt: 'not-a-date',
      completedAt: completed,
    })
    expect(rows).toEqual([{ attempt: 1, seconds: null }])
  })

  it('coerces a missing or zero attempt count to a single attempt', () => {
    const rows = attemptElapsedRows({ attempts: 0, startedAt: start, completedAt: completed })
    expect(rows).toEqual([{ attempt: 1, seconds: 6600 }])
  })
})

describe('parseAttemptNumber', () => {
  it('reads the trailing attempt number from an attempt key', () => {
    expect(parseAttemptNumber('run:abc:node:node-a:3')).toBe(3)
  })

  it('returns null for a key without a numeric suffix', () => {
    expect(parseAttemptNumber('__final__')).toBeNull()
    expect(parseAttemptNumber(undefined)).toBeNull()
  })

  it('returns null for a non-positive numeric suffix', () => {
    expect(parseAttemptNumber('run:abc:node:node-a:0')).toBeNull()
  })
})
