import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import DeterminationComparisonPanel from '../../components/determination/DeterminationComparisonPanel.vue'
import {
  behaviourLabelKey,
  comparisonIsEmpty,
  propertyValueView,
  sideBadgeClass,
} from '../../components/determination/comparisonPresentation'
import type { DeterminationDraftResponse } from '../../composables/useDetermination'
import type { components } from '../../lib/api/schema'

type ProposalComparison = components['schemas']['ProposalComparison']
type ProvenanceBundle = components['schemas']['ProvenanceBundle']
type ProposalSideSummary = components['schemas']['ProposalSideSummary']
type BehaviourChange = components['schemas']['BehaviourChange']

const BASE_ATTEMPT = '11111111-1111-4111-8111-aaaaaaaaaaaa'
const TARGET_ATTEMPT = '22222222-2222-4222-8222-bbbbbbbbbbbb'
const BASE_PROPOSAL = '33333333-3333-4333-8333-cccccccccccc'
const TARGET_PROPOSAL = '44444444-4444-4444-8444-dddddddddddd'

function bundle(overrides: Partial<ProvenanceBundle> = {}): ProvenanceBundle {
  return {
    provenance_kinds: ['observed'],
    evidence_refs: [
      {
        reference_id: 'ref-1',
        source_id: 'github',
        locator: 'repo#12',
        provenance: 'observed',
      },
    ],
    confidence: 'high',
    uncertainty: [],
    ...overrides,
  }
}

function sideSummary(side: 'base' | 'target', attemptId: string, proposalId: string): ProposalSideSummary {
  return {
    side,
    proposal_id: proposalId,
    attempt_id: attemptId,
    review_state: 'needs_review',
    activation_status: 'not_activated',
    confidence: 'high',
    uncertainty: [],
    provenance: [],
    unavailable_evidence: [],
  }
}

function emptyComparison(): ProposalComparison {
  return {
    contract_version: 'alpha.determination-comparison.v1',
    base: sideSummary('base', BASE_ATTEMPT, BASE_PROPOSAL),
    target: sideSummary('target', TARGET_ATTEMPT, TARGET_PROPOSAL),
    nodes_added: [],
    nodes_removed: [],
    nodes_changed: [],
    edges_added: [],
    edges_removed: [],
    edges_changed: [],
    behaviour_changes: [],
  }
}

function behaviour(kind: string, subjectKind: 'node' | 'edge', subjectId: string): BehaviourChange {
  return {
    kind: kind as BehaviourChange['kind'],
    subject_kind: subjectKind,
    subject_id: subjectId,
    property: subjectKind === 'edge' ? 'hitl_review' : 'node_type',
    base_value: true,
    target_value: null,
  }
}

function populatedComparison(): ProposalComparison {
  return {
    ...emptyComparison(),
    nodes_added: [
      {
        id: 'ship',
        side: 'target',
        node: { id: 'ship', node_type: 'manual', label: 'Ship it' },
        provenance: bundle({ provenance_kinds: ['inferred'], uncertainty: ['Owner team is not recorded.'] }),
      },
    ],
    nodes_removed: [
      {
        id: 'legacy-review',
        side: 'base',
        node: { id: 'legacy-review', node_type: 'agent', label: 'Legacy review' },
        provenance: bundle(),
      },
    ],
    nodes_changed: [
      {
        id: 'deploy',
        base: { id: 'deploy', node_type: 'agent', label: 'Deploy' },
        target: { id: 'deploy', node_type: 'manual', label: 'Deploy manually' },
        changes: [
          { property: 'node_type', base_value: 'agent', target_value: 'manual' },
          { property: 'label', base_value: 'Deploy', target_value: 'Deploy manually' },
        ],
        base_provenance: bundle({ provenance_kinds: ['observed'] }),
        target_provenance: bundle({ provenance_kinds: ['derived'], confidence: 'medium' }),
      },
    ],
    edges_added: [
      {
        id: 'build->ship',
        side: 'target',
        edge: { source: 'build', target: 'ship', edge_type: 'normal', hitl_review: true },
        provenance: bundle(),
      },
    ],
    edges_removed: [
      {
        id: 'build->legacy-review',
        side: 'base',
        edge: { source: 'build', target: 'legacy-review', edge_type: 'normal', hitl_review: false },
        provenance: bundle(),
      },
    ],
    edges_changed: [
      {
        id: 'test->deploy',
        base: { source: 'test', target: 'deploy', edge_type: 'normal', hitl_review: false },
        target: { source: 'test', target: 'deploy', edge_type: 'normal', hitl_review: true },
        changes: [{ property: 'hitl_review', base_value: false, target_value: true }],
        base_provenance: bundle(),
        target_provenance: bundle(),
      },
    ],
    behaviour_changes: [
      behaviour('approval_gate_removed', 'edge', 'build->legacy-review'),
      behaviour('step_deautomated', 'node', 'deploy'),
    ],
  }
}

function draft(attemptId: string, proposalId: string): DeterminationDraftResponse {
  return {
    proposal: { attempt: { id: attemptId }, proposal_id: proposalId },
    nodes: [],
    edges: [],
  } as unknown as DeterminationDraftResponse
}

function mountPanel(overrides: Partial<{
  baseline: DeterminationDraftResponse | null
  target: DeterminationDraftResponse | null
  comparison: ProposalComparison | null
  loading: boolean
  error: string | null
}> = {}) {
  return mount(DeterminationComparisonPanel, {
    props: {
      baseline: null,
      target: null,
      comparison: null,
      loading: false,
      error: null,
      ...overrides,
    },
  })
}

describe('comparisonPresentation', () => {
  it('detects an empty comparison from every item list', () => {
    expect(comparisonIsEmpty(emptyComparison())).toBe(true)
    expect(comparisonIsEmpty(populatedComparison())).toBe(false)
    expect(comparisonIsEmpty({ ...emptyComparison(), behaviour_changes: undefined })).toBe(true)
  })

  it('keeps current and proposed sides visually distinct', () => {
    expect(sideBadgeClass('base')).not.toBe(sideBadgeClass('target'))
    expect(sideBadgeClass('base')).toContain('muted')
    expect(sideBadgeClass('target')).toContain('warning')
  })

  it('never maps an unknown behaviour kind onto a known category', () => {
    expect(behaviourLabelKey('approval_gate_removed')).toBe('comparison_behaviour_approval_gate_removed')
    expect(behaviourLabelKey('something-new')).toBe('comparison_behaviour_other')
  })

  it('distinguishes an absent property from an empty or boolean value', () => {
    expect(propertyValueView(null)).toEqual({ kind: 'absent' })
    expect(propertyValueView(true)).toEqual({ kind: 'boolean', value: true })
    expect(propertyValueView('')).toEqual({ kind: 'text', value: '' })
  })
})

describe('DeterminationComparisonPanel', () => {
  it('renders added, removed and changed nodes and edges', () => {
    const wrapper = mountPanel({ comparison: populatedComparison() })

    expect(wrapper.find('[data-testid="determination-comparison-nodes-added"]').exists()).toBe(true)
    expect(wrapper.findAll('[data-testid="determination-comparison-node-added"]')).toHaveLength(1)
    expect(wrapper.find('[data-testid="determination-comparison-nodes-removed"]').exists()).toBe(true)
    expect(wrapper.findAll('[data-testid="determination-comparison-node-removed"]')).toHaveLength(1)
    expect(wrapper.find('[data-testid="determination-comparison-nodes-changed"]').exists()).toBe(true)

    const changed = wrapper.find('[data-testid="determination-comparison-node-changed"]')
    expect(changed.findAll('[data-testid="determination-comparison-property"]')).toHaveLength(2)
    expect(changed.findAll('[data-testid="determination-comparison-property-name"]')[0].text()).toBe('node_type')
    expect(changed.findAll('[data-testid="determination-comparison-property-current"]')[0].text()).toBe('agent')
    expect(changed.findAll('[data-testid="determination-comparison-property-target"]')[0].text()).toBe('manual')

    expect(wrapper.findAll('[data-testid="determination-comparison-edge-added"]')).toHaveLength(1)
    expect(wrapper.findAll('[data-testid="determination-comparison-edge-removed"]')).toHaveLength(1)
    expect(wrapper.findAll('[data-testid="determination-comparison-edge-changed"]')).toHaveLength(1)
  })

  it('shows each item side, provenance, confidence and uncertainty', () => {
    const wrapper = mountPanel({ comparison: populatedComparison() })

    const added = wrapper.find('[data-testid="determination-comparison-node-added"]')
    expect(added.find('[data-testid="determination-comparison-side-badge"]').text()).toBe('Proposed')
    expect(added.findAll('[data-testid="determination-comparison-provenance-kind"]')).toHaveLength(1)
    expect(added.find('[data-testid="determination-comparison-confidence"]').text()).toBe('High')
    expect(added.find('[data-testid="determination-comparison-uncertainty"]').text()).toContain(
      'Owner team is not recorded.',
    )
    expect(added.find('[data-testid="determination-evidence-ref"]').text()).toContain('ref-1')

    const removed = wrapper.find('[data-testid="determination-comparison-node-removed"]')
    expect(removed.find('[data-testid="determination-comparison-side-badge"]').text()).toBe('Current')

    const changed = wrapper.find('[data-testid="determination-comparison-node-changed"]')
    expect(changed.findAll('[data-testid="determination-comparison-provenance"]')).toHaveLength(2)
    expect(changed.findAll('[data-testid="determination-comparison-confidence"]').map((node) => node.text())).toEqual([
      'High',
      'Medium',
    ])
  })

  it('renders only the approval and automation categories the response contains', () => {
    const wrapper = mountPanel({ comparison: populatedComparison() })

    const rows = wrapper.findAll('[data-testid="determination-comparison-behaviour-row"]')
    expect(rows).toHaveLength(2)
    const text = wrapper.find('[data-testid="determination-comparison-behaviour"]').text()
    expect(text).toContain('Approval gate removed')
    expect(text).toContain('Step deautomated')
    expect(text).not.toContain('Approval gate added')
    expect(text).not.toContain('Step automated')
    expect(wrapper.find('[data-testid="determination-comparison-behaviour-empty"]').exists()).toBe(false)
  })

  it('renders an out-of-vocabulary behaviour kind as its reported value', () => {
    const comparison = {
      ...populatedComparison(),
      behaviour_changes: [behaviour('brand_new_category', 'node', 'deploy')],
    } as ProposalComparison

    const wrapper = mountPanel({ comparison })

    expect(wrapper.find('[data-testid="determination-comparison-behaviour"]').text()).toContain(
      'Reported change: brand_new_category',
    )
  })

  it('states that comparing activates and mutates nothing', () => {
    const wrapper = mountPanel({ comparison: populatedComparison() })

    const notice = wrapper.find('[data-testid="determination-comparison-not-activated"]')
    expect(notice.exists()).toBe(true)
    expect(notice.text()).toContain('no pipeline is created')
    expect(notice.text()).toContain('nothing is written to a connected tool')
  })

  it('shows the identical-inputs empty state and hides every change section', () => {
    const wrapper = mountPanel({ comparison: emptyComparison() })

    expect(wrapper.find('[data-testid="determination-comparison-identical"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="determination-comparison-behaviour"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="determination-comparison-nodes-added"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="determination-comparison-nodes-removed"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="determination-comparison-nodes-changed"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="determination-comparison-edges-added"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="determination-comparison-edges-removed"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="determination-comparison-edges-changed"]').exists()).toBe(false)
  })

  it('surfaces a failed compare in an assertive region and drops the previous result', () => {
    const wrapper = mountPanel({ comparison: populatedComparison(), error: 'Comparison was rejected: duplicate node ids' })

    const alert = wrapper.find('[data-testid="determination-comparison-error"]')
    expect(alert.exists()).toBe(true)
    expect(alert.attributes('aria-live')).toBe('assertive')
    expect(alert.attributes('role')).toBe('alert')
    expect(alert.text()).toContain('duplicate node ids')
    expect(wrapper.find('[data-testid="determination-comparison-sides"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="determination-comparison-behaviour"]').exists()).toBe(false)
  })

  it('shows the held baseline attempt and emits the comparison controls', async () => {
    const wrapper = mountPanel({
      baseline: draft(BASE_ATTEMPT, BASE_PROPOSAL),
      target: draft(TARGET_ATTEMPT, TARGET_PROPOSAL),
    })

    expect(wrapper.find('[data-testid="determination-comparison-baseline-attempt"]').text()).toBe(BASE_ATTEMPT)
    expect(wrapper.find('[data-testid="determination-comparison-baseline-proposal"]').text()).toBe(BASE_PROPOSAL)
    expect(wrapper.find('[data-testid="determination-comparison-baseline-empty"]').exists()).toBe(false)

    await wrapper.find('[data-testid="determination-comparison-hold"]').trigger('click')
    await wrapper.find('[data-testid="determination-comparison-clear"]').trigger('click')
    await wrapper.find('[data-testid="determination-comparison-run"]').trigger('click')

    expect(wrapper.emitted('hold')).toHaveLength(1)
    expect(wrapper.emitted('clear')).toHaveLength(1)
    expect(wrapper.emitted('compare')).toHaveLength(1)
  })

  it('refuses to compare without a held baseline or a target draft', () => {
    const noBaseline = mountPanel({ target: draft(TARGET_ATTEMPT, TARGET_PROPOSAL) })
    expect(noBaseline.find('[data-testid="determination-comparison-run"]').attributes('disabled')).toBeDefined()

    const noTarget = mountPanel({ baseline: draft(BASE_ATTEMPT, BASE_PROPOSAL) })
    expect(noTarget.find('[data-testid="determination-comparison-run"]').attributes('disabled')).toBeDefined()
  })
})
