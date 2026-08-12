import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import { setActivePinia, createPinia } from 'pinia'
import JourneyCard from '../components/lifecycle-map/JourneyCard.vue'
import ProvenanceBadge from '../components/lifecycle-map/ProvenanceBadge.vue'
import type { JourneySummary } from '../types/lifecycleMap'

vi.mock('../lib/api/auth', () => ({
  getAuthHeaders: vi.fn(() => ({ Authorization: 'Bearer token-1' })),
  attemptTokenRefresh: vi.fn(async () => true),
  clearAccessToken: vi.fn(),
  redirectToLogin: vi.fn(),
}))

import { useLifecycleMapsStore, UNATTRIBUTED_STAGE_KEY } from '../stores/lifecycleMaps'

beforeEach(() => {
  setActivePinia(createPinia())
})

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
            unattributed: 'Unattributed',
            unattributed_hint: '{count} unattributed run | {count} unattributed runs',
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
    unattributed: false,
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

  it('renders the unattributed faded state with a chip and no status badge', () => {
    const wrapper = mountCard(makeJourney({ unattributed: true, current_stage: null, run_count: 0 }))
    expect(wrapper.text()).toContain('pr 123')
    expect(wrapper.text()).toContain('Unattributed')
    expect(wrapper.classes()).toContain('border-dashed')
    expect(wrapper.classes()).toContain('opacity-60')
    expect(wrapper.find('[data-testid="journey-unattributed-pr-123"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="journey-status-running"]').exists()).toBe(false)
  })

  it('emits open when an unattributed card is clicked', async () => {
    const wrapper = mountCard(makeJourney({ unattributed: true, current_stage: null }))
    await wrapper.trigger('click')
    expect(wrapper.emitted('open')).toHaveLength(1)
  })

  it('emits open when clicked', async () => {
    const wrapper = mountCard(makeJourney())
    await wrapper.trigger('click')
    expect(wrapper.emitted('open')).toHaveLength(1)
  })
})

describe('lifecycleMaps store — unattributed grouping', () => {
  it('groups unattributed journeys under the synthetic bucket', () => {
    const store = useLifecycleMapsStore()
    store.journeys = [
      makeJourney(),
      makeJourney({ ref: '456', unattributed: true, current_stage: null }),
    ]

    expect(store.journeysByStage['stage-1']).toHaveLength(1)
    expect(store.journeysByStage['stage-1'][0].ref).toBe('123')
    expect(store.journeysByStage[UNATTRIBUTED_STAGE_KEY]).toHaveLength(1)
    expect(store.journeysByStage[UNATTRIBUTED_STAGE_KEY][0].ref).toBe('456')
    expect(store.unattributedJourneys.map((j) => j.ref)).toEqual(['456'])
  })

  it('keeps non-attributed stage-less journeys out of the grouping', () => {
    const store = useLifecycleMapsStore()
    store.journeys = [makeJourney({ current_stage: null, unattributed: false })]

    expect(store.journeysByStage).toEqual({})
    expect(store.unattributedJourneys).toEqual([])
  })
})
