import { describe, it, expect } from 'vitest'
import {
  errorGroupRowId,
  formatErrorAssignee,
  formatErrorDate,
  levelBadgeClass,
  statusBadgeClass,
} from '../utils/errorGroupPresentation'

describe('error group presentation helpers', () => {
  it('maps every peak level to its badge class', () => {
    expect(levelBadgeClass('critical')).toBe('badge badge-status-destructive')
    expect(levelBadgeClass('warning')).toBe('badge badge-status-warning')
    expect(levelBadgeClass('error')).toBe('badge badge-context-blue')
    expect(levelBadgeClass('unknown')).toBe('badge badge-context-blue')
  })

  it('maps every triage status to its badge class', () => {
    expect(statusBadgeClass('new')).toBe('badge badge-status-destructive')
    expect(statusBadgeClass('acknowledged')).toBe('badge badge-status-warning')
    expect(statusBadgeClass('resolved')).toBe('badge badge-status-success')
    expect(statusBadgeClass('archived')).toBe('badge badge-status-muted')
    expect(statusBadgeClass('other')).toBe('badge')
  })

  it('formats a timestamp and falls back to an em dash when absent', () => {
    const formatted = formatErrorDate('2026-10-20T12:34:00Z')
    expect(formatted).not.toBe('—')
    expect(formatted).toContain('Oct')
    expect(formatErrorDate(null)).toBe('—')
    expect(formatErrorDate(undefined)).toBe('—')
    expect(formatErrorDate('')).toBe('—')
    expect(formatErrorDate('not-a-date')).toBe('—')
  })

  it('stringifies a row id and tolerates a missing id', () => {
    expect(errorGroupRowId({ id: 'eg-1' })).toBe('eg-1')
    expect(errorGroupRowId({ id: 7 })).toBe('7')
    expect(errorGroupRowId({})).toBe('')
    expect(errorGroupRowId({ id: null })).toBe('')
  })

  it('shortens a string assignee and shows an em dash otherwise', () => {
    expect(formatErrorAssignee({ assigned_to: 'abcdef123456' })).toBe('#abcdef12')
    expect(formatErrorAssignee({ assigned_to: '' })).toBe('—')
    expect(formatErrorAssignee({ assigned_to: null })).toBe('—')
    expect(formatErrorAssignee({ assigned_to: 42 })).toBe('—')
    expect(formatErrorAssignee({})).toBe('—')
  })
})
