import { describe, it, expect } from 'vitest'
import {
  DEFAULT_DATE_RANGE_DAYS,
  dateRangeQuery,
  defaultDateRange,
  formatDateInput,
  formatDateLogTimestamp,
} from '../lib/formatDate'

describe('formatDateInput', () => {
  it('formats a local calendar date as YYYY-MM-DD with zero padding', () => {
    expect(formatDateInput(new Date(2026, 0, 5, 23, 59))).toBe('2026-01-05')
    expect(formatDateInput(new Date(2026, 11, 31, 0, 0))).toBe('2026-12-31')
  })
})

describe('defaultDateRange', () => {
  it('spans the last 30 calendar days including today', () => {
    // Local noon: the local calendar date is stable in any timezone.
    const now = new Date(2026, 10, 4, 12, 0, 0)
    expect(defaultDateRange(now)).toEqual({ from: '2026-10-06', to: '2026-11-04' })
    expect(DEFAULT_DATE_RANGE_DAYS).toBe(30)
  })

  it('crosses a month and year boundary correctly', () => {
    const now = new Date(2026, 0, 1, 12, 0, 0)
    // Jan 1 minus 29 days is Dec 3 (not Dec 2, which a 30-day subtraction gives).
    expect(defaultDateRange(now)).toEqual({ from: '2025-12-03', to: '2026-01-01' })
  })
})

describe('formatDateLogTimestamp', () => {
  it('formats valid timestamps with time', () => {
    expect(formatDateLogTimestamp('2024-03-15T14:05:00Z')).toContain('Mar')
    expect(formatDateLogTimestamp('2024-03-15T14:05:00Z')).toContain('15')
  })

  it('returns an em dash for invalid timestamps', () => {
    expect(formatDateLogTimestamp('not-a-date')).toBe('—')
    expect(formatDateLogTimestamp(null)).toBe('—')
  })
})

describe('dateRangeQuery', () => {
  it('keeps both bounds when a range is selected', () => {
    expect(dateRangeQuery('2026-09-01', '2026-09-30')).toEqual({ from: '2026-09-01', to: '2026-09-30' })
  })

  it('omits a cleared bound instead of sending an empty string', () => {
    expect(Object.keys(dateRangeQuery('', ''))).toHaveLength(0)
    expect(Object.keys(dateRangeQuery(null, undefined))).toHaveLength(0)
    expect(dateRangeQuery('2026-09-01', '')).toEqual({ from: '2026-09-01' })
    expect(dateRangeQuery('', '2026-09-30')).toEqual({ to: '2026-09-30' })
  })
})
