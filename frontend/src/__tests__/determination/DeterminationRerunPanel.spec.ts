import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import DeterminationRerunPanel from '../../components/determination/DeterminationRerunPanel.vue'
import {
  correctionsAreEmpty,
  elementChangeBadgeClass,
  elementChangeCounts,
  elementChangeGroupClass,
  inputChangesPresent,
  matchedCorrectionCount,
  rerunReportsIncomplete,
  unmatchedCorrectionCount,
} from '../../components/determination/rerunPresentation'
import type {
  DeterminationProposal,
  DeterminationRerunResult,
  ElementChange,
  ElementChangeStatus,
  ReviewCorrections,
} from '../../composables/useDetermination'
import type { components } from '../../lib/api/schema'

const PRIOR_ATTEMPT = '11111111-1111-4111-8111-aaaaaaaaaaaa'
const CURRENT_ATTEMPT = '22222222-2222-4222-8222-bbbbbbbbbbbb'
const PROPOSAL_ID = '33333333-3333-4333-8333-cccccccccccc'

function proposal(): DeterminationProposal {
  return { proposal_id: PROPOSAL_ID, attempt: { id: PRIOR_ATTEMPT } } as unknown as DeterminationProposal
}

function attemptSummary(
  attemptId: string,
  overrides: Partial<components['schemas']['RerunAttemptSummary']> = {},
): components['schemas']['RerunAttemptSummary'] {
  return {
    attempt_id: attemptId,
    proposal_id: PROPOSAL_ID,
    status: 'completed',
    confidence: 'high',
    uncertainty: [],
    provenance: [],
    unavailable_evidence: [],
    source_inventory: [],
    ...overrides,
  }
}

function change(
  status: ElementChangeStatus,
  statement: string,
  overrides: Partial<ElementChange> = {},
): ElementChange {
  return {
    status,
    statement,
    prior_element_id: status === 'new' ? null : 'prior-element',
    current_element_id: status === 'new' ? 'current-element' : null,
    prior_confidence: status === 'new' ? null : 'high',
    current_confidence: null,
    prior_evidence_refs: [],
    current_evidence_refs: [],
    ...overrides,
  }
}

function emptyResult(overrides: Partial<DeterminationRerunResult> = {}): DeterminationRerunResult {
  return {
    contract_version: 'alpha.determination-rerun.v1',
    proposal: proposal(),
    prior_attempt: attemptSummary(PRIOR_ATTEMPT),
    current_attempt: attemptSummary(CURRENT_ATTEMPT),
    corrections: {},
    source_changes: {},
    input_set_unchanged: true,
    element_changes: [],
    incomplete: false,
    incomplete_reasons: [],
    ...overrides,
  }
}

function noCorrections(): ReviewCorrections {
  return { correctedDerivedElements: [], removedDerivedElementIds: [] }
}

function mountPanel(overrides: Partial<{
  proposal: DeterminationProposal | null
  corrections: ReviewCorrections
  result: DeterminationRerunResult | null
  loading: boolean
  error: string | null
}> = {}) {
  return mount(DeterminationRerunPanel, {
    props: {
      proposal: proposal(),
      corrections: noCorrections(),
      result: null,
      loading: false,
      error: null,
      ...overrides,
    },
  })
}

function group(wrapper: ReturnType<typeof mountPanel>, status: ElementChangeStatus) {
  return wrapper.find(`[data-testid="determination-rerun-group"][data-status="${status}"]`)
}

describe('rerunPresentation', () => {
  it('counts every classification, including the ones with none', () => {
    const changes = [
      change('retained', 'a'),
      change('retained', 'b'),
      change('not_reproduced', 'c'),
      change('removed', 'd'),
    ]

    const counts = elementChangeCounts(changes)

    expect(counts.retained).toBe(2)
    expect(counts.not_reproduced).toBe(1)
    expect(counts.removed).toBe(1)
    expect(counts.new).toBe(0)
    expect(counts.confidence_changed).toBe(0)
    expect(counts.evidence_changed).toBe(0)
  })

  it('keeps not_reproduced and removed stylistically distinct', () => {
    expect(elementChangeBadgeClass('not_reproduced')).not.toBe(elementChangeBadgeClass('removed'))
    expect(elementChangeBadgeClass('removed')).toContain('destructive')
    expect(elementChangeBadgeClass('not_reproduced')).not.toContain('destructive')
    expect(elementChangeGroupClass('not_reproduced')).not.toBe(elementChangeGroupClass('removed'))
    expect(elementChangeGroupClass('removed')).toContain('destructive')
  })

  it('detects an input change from every source-difference list', () => {
    expect(inputChangesPresent({})).toBe(false)
    expect(inputChangesPresent({ newly_available: ['github'] })).toBe(true)
    expect(inputChangesPresent({ sample_count_changes: [{ source_id: 'github', prior_sample_count: 1, current_sample_count: 2 }] })).toBe(true)
    expect(inputChangesPresent({ missing_sources: [{ source_id: 'github', connector_type: 'github', resource: 'repos', sample_count: 1, status: 'available' }] })).toBe(true)
  })

  it('treats a newly unavailable source as an incomplete rerun', () => {
    expect(rerunReportsIncomplete(emptyResult({ incomplete: true }))).toBe(true)
    expect(
      rerunReportsIncomplete(emptyResult({ source_changes: { newly_unavailable: ['github'] } })),
    ).toBe(true)
    expect(
      rerunReportsIncomplete(
        emptyResult({
          source_changes: {
            missing_sources: [
              { source_id: 'linear', connector_type: 'linear', resource: 'issues', sample_count: 1, status: 'available' },
            ],
          },
        }),
      ),
    ).toBe(true)
    expect(
      rerunReportsIncomplete(
        emptyResult({ current_attempt: attemptSummary(CURRENT_ATTEMPT, { status: 'completed_with_gaps' }) }),
      ),
    ).toBe(true)
    expect(rerunReportsIncomplete(emptyResult())).toBe(false)
  })

  it('counts matched and unmatched corrections independently', () => {
    const corrections = {
      corrected_derived_element_ids: ['a'],
      removed_derived_element_ids: ['b'],
      unmatched_corrected_derived_element_ids: ['c'],
      unmatched_removed_derived_element_ids: ['d'],
    }

    expect(matchedCorrectionCount(corrections)).toBe(2)
    expect(unmatchedCorrectionCount(corrections)).toBe(2)
    expect(correctionsAreEmpty({})).toBe(true)
    expect(correctionsAreEmpty(corrections)).toBe(false)
  })
})

describe('DeterminationRerunPanel', () => {
  it('renders each classification group from the returned changes', () => {
    const result = emptyResult({
      input_set_unchanged: false,
      element_changes: [
        change('retained', 'Retained inference'),
        change('new', 'Brand new inference', { current_confidence: 'medium' }),
        change('not_reproduced', 'Not reproduced inference'),
        change('removed', 'Removed inference'),
        change('confidence_changed', 'Confidence moved', { current_confidence: 'low' }),
        change('evidence_changed', 'Evidence moved'),
      ],
    })

    const wrapper = mountPanel({ result })

    expect(wrapper.findAll('[data-testid="determination-rerun-group"]')).toHaveLength(6)
    for (const status of [
      'retained',
      'new',
      'not_reproduced',
      'removed',
      'confidence_changed',
      'evidence_changed',
    ] as ElementChangeStatus[]) {
      expect(group(wrapper, status).exists()).toBe(true)
    }
    expect(wrapper.findAll('[data-testid="determination-rerun-change"]')).toHaveLength(6)
    expect(wrapper.findAll('[data-testid="determination-rerun-count"]')).toHaveLength(6)
    expect(wrapper.find('[data-testid="determination-rerun-counts"]').text()).toContain('Retained: 1')
    expect(wrapper.find('[data-testid="determination-rerun-changes"]').text()).toContain('Retained inference')
    expect(wrapper.find('[data-testid="determination-rerun-no-changes"]').exists()).toBe(false)
  })

  it('never presents not_reproduced as removed, false or wrong', () => {
    const result = emptyResult({
      element_changes: [
        change('not_reproduced', 'Sampling inference'),
        change('removed', 'Input-dependent inference'),
      ],
    })

    const wrapper = mountPanel({ result })
    const notReproduced = group(wrapper, 'not_reproduced')
    const removed = group(wrapper, 'removed')

    expect(notReproduced.text()).toContain('Not reproduced')
    expect(notReproduced.text()).toContain('That is not a judgement that they are false or wrong.')
    expect(notReproduced.attributes('class')).not.toContain('border-destructive')
    expect(notReproduced.attributes('class')).toContain('border-dashed')

    expect(removed.text()).toContain('Removed')
    expect(removed.text()).toContain('because the sampled inputs changed')
    expect(removed.attributes('class')).toContain('border-destructive')
    expect(removed.text()).not.toContain('false')
  })

  it('renders an explicit inputs-unchanged state rather than no input section', () => {
    const wrapper = mountPanel({ result: emptyResult({ input_set_unchanged: true }) })

    expect(wrapper.find('[data-testid="determination-rerun-inputs-unchanged"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="determination-rerun-input-group"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="determination-rerun-inputs"]').text()).toContain('Inputs unchanged')
  })

  it('renders every kind of input change', () => {
    const result = emptyResult({
      input_set_unchanged: false,
      source_changes: {
        new_sources: [
          { source_id: 'new-source', connector_type: 'github', resource: 'issues', sample_count: 4, status: 'available' },
        ],
        missing_sources: [
          { source_id: 'old-source', connector_type: 'linear', resource: 'issues', sample_count: 2, status: 'available' },
        ],
        newly_available: ['now-available'],
        newly_unavailable: ['now-unavailable'],
        sample_count_changes: [{ source_id: 'count-source', prior_sample_count: 2, current_sample_count: 5 }],
      },
    })

    const wrapper = mountPanel({ result })

    expect(wrapper.find('[data-testid="determination-rerun-inputs-unchanged"]').exists()).toBe(false)
    expect(wrapper.findAll('[data-testid="determination-rerun-input-new"]')).toHaveLength(1)
    expect(wrapper.findAll('[data-testid="determination-rerun-input-missing"]')).toHaveLength(1)
    expect(wrapper.findAll('[data-testid="determination-rerun-input-newly-available"]')).toHaveLength(1)
    expect(wrapper.findAll('[data-testid="determination-rerun-input-newly-unavailable"]')).toHaveLength(1)
    expect(wrapper.findAll('[data-testid="determination-rerun-input-sample-count"]')).toHaveLength(1)
    expect(wrapper.find('[data-testid="determination-rerun-inputs"]').text()).toContain(
      'count-source: changed from 2 records to 5 records',
    )
  })

  it('shows a loud incomplete banner with the returned reasons', () => {
    const wrapper = mountPanel({
      result: emptyResult({
        incomplete: true,
        incomplete_reasons: ['the fresh attempt completed with gaps', 'source no longer sampled: linear'],
      }),
    })

    const banner = wrapper.find('[data-testid="determination-rerun-incomplete"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('Incomplete rerun')
    expect(banner.text()).toContain('not an authoritative one')
    const reasons = wrapper.findAll('[data-testid="determination-rerun-incomplete-reason"]')
    expect(reasons.map((node) => node.text())).toEqual([
      'the fresh attempt completed with gaps',
      'source no longer sampled: linear',
    ])
    expect(banner.attributes('aria-live')).toBeUndefined()
  })

  it('treats a newly unavailable source as an incomplete result even when the flag is false', () => {
    const wrapper = mountPanel({
      result: emptyResult({
        incomplete: false,
        source_changes: { newly_unavailable: ['github'] },
      }),
    })

    const banner = wrapper.find('[data-testid="determination-rerun-incomplete"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('source became unavailable: github')
  })

  it('names a missing source even when the returned reasons did not', () => {
    const wrapper = mountPanel({
      result: emptyResult({
        incomplete: false,
        incomplete_reasons: [],
        source_changes: {
          missing_sources: [
            { source_id: 'linear', connector_type: 'linear', resource: 'issues', sample_count: 1, status: 'available' },
          ],
        },
      }),
    })

    const banner = wrapper.find('[data-testid="determination-rerun-incomplete"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('source no longer sampled: linear')
  })

  it('shows no incomplete banner for a complete rerun', () => {
    const wrapper = mountPanel({ result: emptyResult() })

    expect(wrapper.find('[data-testid="determination-rerun-incomplete"]').exists()).toBe(false)
  })

  it('renders carried-forward corrections and surfaces unmatched ids', () => {
    const wrapper = mountPanel({
      result: emptyResult({
        corrections: {
          corrected_derived_element_ids: ['matched-correction'],
          removed_derived_element_ids: ['matched-removal'],
          unmatched_corrected_derived_element_ids: ['unmatched-correction'],
          unmatched_removed_derived_element_ids: ['unmatched-removal'],
        },
      }),
    })

    const matched = wrapper.find('[data-testid="determination-rerun-corrections-matched"]')
    expect(matched.text()).toContain('matched-correction')
    expect(matched.text()).toContain('matched-removal')
    expect(matched.findAll('[data-testid="determination-rerun-correction-id"][data-matched="true"]')).toHaveLength(2)

    const unmatched = wrapper.find('[data-testid="determination-rerun-corrections-unmatched"]')
    expect(unmatched.text()).toContain('unmatched-correction')
    expect(unmatched.text()).toContain('unmatched-removal')
    expect(unmatched.text()).toContain('could not be carried forward')
    expect(unmatched.findAll('[data-testid="determination-rerun-correction-id"][data-matched="false"]')).toHaveLength(2)
  })

  it('states that there were no corrections to carry forward', () => {
    const wrapper = mountPanel({ result: emptyResult({ corrections: {} }) })

    expect(wrapper.find('[data-testid="determination-rerun-corrections-none-result"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="determination-rerun-corrections-matched"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="determination-rerun-corrections-unmatched"]').exists()).toBe(false)
  })

  it('renders both attempt identities with their confidence, uncertainty and provenance', () => {
    const reference = {
      reference_id: 'ref-1',
      source_id: 'github',
      locator: 'repo#1',
      provenance: 'observed' as const,
    }
    const wrapper = mountPanel({
      result: emptyResult({
        prior_attempt: attemptSummary(PRIOR_ATTEMPT, {
          uncertainty: ['Owner team is not recorded.'],
          provenance: [reference],
        }),
        current_attempt: attemptSummary(CURRENT_ATTEMPT, {
          status: 'completed_with_gaps',
          confidence: 'medium',
          provenance: [reference],
        }),
      }),
    })

    const attempts = wrapper.findAll('[data-testid="determination-rerun-attempt"]')
    expect(attempts).toHaveLength(2)
    expect(attempts[0].find('[data-testid="determination-rerun-attempt-heading"]').text()).toBe('Prior attempt')
    expect(attempts[1].find('[data-testid="determination-rerun-attempt-heading"]').text()).toBe('New attempt')
    expect(attempts[0].find('[data-testid="determination-rerun-attempt-id"]').text()).toBe(PRIOR_ATTEMPT)
    expect(attempts[1].find('[data-testid="determination-rerun-attempt-id"]').text()).toBe(CURRENT_ATTEMPT)
    expect(attempts[0].find('[data-testid="determination-rerun-attempt-confidence"]').text()).toBe('High')
    expect(attempts[1].find('[data-testid="determination-rerun-attempt-confidence"]').text()).toBe('Medium')
    expect(attempts[0].find('[data-testid="determination-rerun-attempt-uncertainty"]').text()).toContain(
      'Owner team is not recorded.',
    )
    expect(attempts[0].find('[data-testid="determination-rerun-attempt-uncertainty-empty"]').exists()).toBe(false)
    expect(attempts[1].find('[data-testid="determination-rerun-attempt-uncertainty-empty"]').exists()).toBe(true)
    expect(attempts[0].findAll('[data-testid="determination-evidence-ref"]')).toHaveLength(1)
    expect(attempts[1].find('[data-testid="determination-rerun-attempt-status"]').text()).toBe('Completed with gaps')
  })

  it('states that a rerun creates a new attempt and changes nothing', () => {
    const wrapper = mountPanel()

    const notice = wrapper.find('[data-testid="determination-rerun-not-activated"]')
    expect(notice.exists()).toBe(true)
    expect(notice.text()).toContain('creates a new attempt')
    expect(notice.text()).toContain('changes nothing')
    expect(notice.text()).toContain('never modified')
    expect(notice.text()).toContain('not activated or published')
  })

  it('surfaces a failed rerun assertively and hides any previous result', () => {
    const wrapper = mountPanel({
      result: emptyResult(),
      error: 'Prior proposal carries duplicate derived element ids',
    })

    const alert = wrapper.find('[data-testid="determination-rerun-error"]')
    expect(alert.exists()).toBe(true)
    expect(alert.attributes('aria-live')).toBe('assertive')
    expect(alert.attributes('role')).toBe('alert')
    expect(alert.text()).toContain('duplicate derived element ids')
    expect(wrapper.find('[data-testid="determination-rerun-result"]').exists()).toBe(false)
  })

  it('emits rerun, disables while running and announces progress politely', async () => {
    const wrapper = mountPanel()
    await wrapper.find('[data-testid="determination-rerun-run"]').trigger('click')
    expect(wrapper.emitted('rerun')).toHaveLength(1)

    const running = mountPanel({ loading: true })
    const button = running.find('[data-testid="determination-rerun-run"]')
    expect(button.text()).toBe('Rerunning...')
    expect(button.attributes('disabled')).toBeDefined()
    expect(running.find('[data-testid="determination-rerun-loading"]').attributes('aria-live')).toBe('polite')
  })

  it('shows the pending correction count before a rerun', () => {
    const wrapper = mountPanel({
      corrections: {
        correctedDerivedElements: [{ id: 'element-1', statement: 'Corrected' }],
        removedDerivedElementIds: ['element-2'],
      },
    })

    expect(wrapper.find('[data-testid="determination-rerun-corrections-pending"]').text()).toContain(
      '2 review corrections will be carried forward.',
    )
    expect(wrapper.find('[data-testid="determination-rerun-corrections-none"]').exists()).toBe(false)
  })

  it('needs a generated proposal before a rerun is offered', () => {
    const wrapper = mountPanel({ proposal: null })

    expect(wrapper.find('[data-testid="determination-rerun-needs-proposal"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="determination-rerun-run"]').exists()).toBe(false)
  })
})
