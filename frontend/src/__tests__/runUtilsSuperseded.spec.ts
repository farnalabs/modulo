import { describe, it, expect } from 'vitest'
import { isSupersededRun, runStatusBadgeClass, runStatusDescription, runStatusLabel } from '../utils/runUtils'

// FAR-1487 (Roddy): a coalesced-supersede run ends `rejected` with
// error_code `hitl.superseded` and must NEVER read as a human "no".
describe('rejected / superseded run labelling (FAR-1487)', () => {
  const t = (key: string) => (key === 'statusDescriptions.superseded' ? 'SUPERSEDED DESC' : key === 'statusDescriptions.rejected' ? 'REJECTED DESC' : key)

  it('detects only rejected runs carrying hitl.superseded', () => {
    expect(isSupersededRun('rejected', 'hitl.superseded')).toBe(true)
    expect(isSupersededRun('rejected', 'hitl.rejected')).toBe(false)
    expect(isSupersededRun('rejected', null)).toBe(false)
    expect(isSupersededRun('failed', 'hitl.superseded')).toBe(false)
  })

  it('labels a superseded run as superseded, not rejected', () => {
    expect(runStatusLabel('rejected', 'hitl.superseded')).toBe('superseded by a newer version')
    expect(runStatusLabel('rejected', 'hitl.rejected')).toBe('rejected')
    expect(runStatusLabel('rejected')).toBe('rejected')
  })

  it('describes a superseded run with its own copy', () => {
    expect(runStatusDescription('rejected', t, 'hitl.superseded')).toBe('SUPERSEDED DESC')
    expect(runStatusDescription('rejected', t, 'hitl.rejected')).toBe('REJECTED DESC')
  })

  it('keeps the superseded phrasing when the locale lacks the superseded description', () => {
    const untranslated = (key: string) => key
    expect(runStatusDescription('rejected', untranslated, 'hitl.superseded')).toBe('superseded by a newer version')
    expect(runStatusDescription('rejected', untranslated, 'hitl.rejected')).toBe('rejected')
  })

  it('renders rejected as a neutral (non-destructive) badge', () => {
    expect(runStatusBadgeClass('rejected')).toBe('bg-muted text-muted-foreground')
  })
})
