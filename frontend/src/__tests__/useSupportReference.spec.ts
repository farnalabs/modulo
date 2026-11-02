import { describe, it, expect, vi, afterEach } from 'vitest'
import {
  buildErrorGroupSupportReference,
  buildRunSupportReference,
  supportReferenceBaseUrl,
  useSupportReference,
} from '../composables/useSupportReference'
import enUS from '../locales/en-US.js'

/**
 * Resolve a real catalogue key so these tests fail if a support-reference
 * label is missing from en-US.js (the reference is user-facing text).
 */
function translate(key: string): string {
  let node: unknown = enUS
  for (const part of key.split('.')) {
    if (node === null || typeof node !== 'object') {
      throw new Error(`missing locale key: ${key}`)
    }
    node = (node as Record<string, unknown>)[part]
  }
  if (typeof node !== 'string') {
    throw new Error(`locale key is not a string: ${key}`)
  }
  return node
}

const BASE_URL = 'https://app.modulo.run'
const RUN_ID = '3f6c1a52-9d0e-4f1b-8a77-2b1c9e4d5a60'

describe('buildRunSupportReference', () => {
  it('renders the canonical run reference with every available field', () => {
    const reference = buildRunSupportReference(
      {
        run_id: RUN_ID,
        pipeline_id: 'pl-42',
        pipeline_name: 'Release pipeline',
        status: 'failed',
        error_code: 'provider.rate_limit',
      },
      { t: translate, baseUrl: BASE_URL },
    )

    expect(reference).toBe(
      `run ${RUN_ID} · pipeline pl-42 (Release pipeline) · status failed · error provider.rate_limit\n`
        + `url ${BASE_URL}/runs/${RUN_ID}`,
    )
  })

  it('carries the full run id, never a truncated id', () => {
    const reference = buildRunSupportReference({ run_id: RUN_ID }, { t: translate, baseUrl: BASE_URL })

    expect(reference).toContain(RUN_ID)
    expect(reference.split('\n')[0]).toBe(`run ${RUN_ID}`)
  })

  it('omits the pipeline name parentheses when the name is unavailable', () => {
    const reference = buildRunSupportReference(
      { run_id: RUN_ID, pipeline_id: 'pl-42', pipeline_name: null },
      { t: translate, baseUrl: BASE_URL },
    )

    expect(reference.split('\n')[0]).toBe(`run ${RUN_ID} · pipeline pl-42`)
  })

  it('omits the error segment rather than printing a placeholder', () => {
    const reference = buildRunSupportReference(
      { run_id: RUN_ID, pipeline_id: 'pl-42', status: 'complete', error_code: null },
      { t: translate, baseUrl: BASE_URL },
    )
    const details = reference.split('\n')[0]

    expect(details).toBe(`run ${RUN_ID} · pipeline pl-42 · status complete`)
    expect(details).not.toContain('error ')
    expect(reference).not.toContain('—')
  })

  it('omits the pipeline segment when the run carries no pipeline id', () => {
    const reference = buildRunSupportReference(
      { run_id: RUN_ID, pipeline_id: null, status: 'failed' },
      { t: translate, baseUrl: BASE_URL },
    )

    expect(reference.split('\n')[0]).toBe(`run ${RUN_ID} · status failed`)
  })

  it('returns an empty string when the run has no id', () => {
    expect(buildRunSupportReference({ run_id: null }, { t: translate, baseUrl: BASE_URL })).toBe('')
  })

  it('normalises a trailing slash on the base url', () => {
    const reference = buildRunSupportReference({ run_id: RUN_ID }, { t: translate, baseUrl: `${BASE_URL}/` })

    expect(reference.split('\n')[1]).toBe(`url ${BASE_URL}/runs/${RUN_ID}`)
  })
})

describe('buildErrorGroupSupportReference', () => {
  it('renders the canonical error-group reference with every available field', () => {
    const reference = buildErrorGroupSupportReference(
      {
        id: 'eg-7',
        fingerprint: 'sha256:abc123',
        status: 'new',
        level_peak: 'critical',
        count: 12,
      },
      { t: translate, baseUrl: BASE_URL },
    )

    expect(reference).toBe(
      'error eg-7 · fingerprint sha256:abc123 · status new · level critical · count 12\n'
        + `url ${BASE_URL}/admin/errors/eg-7`,
    )
  })

  it('omits unavailable fields rather than printing placeholders', () => {
    const reference = buildErrorGroupSupportReference(
      { id: 'eg-7', fingerprint: null, status: null, level_peak: null, count: null },
      { t: translate, baseUrl: BASE_URL },
    )

    expect(reference.split('\n')[0]).toBe('error eg-7')
    expect(reference).not.toContain('—')
  })

  it('keeps a zero occurrence count', () => {
    const reference = buildErrorGroupSupportReference(
      { id: 'eg-7', count: 0 },
      { t: translate, baseUrl: BASE_URL },
    )

    expect(reference.split('\n')[0]).toBe('error eg-7 · count 0')
  })

  it('carries the instance scope in the url when the group is instance-level', () => {
    const reference = buildErrorGroupSupportReference(
      { id: 'eg-7' },
      { t: translate, baseUrl: BASE_URL, scope: 'instance' },
    )

    expect(reference.split('\n')[1]).toBe(`url ${BASE_URL}/admin/errors/eg-7?scope=instance`)
  })

  it('returns an empty string when the group has no id', () => {
    expect(buildErrorGroupSupportReference({ id: '' }, { t: translate, baseUrl: BASE_URL })).toBe('')
  })
})

describe('supportReferenceBaseUrl', () => {
  it('falls back to the page origin when no base url is supplied', () => {
    expect(supportReferenceBaseUrl()).toBe(window.location.origin)
  })
})

describe('useSupportReference', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('copies the reference and reports success until the feedback window closes', async () => {
    vi.useFakeTimers()
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const { copied, copyFailed, copyReference } = useSupportReference()

    await expect(copyReference('run abc')).resolves.toBe(true)

    expect(writeText).toHaveBeenCalledWith('run abc')
    expect(copied.value).toBe(true)
    expect(copyFailed.value).toBe(false)

    vi.advanceTimersByTime(2000)
    expect(copied.value).toBe(false)
  })

  it('reports a refused clipboard instead of claiming success', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('clipboard blocked'))
    Object.assign(navigator, { clipboard: { writeText } })
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const { copied, copyFailed, copyReference } = useSupportReference()

    await expect(copyReference('run abc')).resolves.toBe(false)

    expect(copied.value).toBe(false)
    expect(copyFailed.value).toBe(true)
    expect(warn).toHaveBeenCalled()
  })

  it('does not touch the clipboard for an empty reference', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const { copyReference } = useSupportReference()

    await expect(copyReference('')).resolves.toBe(false)
    expect(writeText).not.toHaveBeenCalled()
  })
})
