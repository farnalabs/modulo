import { describe, it, expect, afterEach } from 'vitest'
import {
  buildErrorGroupSupportReference,
  buildRunSupportReference,
} from '../lib/supportReference'

const RUN_ID = '1f0c0d6e-6a0b-4f1e-9d1a-0c9b8a7f6e5d'
const PIPELINE_ID = '2a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d'

const FAILED_RUN = {
  runId: RUN_ID,
  pipelineId: PIPELINE_ID,
  pipelineName: 'PR Reviewer',
  status: 'failed',
  errorCode: 'node_deadline_exceeded',
}

describe('buildRunSupportReference', () => {
  const originalLocation = window.location

  afterEach(() => {
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: originalLocation,
    })
  })

  it('carries the FULL run id and the error code for a failed run', () => {
    const text = buildRunSupportReference(FAILED_RUN, 'https://control.farna.dev')

    // The two fields a support engineer cannot reconstruct from anything else.
    expect(text).toContain(RUN_ID)
    expect(text).toContain('error node_deadline_exceeded')
  })

  it('emits the canonical run shape, one field group per line', () => {
    const text = buildRunSupportReference(FAILED_RUN, 'https://control.farna.dev')

    expect(text.split('\n')).toEqual([
      `run ${RUN_ID} · pipeline ${PIPELINE_ID} (PR Reviewer) · status failed · error node_deadline_exceeded`,
      `url https://control.farna.dev/runs/${RUN_ID}`,
    ])
  })

  it('omits the parenthetical name when the pipeline name is unknown', () => {
    const text = buildRunSupportReference(
      { ...FAILED_RUN, pipelineName: null },
      'https://control.farna.dev',
    )

    expect(text).toContain(`pipeline ${PIPELINE_ID} ·`)
    expect(text).not.toContain('()')
  })

  it('reads error none for a run that did not fail', () => {
    const text = buildRunSupportReference(
      { ...FAILED_RUN, status: 'complete', errorCode: null },
      'https://control.farna.dev',
    )

    expect(text).toContain('status complete')
    expect(text).toContain('error none')
  })

  it('derives the url from the app origin when none is supplied', () => {
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { origin: 'https://self-hosted.example/' },
    })

    const text = buildRunSupportReference(FAILED_RUN)

    expect(text).toContain(`url https://self-hosted.example/runs/${RUN_ID}`)
  })

  it('drops a duplicate trailing slash from the supplied origin', () => {
    const text = buildRunSupportReference(FAILED_RUN, 'https://control.farna.dev/')

    expect(text).toContain(`url https://control.farna.dev/runs/${RUN_ID}`)
  })

  it('omits the url line when no origin is available', () => {
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: {},
    })

    const text = buildRunSupportReference(FAILED_RUN)

    expect(text).not.toContain('url ')
    expect(text.split('\n')).toHaveLength(1)
  })
})

describe('buildErrorGroupSupportReference', () => {
  it('emits the canonical error-group shape', () => {
    const text = buildErrorGroupSupportReference({
      groupId: 'eg-1234',
      fingerprint: 'abc123def456',
      level: 'error',
      count: 7,
      source: 'saq',
    })

    expect(text).toBe(
      'error eg-1234 · fingerprint abc123def456 · level error · count 7 · source saq',
    )
  })

  it('omits the source segment when the group has no source', () => {
    const text = buildErrorGroupSupportReference({
      groupId: 'eg-1234',
      fingerprint: 'abc123def456',
      level: 'warning',
      count: 2,
    })

    expect(text).toBe(
      'error eg-1234 · fingerprint abc123def456 · level warning · count 2',
    )
    expect(text).not.toContain('source')
  })
})
