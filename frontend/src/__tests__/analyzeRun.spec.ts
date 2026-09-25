import { describe, it, expect } from 'vitest'
import { createI18n } from 'vue-i18n'
import enUS from '../locales/en-US.js'
import {
  ANALYZABLE_FAILURE_STATUSES,
  buildAnalyzeSeedMessage,
  isAnalyzableFailure,
  MAX_ERROR_DETAIL_CHARS,
  pipelineLabel,
  runLabel,
  type AnalyzeRunInfo,
  type TranslateFn,
} from '../components/runs/analyzeRun'

const i18n = createI18n({
  legacy: false,
  locale: 'en-US',
  messages: { 'en-US': enUS },
})
const t = i18n.global.t as unknown as TranslateFn

function run(overrides: Partial<AnalyzeRunInfo> = {}): AnalyzeRunInfo {
  return {
    runId: 'a1b2c3d4-e5f6-7890-abcd-ef1234567890',
    runNumber: 42,
    pipelineId: 'pipe-1',
    pipelineName: 'Deploy pipeline',
    status: 'failed',
    errorCode: 'harness.worker_failed',
    errorDetail: 'node "build" exited with code 1',
    failingNode: 'build',
    ...overrides,
  }
}

describe('analyzable failure statuses', () => {
  it('treats every terminal failure status as analyzable', () => {
    for (const status of ANALYZABLE_FAILURE_STATUSES) {
      expect(isAnalyzableFailure(status)).toBe(true)
    }
  })

  it('never treats success or a deliberate cancellation as analyzable', () => {
    expect(isAnalyzableFailure('complete')).toBe(false)
    expect(isAnalyzableFailure('cancelled')).toBe(false)
    expect(isAnalyzableFailure('running')).toBe(false)
    expect(isAnalyzableFailure('pending')).toBe(false)
  })

  it('covers every current terminal status except complete and cancelled', () => {
    // Guard against a new terminal status shipping without an Analyze decision.
    const terminal = [
      'complete',
      'failed',
      'cancelled',
      'eval_failed',
      'stalled',
      'budget_exceeded',
      'router_no_match',
      'cost_ceiling_exceeded',
      'compensation_failed',
    ]
    const expected = terminal.filter(s => s !== 'complete' && s !== 'cancelled')
    expect([...ANALYZABLE_FAILURE_STATUSES].sort()).toEqual([...expected].sort())
  })
})

describe('run / pipeline labels', () => {
  it('prefers the run number and falls back to the short id', () => {
    expect(runLabel(run())).toBe('#42')
    expect(runLabel(run({ runNumber: null }))).toBe('#a1b2c3d4')
  })

  it('prefers the pipeline name and falls back to the short pipeline id', () => {
    expect(pipelineLabel(run())).toBe('Deploy pipeline')
    expect(pipelineLabel(run({ pipelineName: null }))).toBe('#pipe-1')
  })
})

describe('buildAnalyzeSeedMessage', () => {
  it('carries the run identity, the error, and the root-cause-analysis request', () => {
    const message = buildAnalyzeSeedMessage(t, run())

    expect(message).toContain('Run: #42')
    expect(message).toContain('Pipeline: Deploy pipeline')
    expect(message).toContain('Status: failed')
    expect(message).toContain('Error code: harness.worker_failed')
    expect(message).toContain('node "build" exited with code 1')
    expect(message).toContain('Failing node: build')
    expect(message.toLowerCase()).toContain('root cause')
  })

  it('omits the optional lines the run does not have', () => {
    const message = buildAnalyzeSeedMessage(
      t,
      run({ errorCode: null, errorDetail: null, failingNode: null }),
    )

    expect(message).toContain('Run: #42')
    expect(message).not.toContain('Error code:')
    expect(message).not.toContain('Error detail:')
    expect(message).not.toContain('Failing node:')
    expect(message.toLowerCase()).toContain('root cause')
  })

  it('caps a huge error detail so the seed cannot blow the context window', () => {
    const huge = 'x'.repeat(MAX_ERROR_DETAIL_CHARS + 500)
    const message = buildAnalyzeSeedMessage(t, run({ errorDetail: huge }))

    expect(message).not.toContain(huge)
    expect(message).toContain('x'.repeat(MAX_ERROR_DETAIL_CHARS))
    expect(message).not.toContain('x'.repeat(MAX_ERROR_DETAIL_CHARS + 1))
  })

  it('interpolates only data — a run/pipeline name containing braces stays literal', () => {
    const message = buildAnalyzeSeedMessage(
      t,
      run({ pipelineName: 'weird {name} pipeline', errorDetail: 'value {not_a_placeholder}' }),
    )

    expect(message).toContain('Pipeline: weird {name} pipeline')
    expect(message).toContain('value {not_a_placeholder}')
    expect(message).not.toContain('[object Object]')
  })
})
