import { test, expect } from './setup/fixtures'
import { waitForRunCompletionWithHitlRecovery } from './setup/realstack-api'

/**
 * Harness probe for the HITL recovery outcome (FAR-1408 — the revisit #1214's
 * own PR body asked for).
 *
 * `waitForRunCompletionWithHitlRecovery` has exactly two outcomes: `complete`,
 * or `incomplete` — and the journey turns anything but `complete` into a thrown
 * failure. The `infra-blocked` arm #1214 added (classify a sustained
 * approve-503 as infrastructure and SKIP the test) is gone, because that
 * "staging storm" was not an outage: `approve_review` 503'd deterministically
 * on every request while status polls kept returning 200 `claimed` — a product
 * defect, fixed in #1230.
 *
 * Two cases pin both halves of the contract without a backend:
 *
 *  1. The incident signature — a persistent approve 503 behind a HEALTHY
 *     status endpoint — must come back `incomplete`, so the journey FAILS on
 *     it instead of skipping.
 *  2. A genuine transient approve 503 must still be absorbed by the bounded
 *     per-request retry, so the journey recovers and passes.
 *
 * The endpoints are stubbed by replacing `global fetch` for the duration of
 * each test (restored in `finally`): no page, no browser, no network.
 */

const RUN_ID = '00000000-0000-4000-8000-000000000000'
const API_BASE = 'https://hitl-recovery-probe.invalid'

interface StubbedResponse {
  status: number
  body?: unknown
}

type StubRouter = (url: string) => StubbedResponse | undefined

/**
 * Replace `global fetch` with a URL router for the duration of a test.
 * Returns the restore function; call it in `finally` so a leak can never
 * poison the next test in the same Playwright worker.
 */
function stubFetch(route: StubRouter): () => void {
  const original = globalThis.fetch
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    const matched = route(url)
    if (!matched) throw new Error(`[hitl-probe] unexpected request: ${url}`)
    const text = JSON.stringify(matched.body ?? {})
    return {
      status: matched.status,
      ok: matched.status >= 200 && matched.status < 300,
      statusText: '',
      headers: new Headers(),
      text: async () => text,
      json: async () => JSON.parse(text),
    } as unknown as Response
  }) as typeof globalThis.fetch
  return () => {
    globalThis.fetch = original
  }
}

test.describe('Harness probe: HITL recovery outcome (no backend)', { tag: '@regression' }, () => {
  test('a persistent approve 503 behind a healthy status endpoint comes back incomplete, not skipped', { tag: '@regression' }, async () => {
    // The exact 2026-10-02 signature: GET /runs answers 200 `claimed` while
    // POST .../approve answers 503 "Database temporarily unavailable." — the
    // API and DB reachable, the approve path broken. #1214 skipped on this;
    // the journey must FAIL on it (FAR-1408 / #1230).
    const restore = stubFetch((url) => {
      if (url.includes('/hitl/pending')) return { status: 200, body: { reviews: [{ review_id: 'rev-1', decision: null }] } }
      if (url.includes('/hitl/rev-1/claim')) return { status: 200, body: { claim_token: 'probe-claim-token' } }
      if (url.includes('/hitl/rev-1/approve')) return { status: 503, body: { detail: 'Database temporarily unavailable.' } }
      if (url.includes(`/runs/${RUN_ID}`)) return { status: 200, body: { run_id: RUN_ID, status: 'claimed' } }
      return undefined
    })
    try {
      const outcome = await waitForRunCompletionWithHitlRecovery(API_BASE, 'probe-token', RUN_ID, {
        deadlineMs: 5_000,
        pollMs: 1_000,
        notes: 'probe',
      })
      // The journey throws on anything but `complete`, so this outcome IS
      // "the test fails". A reintroduced skip-worthy classification would
      // surface here as a kind this guard rejects.
      if (outcome.kind !== 'incomplete') {
        throw new Error(
          `expected 'incomplete' (a failure signal the journey throws on), got '${outcome.kind}' — ` +
            'a persistent approve 503 must never be reported as anything the caller can pass on',
        )
      }
      // Legibility: the failure reason must name the persistent approve 503
      // it actually observed, not an opaque "did not complete".
      expect(outcome.lastError).toContain('503')
      expect(outcome.lastError).toContain('Database temporarily unavailable')
      expect(outcome.lastError).toContain('approve')
    } finally {
      restore()
    }
  })

  test('a genuine transient approve 503 is still recovered by the bounded per-request retry', { tag: '@regression' }, async () => {
    // The approve 503s three times (the rolled-back blip class) and only the
    // FOURTH request inside ONE re-issue succeeds. A later re-issue cycle
    // would never see a 200 again — only call 4 returns one — so passing here
    // proves the bounded retry absorbed the blip; without it the outcome would
    // be `incomplete` and this test would fail.
    let approveCalls = 0
    let approved = false
    const restore = stubFetch((url) => {
      if (url.includes('/hitl/pending')) {
        return { status: 200, body: { reviews: [{ review_id: 'rev-1', decision: approved ? 'approved' : null }] } }
      }
      if (url.includes('/hitl/rev-1/claim')) return { status: 200, body: { claim_token: 'probe-claim-token' } }
      if (url.includes('/hitl/rev-1/approve')) {
        approveCalls += 1
        if (approveCalls < 4) return { status: 503, body: { detail: 'Database temporarily unavailable.' } }
        approved = true
        return { status: 200, body: {} }
      }
      if (url.includes(`/runs/${RUN_ID}`)) {
        return { status: 200, body: { run_id: RUN_ID, status: approved ? 'complete' : 'awaiting_human' } }
      }
      return undefined
    })
    try {
      const outcome = await waitForRunCompletionWithHitlRecovery(API_BASE, 'probe-token', RUN_ID, {
        deadlineMs: 8_000,
        pollMs: 3_000,
        notes: 'probe',
      })
      expect(outcome.kind).toBe('complete')
      // Exactly four approve requests: three refused 503s plus the retry that
      // got through — the recovery happened inside a single re-issue.
      expect(approveCalls).toBe(4)
    } finally {
      restore()
    }
  })
})
