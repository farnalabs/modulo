import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import JourneyCard from '../components/lifecycle-map/JourneyCard.vue'
import ProvenanceBadge from '../components/lifecycle-map/ProvenanceBadge.vue'
import type { JourneySummary } from '../types/lifecycleMap'

const i18n = createI18n({
  legacy: false,
  locale: 'en-US',
  messages: {
    'en-US': {
      views: {
        LifecycleMapView: {
          journey: {
            detail_title: 'Journey: {journey}',
            close: 'Close',
            loading: 'Loading journey...',
            no_runs: 'No runs yet',
            run_count: '{count} run | {count} runs',
            provenance: {
              derived: 'Derived',
              reported: 'Reported',
            },
            status: {
              complete: 'Complete',
              failed: 'Failed',
              stalled: 'Stalled',
              running: 'Running',
              pending: 'Pending',
              awaiting_human: 'Awaiting Human',
              cancelled: 'Cancelled',
              eval_failed: 'Eval Failed',
              claimed: 'Claimed',
            },
          },
        },
      },
    },
  },
})

function mountBadge(provenance: string | null) {
  return mount(ProvenanceBadge, {
    global: { plugins: [i18n] },
    props: { provenance },
  })
}

describe('ProvenanceBadge', () => {
  it('renders the derived badge green', () => {
    const wrapper = mountBadge('derived')
    expect(wrapper.text()).toContain('Derived')
    expect(wrapper.classes()).toContain('badge-context-green')
    expect(wrapper.attributes('data-testid')).toBe('provenance-badge-derived')
  })

  it('renders the reported badge amber', () => {
    const wrapper = mountBadge('reported')
    expect(wrapper.text()).toContain('Reported')
    expect(wrapper.classes()).toContain('badge-context-amber')
    expect(wrapper.attributes('data-testid')).toBe('provenance-badge-reported')
  })

  it('renders nothing when provenance is null', () => {
    const wrapper = mountBadge(null)
    expect(wrapper.find('span').exists()).toBe(false)
  })
})

function makeJourney(overrides: Partial<JourneySummary> = {}): JourneySummary {
  return {
    kind: 'pr',
    ref: '123',
    canonical_work_item_id: '00000000-0000-0000-0000-000000000001',
    current_stage: {
      map_id: '00000000-0000-0000-0000-000000000002',
      version: 1,
      stage_id: 'stage-1',
      stage_name: 'Build',
      position: 0,
    },
    status: 'running',
    provenance: 'derived',
    run_count: 3,
    latest_run_id: '00000000-0000-0000-0000-000000000003',
    updated_at: '2026-08-12T10:00:00Z',
    ...overrides,
  }
}

function mountCard(journey: JourneySummary) {
  return mount(JourneyCard, {
    global: { plugins: [i18n] },
    props: { journey },
  })
}

describe('JourneyCard', () => {
  it('renders the kind:ref label', () => {
    const wrapper = mountCard(makeJourney())
    expect(wrapper.text()).toContain('pr 123')
  })

  it('renders the translated status badge', () => {
    const wrapper = mountCard(makeJourney({ status: 'running' }))
    expect(wrapper.text()).toContain('Running')
    expect(wrapper.find('[data-testid="journey-status-running"]').classes()).toContain('badge-context-blue')
  })

  it('renders the provenance badge', () => {
    const wrapper = mountCard(makeJourney({ provenance: 'reported' }))
    expect(wrapper.text()).toContain('Reported')
    expect(wrapper.find('[data-testid="provenance-badge-reported"]').exists()).toBe(true)
  })

  it('shows the run count', () => {
    const wrapper = mountCard(makeJourney({ run_count: 3 }))
    expect(wrapper.text()).toContain('×3')
  })

  it('emits open when clicked', async () => {
    const wrapper = mountCard(makeJourney())
    await wrapper.trigger('click')
    expect(wrapper.emitted('open')).toHaveLength(1)
  })
})
