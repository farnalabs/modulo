import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import enUS from '../../locales/en-US.js'
import PipelineGovernanceVisibility from './PipelineGovernanceVisibility.vue'

const i18n = createI18n({
  legacy: false,
  locale: 'en-US',
  messages: { 'en-US': enUS },
})

type GovernanceProps = {
  mode: 'toolbar' | 'node'
  pipeline?: Record<string, unknown> | null
  node?: Record<string, unknown> | null
  teamNames?: Record<string, string>
}

function mountVisibility(props: GovernanceProps) {
  return mount(PipelineGovernanceVisibility, {
    props,
    global: { plugins: [i18n] },
  })
}

describe('PipelineGovernanceVisibility', () => {
  it('shows only values supplied by the loaded pipeline, without live usage claims', () => {
    const wrapper = mountVisibility({
      mode: 'toolbar',
      pipeline: { max_autonomy_level: 'fully_autonomous', max_concurrent_runs: 5, circuit_breaker_threshold: 100 },
    })

    expect(wrapper.text()).toContain('Ceiling: Fully autonomous')
    expect(wrapper.text()).toContain('Capacity: 5 concurrent runs')
    expect(wrapper.text()).toContain('Breaker: $100.00/mo')
    // No live run count or current spend is loaded by this view, so neither is claimed.
    expect(wrapper.text()).not.toContain('3/5')
    expect(wrapper.text()).not.toContain('$47.20')
    // Resolved locale keys, never a raw catalogue key in the DOM.
    expect(wrapper.html()).not.toContain('views.PipelineEditorView.governance_')
    expect(wrapper.find('[data-testid="pipeline-editor-governance-visibility"]').attributes('aria-label')).toBe('Governance visibility')
    expect(wrapper.find('[data-testid="pipeline-editor-governance-autonomy"]').attributes('aria-label')).toBe('Autonomy ceiling: Fully autonomous')
    expect(wrapper.find('[data-testid="pipeline-editor-governance-capacity"]').attributes('title')).toContain('Current usage is not loaded')
    wrapper.unmount()
  })

  it('shows HITL details and a guardrail only from the node graph contract', () => {
    const wrapper = mountVisibility({
      mode: 'node',
      node: {
        hitl_config: { human_only: true, required_team_name: 'Platform', claim_expiry_minutes: 240 },
        policy_gate: { action: 'block', eval_id: 'eval-1' },
      },
    })

    expect(wrapper.text()).toContain('Human-only')
    expect(wrapper.text()).toContain('Team: Platform')
    expect(wrapper.text()).toContain('Expiry: 4h')
    expect(wrapper.text()).toContain('Guardrail')
    expect(wrapper.find('[data-testid="pipeline-node-governance-human-only"]').attributes('aria-label')).toBe('Human-only')
    expect(wrapper.find('[data-testid="pipeline-node-governance-expiry"]').attributes('aria-label')).toBe('Review expiry: 4h')
    expect(wrapper.html()).not.toContain('views.PipelineEditorView.governance_')
    wrapper.unmount()
  })

  it('follows the pipeline default when the ceiling is inherited', () => {
    const wrapper = mountVisibility({
      mode: 'toolbar',
      pipeline: { default_autonomy_level: 'manual_approval', max_autonomy_level: null },
    })

    expect(wrapper.text()).toContain('Ceiling: Manual approval')
    expect(wrapper.find('[data-testid="pipeline-editor-governance-autonomy"]').attributes('title')).toContain('inherits its autonomy ceiling')
    wrapper.unmount()
  })

  it('resolves a gate team id only from the team names the editor already loaded', () => {
    const wrapper = mountVisibility({
      mode: 'node',
      teamNames: { 'team-1': 'Platform' },
      node: { hitl_config: { human_only: false, required_team_id: 'team-1', claim_expiry_min: 45 } },
    })

    expect(wrapper.text()).toContain('Agent may approve')
    expect(wrapper.text()).toContain('Team: Platform')
    expect(wrapper.text()).toContain('Expiry: 45 min')
    wrapper.unmount()
  })

  it('names an unresolved gate team only as assigned, never by guessing a name', () => {
    const wrapper = mountVisibility({
      mode: 'node',
      teamNames: {},
      node: { hitl_config: { claim_team_id: 'team-unknown' } },
    })

    expect(wrapper.text()).toContain('Team: assigned')
    expect(wrapper.text()).not.toContain('team-unknown')
    wrapper.unmount()
  })

  it('does not invent HITL, guardrail, spend, or capacity values when data is absent', () => {
    const toolbar = mountVisibility({ mode: 'toolbar', pipeline: {} })
    const node = mountVisibility({ mode: 'node', node: {} })

    expect(toolbar.find('[data-testid="pipeline-editor-governance-autonomy"]').exists()).toBe(false)
    expect(toolbar.find('[data-testid="pipeline-editor-governance-capacity"]').exists()).toBe(false)
    expect(toolbar.find('[data-testid="pipeline-editor-governance-breaker"]').exists()).toBe(false)
    expect(node.find('[data-testid="pipeline-node-governance"]').exists()).toBe(false)
    expect(node.find('[data-testid="pipeline-node-governance-human-only"]').exists()).toBe(false)
    expect(node.find('[data-testid="pipeline-node-governance-team"]').exists()).toBe(false)
    expect(node.find('[data-testid="pipeline-node-governance-expiry"]').exists()).toBe(false)
    expect(node.find('[data-testid="pipeline-node-governance-guardrail"]').exists()).toBe(false)
    toolbar.unmount()
    node.unmount()
  })
})
