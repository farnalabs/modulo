import { describe, it, expect } from 'vitest'
import {
  errorCodeDescription,
  errorCodeLabel,
  isQueueExpiredRun,
  isSupersededRun,
  runStatusBadgeClass,
  runStatusDescription,
  runStatusLabel,
} from '../utils/runUtils'

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

// FAR-1883: a capacity-queue timeout ends the run `rejected` with error_code
// `queue.expired`. It is a SUPERSESSION (bookkeeping), never a human "no" and
// never a failure — so it must read as its own superseded-style label, not the
// plain `rejected` status, and its error-code copy must resolve.
describe('queue-expiry run labelling (FAR-1883)', () => {
  const t = (key: string) =>
    key === 'statusDescriptions.queue_expired'
      ? 'QUEUE EXPIRED DESC'
      : key === 'statusDescriptions.superseded'
        ? 'SUPERSEDED DESC'
        : key === 'statusDescriptions.rejected'
          ? 'REJECTED DESC'
          : key === 'errorCodes.queue.expired'
            ? 'QUEUE EXPIRED LABEL'
            : key === 'errorCodeDescriptions.queue.expired'
              ? 'QUEUE EXPIRED ERROR DESC'
              : key

  it('detects only rejected runs carrying queue.expired', () => {
    expect(isQueueExpiredRun('rejected', 'queue.expired')).toBe(true)
    expect(isQueueExpiredRun('rejected', 'hitl.superseded')).toBe(false)
    expect(isQueueExpiredRun('rejected', 'hitl.rejected')).toBe(false)
    expect(isQueueExpiredRun('rejected', null)).toBe(false)
    expect(isQueueExpiredRun('failed', 'queue.expired')).toBe(false)
  })

  it('includes queue.expired in the broadband supersession predicate', () => {
    expect(isSupersededRun('rejected', 'queue.expired')).toBe(true)
    expect(isSupersededRun('rejected', 'hitl.superseded')).toBe(true)
    expect(isSupersededRun('rejected', 'hitl.rejected')).toBe(false)
    expect(isSupersededRun('rejected', null)).toBe(false)
    expect(isSupersededRun('failed', 'queue.expired')).toBe(false)
  })

  it('labels a queue-expired run as expired-while-queued, never plain rejected', () => {
    expect(runStatusLabel('rejected', 'queue.expired')).toBe('expired while queued')
    expect(runStatusLabel('rejected', 'queue.expired')).not.toBe('rejected')
  })

  it('describes a queue-expired run with its own copy, distinct from supersession', () => {
    expect(runStatusDescription('rejected', t, 'queue.expired')).toBe('QUEUE EXPIRED DESC')
    expect(runStatusDescription('rejected', t, 'hitl.superseded')).toBe('SUPERSEDED DESC')
  })

  it('keeps the expired-while-queued phrasing when the locale lacks the queue-expired description', () => {
    const untranslated = (key: string) => key
    expect(runStatusDescription('rejected', untranslated, 'queue.expired')).toBe('expired while queued')
  })

  it('resolves the queue.expired error-code label and description keys', () => {
    expect(errorCodeLabel('queue.expired', t)).toBe('QUEUE EXPIRED LABEL')
    expect(errorCodeDescription('queue.expired', t)).toBe('QUEUE EXPIRED ERROR DESC')
  })
})
