import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import enUS from '../locales/en-US.js'
import SystemMapDraftReview from '../components/system-maps/SystemMapDraftReview.vue'

const i18n = createI18n({ legacy: false, locale: 'en-US', messages: { 'en-US': enUS } })

function draft(overrides: Record<string, unknown> = {}) {
  return {
    sources_inspected: [
      { kind: 'pipeline', identifier: 'pipeline-1', name: 'Build' },
      { kind: 'trigger', identifier: 'trigger-1', name: 'Nightly' },
    ],
    node_count: 2,
    edge_count: 1,
    inferred_nodes: [{ node_key: 'pipeline:pipeline-1' }],
    inferred_edges: [{ source_node_key: 'trigger:trigger-1', target_node_key: 'pipeline:pipeline-1' }],
    skipped: [],
    warnings: [],
    draft_map_id: 'draft-map',
    draft_revision_id: 'draft-revision',
    draft_revision_number: 3,
    published: false,
    ...overrides,
  }
}

function mountReview(props: Record<string, unknown>) {
  return mount(SystemMapDraftReview, {
    props: { draft: null, loading: false, error: null, ...props },
    global: { plugins: [i18n] },
  })
}

describe('SystemMapDraftReview', () => {
  it('prompts the operator to run discovery when there is no draft', () => {
    const wrapper = mountReview({})

    const empty = wrapper.get('[data-testid="system-map-draft-empty"]')
    expect(empty.text()).toContain('Run discovery to inspect your pipelines')
    expect(wrapper.find('[data-testid="system-map-draft-summary"]').exists()).toBe(false)
  })

  it('renders a loading state while discovery runs', () => {
    const wrapper = mountReview({ loading: true })

    expect(wrapper.get('[data-testid="system-map-draft-loading"]').text()).toContain('Inspecting your system')
    expect(wrapper.find('[data-testid="system-map-draft-empty"]').exists()).toBe(false)
  })

  it('renders an error state with the underlying message', () => {
    const wrapper = mountReview({ error: 'discovery denied' })

    const error = wrapper.get('[data-testid="system-map-draft-error"]')
    expect(error.text()).toContain('System discovery failed')
    expect(error.text()).toContain('discovery denied')
    expect(wrapper.find('[data-testid="system-map-draft-empty"]').exists()).toBe(false)
  })

  it('renders the discovery report and never claims it was published', () => {
    const wrapper = mountReview({ draft: draft() })

    expect(wrapper.get('[data-testid="system-map-draft-summary"]').text()).toContain(
      'Draft discovered 2 nodes and 1 edges from 2 sources.',
    )
    expect(wrapper.get('[data-testid="system-map-draft-revision"]').text()).toContain('3')
    expect(wrapper.get('[data-testid="system-map-draft-revision"]').text()).toContain('draft-revision')
    expect(wrapper.get('[data-testid="system-map-draft-not-published"]').text()).toContain(
      'Discovery never publishes automatically',
    )
    expect(wrapper.find('[data-testid="system-map-draft-warnings"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="system-map-draft-skipped"]').exists()).toBe(false)
  })

  it('surfaces warnings and skipped sources', () => {
    const wrapper = mountReview({
      draft: draft({
        warnings: ['Some sources were skipped'],
        skipped: [{ source: 'trigger:orphan', reason: 'no pipeline binding' }],
      }),
    })

    expect(wrapper.get('[data-testid="system-map-draft-warnings"]').text()).toContain('Some sources were skipped')
    const skipped = wrapper.get('[data-testid="system-map-draft-skipped"]')
    expect(skipped.text()).toContain('trigger:orphan')
    expect(skipped.text()).toContain('no pipeline binding')
  })
})
