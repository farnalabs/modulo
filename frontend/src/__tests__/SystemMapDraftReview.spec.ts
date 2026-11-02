import { beforeEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createI18n } from 'vue-i18n'
import enUS from '../locales/en-US.js'
import SystemMapDraftReview from '../components/system-maps/SystemMapDraftReview.vue'
import { useSystemMapsStore } from '../stores/systemMaps'

const i18n = createI18n({ legacy: false, locale: 'en-US', messages: { 'en-US': enUS } })

function mountReview() {
  setActivePinia(createPinia())
  const store = useSystemMapsStore()
  const wrapper = mount(SystemMapDraftReview, { global: { plugins: [i18n] } })
  return { wrapper, store }
}

function report(overrides: Record<string, unknown> = {}) {
  return {
    sources_inspected: [
      { kind: 'pipeline', identifier: 'pipeline-1', name: 'Build' },
      { kind: 'trigger', identifier: 'trigger-1', name: 'Nightly' },
    ],
    node_count: 2,
    edge_count: 1,
    inferred_nodes: [],
    inferred_edges: [],
    skipped: [],
    warnings: [],
    draft_map_id: 'map-1',
    draft_revision_id: 'revision-1',
    draft_revision_number: 3,
    published: false,
    ...overrides,
  }
}

describe('SystemMapDraftReview', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('invites an explicit discovery run and states that nothing is published', () => {
    const { wrapper } = mountReview()

    expect(wrapper.get('[data-testid="system-map-draft-review"]').text()).toContain('Discover your system')
    expect(wrapper.text()).toContain('Publishing is not available yet')
    expect(wrapper.find('[data-testid="system-map-discovery-report"]').exists()).toBe(false)
  })

  it('renders the unpublished draft summary from the discovery report', async () => {
    const { wrapper, store } = mountReview()
    store.discoveryReport = report() as never
    await wrapper.vm.$nextTick()

    const summary = wrapper.get('[data-testid="system-map-discovery-report"]')
    expect(summary.text()).toContain('2 nodes')
    expect(summary.text()).toContain('1 edges')
    expect(summary.text()).toContain('2 sources')
    expect(summary.text()).toContain('revision 3')
    expect(wrapper.text()).toContain('Publishing is not available yet')
  })

  it('surfaces discovery warnings and skipped sources', async () => {
    const { wrapper, store } = mountReview()
    store.discoveryReport = report({
      warnings: ['Some organisation sources were skipped'],
      skipped: [{ source: 'folder-1', reason: 'unsupported source kind' }],
    }) as never
    await wrapper.vm.$nextTick()

    expect(wrapper.text()).toContain('1 warning(s)')
    expect(wrapper.text()).toContain('1 source(s) could not be mapped')
  })

  it('renders a loading state while discovery runs', async () => {
    const { wrapper, store } = mountReview()
    store.discovering = true
    await wrapper.vm.$nextTick()

    expect(wrapper.get('[data-testid="system-map-discovery-loading"]').text()).toContain('Discovering')
    expect(wrapper.find('[data-testid="system-map-discovery-report"]').exists()).toBe(false)
  })

  it('renders a distinct failure state', async () => {
    const { wrapper, store } = mountReview()
    store.discoveryError = 'discovery denied'
    await wrapper.vm.$nextTick()

    const error = wrapper.get('[data-testid="system-map-discovery-error"]')
    expect(error.text()).toContain('System discovery failed')
    expect(error.attributes('role')).toBe('alert')
    expect(wrapper.find('[data-testid="system-map-discovery-report"]').exists()).toBe(false)
  })
})
