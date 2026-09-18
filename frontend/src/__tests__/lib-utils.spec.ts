import { describe, it, expect, vi, afterEach } from 'vitest'
import { currencySymbolFor, formatMoney } from '../lib/money'
import { formatDateShort, formatDateShortWithTime, formatDateFilename } from '../lib/formatDate'
import { withTimeout, asErrorMessage } from '../lib/asyncUtils'
import {
  formatApiError,
  isProblemDetail,
  getProblemTypeLabel,
  toProblemDetail,
  throwOnError,
  type ProblemDetail,
} from '../lib/api/formatError'
import { shortId, formatRun } from '../utils/format'
import { runStatusBadgeClass, formatRunDate } from '../utils/runUtils'

afterEach(() => {
  vi.useRealTimers()
})

describe('currencySymbolFor', () => {
  it('maps known currency codes to their symbols', () => {
    expect(currencySymbolFor('USD')).toBe('$')
    expect(currencySymbolFor('EUR')).toBe('€')
    expect(currencySymbolFor('GBP')).toBe('£')
  })

  it('normalises lowercase and whitespace-padded codes', () => {
    expect(currencySymbolFor('eur')).toBe('€')
    expect(currencySymbolFor('  gbp ')).toBe('£')
  })

  it('falls back to the dollar symbol for unknown codes', () => {
    expect(currencySymbolFor('JPY')).toBe('$')
    expect(currencySymbolFor('')).toBe('$')
  })

  it('falls back to the dollar symbol for nullish or non-string input', () => {
    expect(currencySymbolFor(null)).toBe('$')
    expect(currencySymbolFor(undefined)).toBe('$')
  })
})

describe('formatMoney', () => {
  it('formats an amount with the default two decimals', () => {
    expect(formatMoney(1234.5)).toBe('$1,234.50')
  })

  it('respects the digits parameter', () => {
    expect(formatMoney(1234.5, 'USD', 0)).toBe('$1,235')
    expect(formatMoney(1.005, 'USD', 3)).toBe('$1.005')
  })

  it('uses the supplied currency code', () => {
    expect(formatMoney(10, 'EUR')).toBe('€10.00')
    expect(formatMoney(10, 'GBP')).toBe('£10.00')
  })

  it('normalises lowercase currency codes', () => {
    expect(formatMoney(10, 'eur')).toBe('€10.00')
  })

  it('falls back to USD when the currency is missing', () => {
    expect(formatMoney(10, null)).toBe('$10.00')
    expect(formatMoney(10, undefined)).toBe('$10.00')
    expect(formatMoney(10, '')).toBe('$10.00')
  })

  it('treats non-finite amounts as zero', () => {
    expect(formatMoney(Number.NaN)).toBe('$0.00')
    expect(formatMoney(Number.POSITIVE_INFINITY)).toBe('$0.00')
    expect(formatMoney(Number.NEGATIVE_INFINITY)).toBe('$0.00')
  })

  it('renders the raw code when the currency is not a known Intl currency', () => {
    expect(formatMoney(5, 'ZZZ')).toMatch(/^ZZZ[\s\u00a0]5\.00$/)
  })
})

describe('formatDateShort / formatDateShortWithTime / formatDateFilename', () => {
  it('renders the short date', () => {
    expect(formatDateShort('2024-03-15T12:00:00Z')).toBe('Mar 15, 2024')
  })

  it('renders the short date with time', () => {
    expect(formatDateShortWithTime('2024-03-15T14:05:00Z')).toBe('Mar 15, 2024, 2:05 PM')
  })

  it('renders the filename date', () => {
    expect(formatDateFilename('2024-03-15T12:00:00Z')).toBe('2024-03-15')
  })

  it('returns an em dash for nullish input', () => {
    expect(formatDateShort(null)).toBe('—')
    expect(formatDateShort(undefined)).toBe('—')
    expect(formatDateShortWithTime(null)).toBe('—')
    expect(formatDateFilename(null)).toBe('—')
  })

  it('returns an em dash for invalid dates', () => {
    expect(formatDateShort('not-a-date')).toBe('—')
    expect(formatDateShort(Number.NaN)).toBe('—')
    expect(formatDateShortWithTime('not-a-date')).toBe('—')
    expect(formatDateFilename('not-a-date')).toBe('—')
  })

  it('accepts Date objects and numeric timestamps', () => {
    const d = new Date('2024-03-15T12:00:00Z') // nosemgrep: new-date-without-guard
    expect(formatDateShort(d)).toBe('Mar 15, 2024')
    expect(formatDateShort(d.getTime())).toBe('Mar 15, 2024')
  })
})

describe('withTimeout', () => {
  it('resolves with the promise value when it wins the race', async () => {
    const promise = Promise.resolve('value')
    await expect(withTimeout(promise, 1000, 'op')).resolves.toBe('value')
  })

  it('rejects when the timeout elapses first', async () => {
    vi.useFakeTimers()
    const never = new Promise<string>(() => {})
    const p = withTimeout(never, 1000, 'op')
    const assertion = expect(p).rejects.toThrow('op timed out after 1000ms')
    vi.advanceTimersByTime(1000)
    await assertion
  })

  it('propagates the original rejection when the promise loses', async () => {
    const promise = Promise.reject(new Error('boom'))
    await expect(withTimeout(promise, 1000, 'op')).rejects.toThrow('boom')
  })

  it('uses the label in the timeout message', async () => {
    vi.useFakeTimers()
    const never = new Promise<string>(() => {})
    const p = withTimeout(never, 500, 'fetch-pipelines')
    const assertion = expect(p).rejects.toThrow('fetch-pipelines timed out after 500ms')
    vi.advanceTimersByTime(500)
    await assertion
  })
})

describe('asErrorMessage', () => {
  it('delegates to formatApiError for strings', () => {
    expect(asErrorMessage('plain message')).toBe('plain message')
  })

  it('delegates to formatApiError for Error instances', () => {
    expect(asErrorMessage(new Error('wrapped'))).toBe('wrapped')
  })

  it('delegates to formatApiError for problem details', () => {
    expect(asErrorMessage({ detail: 'detail text', title: 'x', status: 400, type: 't' })).toBe('detail text')
  })
})

describe('getProblemTypeLabel', () => {
  it('maps known problem types to human labels', () => {
    expect(getProblemTypeLabel('validation_error')).toBe('Validation Error')
    expect(getProblemTypeLabel('forbidden')).toBe('Forbidden')
    expect(getProblemTypeLabel('rate_limited')).toBe('Rate Limited')
  })

  it('strips the urn prefix before resolving', () => {
    expect(getProblemTypeLabel('urn:problem:modulo:conflict')).toBe('Conflict')
  })

  it('returns a generic label for unknown types', () => {
    expect(getProblemTypeLabel('mystery')).toBe('Error')
    expect(getProblemTypeLabel('')).toBe('Error')
  })
})

describe('isProblemDetail', () => {
  const valid: ProblemDetail = { type: 't', title: 'T', status: 400, detail: 'd' }

  it('accepts a well-formed problem detail', () => {
    expect(isProblemDetail(valid)).toBe(true)
  })

  it('rejects null and non-objects', () => {
    expect(isProblemDetail(null)).toBe(false)
    expect(isProblemDetail('string')).toBe(false)
    expect(isProblemDetail(42)).toBe(false)
    expect(isProblemDetail(undefined)).toBe(false)
  })

  it('rejects objects missing required fields', () => {
    expect(isProblemDetail({ type: 't' })).toBe(false)
    expect(isProblemDetail({ ...valid, status: '400' })).toBe(false)
    expect(isProblemDetail({ ...valid, title: 1 })).toBe(false)
    expect(isProblemDetail({ ...valid, detail: undefined })).toBe(false)
  })
})

describe('toProblemDetail', () => {
  it('returns problem details unchanged', () => {
    const pd: ProblemDetail = { type: 't', title: 'T', status: 400, detail: 'd' }
    expect(toProblemDetail(pd)).toBe(pd)
  })

  it('wraps an Error into a problem detail whose detail is the message', () => {
    const result = toProblemDetail(new Error('inner message'))
    expect(result.type).toBe('urn:problem:modulo:unknown')
    expect(result.title).toBe('Error')
    expect(result.status).toBe(0)
    expect(result.detail).toBe('inner message')
  })

  it('wraps a string into a problem detail', () => {
    const result = toProblemDetail('raw string')
    expect(result.detail).toBe('raw string')
    expect(result.title).toBe('Error')
  })
})

describe('throwOnError', () => {
  it('returns data when there is no error', () => {
    expect(throwOnError({ data: { id: 1 } })).toEqual({ id: 1 })
  })

  it('throws a formatted Error when result.error is present', () => {
    expect(() => throwOnError({ error: new Error('kaput') })).toThrow('kaput')
  })

  it('throws when error is a problem detail', () => {
    expect(() => throwOnError({ error: { type: 't', title: 'T', status: 400, detail: 'bad body' } })).toThrow('bad body')
  })
})

describe('formatApiError', () => {
  it('returns the detail of a problem detail', () => {
    expect(formatApiError({ type: 't', title: 'T', status: 400, detail: 'the detail' })).toBe('the detail')
  })

  it('returns strings as-is', () => {
    expect(formatApiError('direct')).toBe('direct')
  })

  it('returns Error messages', () => {
    expect(formatApiError(new Error('err msg'))).toBe('err msg')
  })

  it('returns a generic message for nullish and non-string falsy input', () => {
    expect(formatApiError(undefined)).toBe('Unknown error')
    expect(formatApiError(null)).toBe('Unknown error')
    expect(formatApiError(0)).toBe('Unknown error')
    expect(formatApiError(false)).toBe('Unknown error')
  })

  it('passes empty strings through as-is', () => {
    expect(formatApiError('')).toBe('')
  })

  it('prefers detail then message then error then title on plain objects', () => {
    expect(formatApiError({ detail: 'd' })).toBe('d')
    expect(formatApiError({ message: 'm' })).toBe('m')
    expect(formatApiError({ error: 'e' })).toBe('e')
    expect(formatApiError({ title: 't' })).toBe('t')
  })

  it('falls back to JSON for arbitrary objects', () => {
    expect(formatApiError({ code: 42 })).toBe('{"code":42}')
  })

  it('stringifies unknown primitives', () => {
    expect(formatApiError(99)).toBe('99')
  })
})

describe('shortId', () => {
  it('prefixes the first eight characters with a hash', () => {
    expect(shortId('abcdefghijklmnop')).toBe('#abcdefgh')
  })

  it('handles ids shorter than eight characters', () => {
    expect(shortId('ab')).toBe('#ab')
  })

  it('returns an em dash for nullish ids', () => {
    expect(shortId(null)).toBe('—')
    expect(shortId(undefined)).toBe('—')
    expect(shortId('')).toBe('—')
  })
})

describe('formatRun', () => {
  it('returns an em dash for nullish runs', () => {
    expect(formatRun(null)).toBe('—')
    expect(formatRun(undefined)).toBe('—')
  })

  it('formats pipeline name with run number', () => {
    expect(formatRun({ pipeline_name: 'Pipelines/Data Sync', run_number: 7 })).toBe('Pipelines/Data Sync #7')
  })

  it('uses a short run id when run_number is missing', () => {
    expect(formatRun({ pipeline_name: 'Pipelines/Job', run_id: 'r_abcdefgh1234' })).toBe('Pipelines/Job #r_abcdef')
  })

  it('defaults the name to Run when pipeline_name is missing', () => {
    expect(formatRun({ run_number: 3 })).toBe('Run #3')
    expect(formatRun({ run_id: 'r_abcdefgh' })).toBe('Run #r_abcdef')
  })
})

describe('runStatusBadgeClass', () => {
  it('maps known statuses to their badge classes', () => {
    expect(runStatusBadgeClass('complete')).toBe('bg-success/10 text-success')
    expect(runStatusBadgeClass('failed')).toBe('bg-destructive/10 text-destructive')
    expect(runStatusBadgeClass('stalled')).toBe('bg-destructive/10 text-destructive')
    expect(runStatusBadgeClass('budget_exceeded')).toBe('bg-destructive/10 text-destructive')
    expect(runStatusBadgeClass('running')).toBe('bg-primary/10 text-primary')
    expect(runStatusBadgeClass('awaiting_human')).toBe('bg-warning/10 text-warning')
    expect(runStatusBadgeClass('cancelled')).toBe('bg-muted text-muted-foreground')
    expect(runStatusBadgeClass('eval_failed')).toBe('bg-destructive/10 text-destructive')
    expect(runStatusBadgeClass('claimed')).toBe('bg-warning/10 text-warning')
    expect(runStatusBadgeClass('waiting_for_lock')).toBe('bg-muted text-muted-foreground')
  })

  it('uses the muted fallback for unknown statuses', () => {
    expect(runStatusBadgeClass('bogus')).toBe('bg-muted text-muted-foreground')
    expect(runStatusBadgeClass('')).toBe('bg-muted text-muted-foreground')
  })
})

describe('formatRunDate', () => {
  it('returns an em dash for nullish input', () => {
    expect(formatRunDate(null)).toBe('—')
    expect(formatRunDate('')).toBe('—')
  })

  it('returns the raw string for unparseable dates', () => {
    expect(formatRunDate('garbage')).toBe('garbage')
  })

  it('formats valid ISO timestamps', () => {
    const result = formatRunDate('2024-03-15T14:05:00')
    expect(result).toMatch(/Mar/)
    expect(result).toMatch(/15/)
  })
})
