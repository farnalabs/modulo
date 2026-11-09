import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'

const mockGet = vi.hoisted(() => vi.fn())
const mockPost = vi.hoisted(() => vi.fn())

vi.mock('../lib/api/client', () => ({
  api: { GET: mockGet, POST: mockPost },
  getAccessToken: vi.fn().mockReturnValue('token'),
  clearAccessToken: vi.fn(),
}))

import DeterminationView from '../views/DeterminationView.vue'

const PROPOSAL_ID = '11111111-1111-4111-8111-111111111111'
const ATTEMPT_ID = '66666666-6666-4666-8666-666666666666'
const OBSERVED_ID = '22222222-2222-4222-8222-222222222222'
const DERIVED_ID = '33333333-3333-4333-8333-333333333333'
const LOW_ASSUMPTION_ID = '44444444-4444-4444-8444-444444444444'
const HIGH_ASSUMPTION_ID = '55555555-5555-4555-8555-555555555555'

function ok(data: unknown) {
  return { data, error: undefined, response: { ok: true, status: 200 } }
}

function scanResponse() {
  return {
    samples: [
      {
        connector_id: 'c-1',
        connector_type: 'github',
        resource: 'repos',
        sample_count: 3,
        error: null,
      },
      {
        connector_id: 'c-2',
        connector_type: 'jira',
        resource: 'issues',
        sample_count: 0,
        error: 'connector query timed out',
      },
    ],
    findings: [
      {
        category: 'stage',
        finding: 'Development stage detected: source repositories found',
        evidence: '3 repositories accessible',
        confidence: 'high',
        uncertainty: '',
        related_connector: 'c-1',
      },
      {
        category: 'automation',
        finding: 'No CI/CD configuration detected',
        evidence: 'Sampled repo metadata does not reference known CI tooling',
        confidence: 'low',
        uncertainty: 'CI config may exist in files not sampled',
        related_connector: null,
      },
    ],
    summary: 'SDLC stages detected: development',
  }
}

function evidenceRef() {
  return {
    reference_id: 'evidence:c-1:repos',
    source_id: 'c-1:repos',
    locator: 'scan:repos',
    provenance: 'observed',
  }
}

function proposal() {
  return {
    contract_version: 'alpha.determination-proposal.v1',
    proposal_id: PROPOSAL_ID,
    attempt: {
      id: ATTEMPT_ID,
      status: 'completed_with_gaps',
      source_inventory: [
        {
          source_id: 'c-1:repos',
          connector_type: 'github',
          resource: 'repos',
          sample_count: 3,
          status: 'available',
          error: null,
        },
        {
          source_id: 'c-2:issues',
          connector_type: 'jira',
          resource: 'issues',
          sample_count: 0,
          status: 'unavailable',
          error: 'connector query timed out',
        },
      ],
    },
    review_state: 'needs_review',
    activation_status: 'not_activated',
    observed_facts: [
      {
        id: OBSERVED_ID,
        statement: 'Observed 3 records from repos',
        kind: 'observed',
        confidence: 'high',
        evidence_refs: [evidenceRef()],
      },
    ],
    derived_elements: [
      {
        id: DERIVED_ID,
        statement: 'Development stage detected: source repositories found',
        kind: 'derived',
        confidence: 'high',
        evidence_refs: [evidenceRef()],
      },
    ],
    unresolved_assumptions: [
      {
        id: LOW_ASSUMPTION_ID,
        statement: 'Status taxonomy varies by tool',
        reason: 'Determination evidence is insufficient to resolve this claim.',
        confidence: 'low',
        evidence_refs: [],
      },
      {
        id: HIGH_ASSUMPTION_ID,
        statement: 'Deployment was inferred from repository metadata',
        reason: 'Observed only.',
        confidence: 'high',
        evidence_refs: [],
      },
    ],
    unavailable_evidence: [
      {
        source_id: 'c-2:issues',
        reason: 'connector query timed out',
        requested_resource: 'issues',
      },
    ],
    confidence: 'high',
    uncertainty: ['Deployment stage cannot be assessed without a deployment tool connector'],
    provenance: [evidenceRef()],
  }
}

function draftResponse() {
  return {
    nodes: [],
    edges: [],
    findings: scanResponse().findings,
    automation_suggestions: [],
    summary: 'SDLC stages detected: development',
    proposal: proposal(),
  }
}

function reviewResponse(decision: 'approve' | 'reject' | 'needs_review') {
  return {
    proposal: { ...proposal(), review_state: decision === 'approve' ? 'approved' : 'rejected' },
    decision,
    review_state: decision === 'approve' ? 'approved' : 'rejected',
    activation_status: 'not_activated',
    actor_id: '77777777-7777-4777-8777-777777777777',
    actor_username: 'dana',
    decided_at: '2026-01-02T03:04:05Z',
    reason: 'Out of date',
    acknowledged_assumption_ids: [],
    corrected_derived_element_ids: [],
    removed_derived_element_ids: [],
  }
}

function problem(status: number, detail: string) {
  return {
    data: undefined,
    error: { type: 'urn:problem:modulo:validation_error', title: 'Validation Error', status, detail },
    response: { ok: false, status },
  }
}

/**
 * The view makes two POSTs to different paths; branch on the path so a single
 * mock can serve the whole flow (draft -> review) the way a real backend would.
 */
function routeApi(options: {
  draft?: unknown
  review?: unknown
  scan?: unknown
}) {
  mockGet.mockImplementation((path: string) => {
    if (path === '/api/v1/determination') return Promise.resolve(ok(options.scan ?? scanResponse()))
    return Promise.resolve(problem(404, `unexpected GET ${path}`))
  })
  mockPost.mockImplementation((path: string) => {
    if (path === '/api/v1/determination/draft') return Promise.resolve(ok(options.draft ?? draftResponse()))
    if (path === '/api/v1/determination/review') return Promise.resolve(ok(options.review ?? reviewResponse('approve')))
    return Promise.resolve(problem(404, `unexpected POST ${path}`))
  })
}

async function mountView() {
  const wrapper = mount(DeterminationView, { global: { plugins: [createPinia()] } })
  await flushPromises()
  return wrapper
}

async function generateDraft(wrapper: Awaited<ReturnType<typeof mountView>>) {
  await wrapper.find('[data-testid="determination-generate-draft"]').trigger('click')
  await flushPromises()
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('DeterminationView', () => {
  it('runs determination and renders the sampled sources and findings', async () => {
    routeApi({})
    const wrapper = await mountView()

    expect(wrapper.find('[data-testid="determination-scan-empty"]').exists()).toBe(true)

    await wrapper.find('[data-testid="determination-run-scan"]').trigger('click')
    await flushPromises()

    expect(mockGet).toHaveBeenCalledWith('/api/v1/determination')
    expect(wrapper.findAll('[data-testid="determination-sample-row"]')).toHaveLength(2)
    expect(wrapper.findAll('[data-testid="determination-finding-row"]')).toHaveLength(2)
    expect(wrapper.find('[data-testid="determination-scan-summary"]').text()).toBe('SDLC stages detected: development')
    expect(wrapper.find('[data-testid="determination-samples"]').text()).toContain('connector query timed out')
    // The count renders as several samples, never as both plural forms at once.
    expect(wrapper.find('[data-testid="determination-samples"]').text()).toContain('2 samples')
    expect(wrapper.find('[data-testid="determination-samples"]').text()).not.toContain('|')
  })

  it('renders the projected proposal contract with evidence, assumptions and provenance', async () => {
    routeApi({})
    const wrapper = await mountView()

    expect(wrapper.find('[data-testid="determination-review-unavailable"]').exists()).toBe(true)

    await generateDraft(wrapper)

    expect(mockPost).toHaveBeenCalledWith('/api/v1/determination/draft')
    expect(wrapper.find('[data-testid="determination-review-unavailable"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="determination-review"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="determination-proposal-confidence"]').text()).toBe('High')
    expect(wrapper.findAll('[data-testid="determination-observed-fact"]')).toHaveLength(1)
    expect(wrapper.findAll('[data-testid="determination-derived-element"]')).toHaveLength(1)
    expect(wrapper.findAll('[data-testid="determination-assumption"]')).toHaveLength(2)
    expect(wrapper.findAll('[data-testid="determination-unavailable-row"]')).toHaveLength(1)
    expect(wrapper.findAll('[data-testid="determination-provenance-row"]')).toHaveLength(1)
    expect(wrapper.findAll('[data-testid="determination-source-row"]')).toHaveLength(2)

    const evidence = wrapper.findAll('[data-testid="determination-evidence-ref"]')
    expect(evidence.length).toBeGreaterThan(0)
    expect(evidence[0].text()).toContain('evidence:c-1:repos')
    expect(evidence[0].text()).toContain('c-1:repos')
    expect(evidence[0].text()).toContain('scan:repos')
    expect(evidence[0].text()).toContain('Observed')

    // The page is explicit that nothing was activated.
    expect(wrapper.find('[data-testid="determination-proposal-not-activated"]').text()).toContain('not been activated')
  })

  it('blocks approval until every blocking assumption is acknowledged, then records the decision', async () => {
    routeApi({ review: reviewResponse('approve') })
    const wrapper = await mountView()
    await generateDraft(wrapper)

    const approve = wrapper.find('[data-testid="determination-review-approve"]')
    expect(approve.attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-testid="determination-review-approve-blocked"]').text()).toContain('1 blocking assumption')

    // Only the low/unknown-confidence assumption is blocking; the high-confidence
    // one is listed in the proposal but has no acknowledgement control.
    expect(wrapper.find(`[data-testid="determination-review-acknowledge-${HIGH_ASSUMPTION_ID}"]`).exists()).toBe(false)

    await wrapper.find(`[data-testid="determination-review-acknowledge-${LOW_ASSUMPTION_ID}"]`).setValue(true)
    expect(approve.attributes('disabled')).toBeUndefined()

    await approve.trigger('click')
    await flushPromises()

    expect(mockPost).toHaveBeenLastCalledWith('/api/v1/determination/review', {
      body: expect.objectContaining({
        proposal_id: PROPOSAL_ID,
        decision: 'approve',
        acknowledged_assumption_ids: [LOW_ASSUMPTION_ID],
        corrected_derived_elements: [],
        removed_derived_element_ids: [],
      }),
    })

    expect(wrapper.find('[data-testid="determination-review-result-state"]').text()).toBe('Approved')
    expect(wrapper.find('[data-testid="determination-review-result-actor"]').text()).toBe('dana')
    expect(wrapper.find('[data-testid="determination-review-result-decided-at"]').text().length).toBeGreaterThan(0)
    expect(wrapper.find('[data-testid="determination-review-not-activated"]').text()).toContain('Not activated')
  })

  it('refuses a reject with no reason and surfaces the server detail when one is refused', async () => {
    routeApi({})
    const wrapper = await mountView()
    await generateDraft(wrapper)

    await wrapper.find('[data-testid="determination-review-reject"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="determination-review-reason-required"]').exists()).toBe(true)
    expect(mockPost).not.toHaveBeenCalledWith('/api/v1/determination/review', expect.anything())

    // A refused decision arrives as a 422 ProblemDetail; its detail is shown
    // verbatim inside the assertive live region.
    mockPost.mockImplementation((path: string) => {
      if (path === '/api/v1/determination/draft') return Promise.resolve(ok(draftResponse()))
      return Promise.resolve(
        problem(422, 'approval is blocked by unacknowledged low or unknown confidence assumptions: abc'),
      )
    })

    await wrapper.find('[data-testid="determination-review-reason"]').setValue('Out of date')
    await wrapper.find('[data-testid="determination-review-reject"]').trigger('click')
    await flushPromises()

    expect(mockPost).toHaveBeenLastCalledWith('/api/v1/determination/review', {
      body: expect.objectContaining({ decision: 'reject', reason: 'Out of date' }),
    })
    const error = wrapper.find('[data-testid="determination-review-error"]')
    expect(error.exists()).toBe(true)
    expect(error.attributes('aria-live')).toBe('assertive')
    expect(error.text()).toContain('approval is blocked by unacknowledged low or unknown confidence assumptions')
    expect(wrapper.find('[data-testid="determination-review-result"]').exists()).toBe(false)
  })

  it('sends only the derived-element corrections and removals the reviewer actually made', async () => {
    routeApi({ review: reviewResponse('needs_review') })
    const wrapper = await mountView()
    await generateDraft(wrapper)

    await wrapper.find(`[data-testid="determination-review-statement-${DERIVED_ID}"]`).setValue('Rewritten stage statement')
    await wrapper.find(`[data-testid="determination-review-confidence-${DERIVED_ID}"]`).setValue('medium')
    await wrapper.find('[data-testid="determination-review-needs-review"]').trigger('click')
    await flushPromises()

    expect(mockPost).toHaveBeenLastCalledWith('/api/v1/determination/review', {
      body: expect.objectContaining({
        decision: 'needs_review',
        corrected_derived_elements: [
          { id: DERIVED_ID, statement: 'Rewritten stage statement', confidence: 'medium' },
        ],
        removed_derived_element_ids: [],
      }),
    })

    // Removing the element sends its id instead of a correction, and an
    // unchanged element contributes nothing at all.
    await wrapper.find(`[data-testid="determination-review-remove-${DERIVED_ID}"]`).trigger('click')
    await wrapper.find('[data-testid="determination-review-needs-review"]').trigger('click')
    await flushPromises()

    expect(mockPost).toHaveBeenLastCalledWith('/api/v1/determination/review', {
      body: expect.objectContaining({
        corrected_derived_elements: [],
        removed_derived_element_ids: [DERIVED_ID],
      }),
    })
  })

  it('clears a recorded decision when a new draft is generated', async () => {
    routeApi({ review: reviewResponse('approve') })
    const wrapper = await mountView()
    await generateDraft(wrapper)

    await wrapper.find(`[data-testid="determination-review-acknowledge-${LOW_ASSUMPTION_ID}"]`).setValue(true)
    await wrapper.find('[data-testid="determination-review-approve"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="determination-review-result"]').exists()).toBe(true)

    await generateDraft(wrapper)

    expect(wrapper.find('[data-testid="determination-review-result"]').exists()).toBe(false)
    // The acknowledgement belonged to the previous proposal and must not carry over.
    expect(wrapper.find('[data-testid="determination-review-approve"]').attributes('disabled')).toBeDefined()
  })

  it('re-runs the scan from the error panel retry', async () => {
    mockGet.mockResolvedValueOnce(problem(502, 'scan exploded'))
    mockGet.mockResolvedValueOnce(ok(scanResponse()))
    const wrapper = await mountView()

    await wrapper.find('[data-testid="determination-run-scan"]').trigger('click')
    await flushPromises()

    const retry = wrapper.find('[data-testid="determination-scan"] [data-testid="error-alert-retry"]')
    expect(retry.exists()).toBe(true)

    await retry.trigger('click')
    await flushPromises()

    expect(mockGet).toHaveBeenCalledTimes(2)
    expect(wrapper.find('[data-testid="determination-scan"] [data-testid="error-alert-retry"]').exists()).toBe(false)
  })

  it('re-generates the draft from the proposal error panel retry', async () => {
    mockGet.mockResolvedValue(ok(scanResponse()))
    mockPost.mockResolvedValueOnce(problem(500, 'draft exploded'))
    mockPost.mockResolvedValueOnce(ok(draftResponse()))
    const wrapper = await mountView()

    await generateDraft(wrapper)

    const retry = wrapper.find('[data-testid="determination-proposal"] [data-testid="error-alert-retry"]')
    expect(retry.exists()).toBe(true)

    await retry.trigger('click')
    await flushPromises()

    expect(mockPost).toHaveBeenCalledTimes(2)
    expect(wrapper.find('[data-testid="determination-review"]').exists()).toBe(true)
  })

  it('drills an evidence reference into its sampled source record and cross-links the source row', async () => {
    routeApi({})
    const wrapper = await mountView()
    await generateDraft(wrapper)

    // Collapsed by default: the drill-down is opt-in per element.
    expect(wrapper.find('[data-testid="determination-evidence-drilldown"]').exists()).toBe(false)

    const toggle = wrapper.findAll('[data-testid="determination-evidence-toggle"]')[0]
    expect(toggle.attributes('aria-expanded')).toBe('false')
    await toggle.trigger('click')

    const drilldown = wrapper.find('[data-testid="determination-evidence-drilldown"]')
    expect(drilldown.exists()).toBe(true)
    expect(toggle.attributes('aria-expanded')).toBe('true')
    // The drill-down names the sampled source record from the attempt inventory.
    const source = wrapper.find('[data-testid="determination-evidence-source"]')
    expect(source.text()).toContain('github')
    expect(source.text()).toContain('repos')
    expect(source.text()).toContain('3')
    expect(source.text()).toContain('Available')

    // The source row cross-links back to the elements that cite it (the
    // observed fact and the derived element both cite this source).
    expect(wrapper.findAll('[data-testid="determination-source-ref-count"]')[0].text()).toContain('2 evidence references')

    await wrapper.find('[data-testid="determination-evidence-link-source"]').trigger('click')
    await flushPromises()

    expect(wrapper.findAll('[data-testid="determination-source-row"]')[0].classes()).toContain('ring-2')

    await toggle.trigger('click')
    expect(wrapper.find('[data-testid="determination-evidence-drilldown"]').exists()).toBe(false)
  })

  it('states when an evidence reference cites a source absent from the inventory', async () => {
    const orphaned = proposal()
    orphaned.derived_elements = [
      {
        id: DERIVED_ID,
        statement: 'Deployment stage inferred from an unlisted source',
        kind: 'derived',
        confidence: 'low',
        evidence_refs: [
          {
            reference_id: 'evidence:unknown:deployments',
            source_id: 'unknown:deployments',
            locator: 'scan:deployments',
            provenance: 'inferred',
          },
        ],
      },
    ]
    routeApi({ draft: { ...draftResponse(), proposal: orphaned } })
    const wrapper = await mountView()
    await generateDraft(wrapper)

    await wrapper.find('[data-testid="determination-derived-element"] [data-testid="determination-evidence-toggle"]').trigger('click')

    const drilldown = wrapper.find('[data-testid="determination-derived-element"] [data-testid="determination-evidence-drilldown"]')
    expect(drilldown.exists()).toBe(true)
    expect(drilldown.find('[data-testid="determination-evidence-source-missing"]').exists()).toBe(true)
    expect(drilldown.find('[data-testid="determination-evidence-source"]').exists()).toBe(false)
    expect(drilldown.find('[data-testid="determination-evidence-link-source"]').exists()).toBe(false)
  })
})
