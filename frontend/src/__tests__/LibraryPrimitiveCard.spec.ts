import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import LibraryPrimitiveCard from '../components/library/LibraryPrimitiveCard.vue'

const i18n = createI18n({
  legacy: false,
  locale: 'en',
  messages: {
    en: {
      views: {
        LibraryView: {
          modulo_badge: 'modulo',
          community_badge: 'not verified',
          preview_badge: 'preview',
          collection_badge: 'Collection',
          create_pipeline: 'Create Pipeline',
          view_details: 'View Details',
          auto_update: 'Auto update',
          copy_to_adapt: 'Copy to Adapt',
          copy_to_adapt_creating: 'Copying...',
        },
      },
    },
  },
})

function mountCard(props: Record<string, unknown> = {}) {
  return mount(LibraryPrimitiveCard, {
    global: { plugins: [i18n] },
    props: {
      prim: {
        id: 'prim-1',
        source: 'modulo',
        primitive_type: 'workflow',
        name: 'Test Primitive',
        description: 'A test primitive',
        tags: ['tag-a', 'tag-b'],
        forked_from: null,
        auto_update: false,
      },
      badge: 'modulo',
      ...props,
    },
  })
}

describe('LibraryPrimitiveCard', () => {
  it('renders the primitive name and description', () => {
    const wrapper = mountCard()
    expect(wrapper.text()).toContain('Test Primitive')
    expect(wrapper.text()).toContain('A test primitive')
  })

  it('shows the modulo badge for native primitives', () => {
    const wrapper = mountCard({ badge: 'modulo' })
    expect(wrapper.text()).toContain('modulo')
    expect(wrapper.find('[data-testid="library-community-badge"]').exists()).toBe(false)
  })

  it('shows the community badge for community primitives', () => {
    const wrapper = mountCard({ badge: 'community' })
    expect(wrapper.find('[data-testid="library-community-badge"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('not verified')
  })

  it('shows the community badge for community-sourced primitives in the native section', () => {
    const wrapper = mountCard({
      badge: 'modulo',
      prim: { id: 'prim-1', source: 'community', primitive_type: 'workflow', name: 'Test', description: null, tags: [], forked_from: null, auto_update: false },
    })
    expect(wrapper.find('[data-testid="library-community-badge"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('not verified')
  })

  it('shows the preview badge for preview primitives', () => {
    const wrapper = mountCard({ badge: 'preview' })
    expect(wrapper.text()).toContain('preview')
  })

  it('shows the collection badge for collection primitives', () => {
    const wrapper = mountCard({ badge: 'collection' })
    expect(wrapper.find('[data-testid="library-collection-badge"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Collection')
  })

  it('renders tags and hides them when disabled', async () => {
    const withTags = mountCard()
    expect(withTags.text()).toContain('tag-a')

    await withTags.setProps({ showTags: false })
    expect(withTags.text()).not.toContain('tag-a')
  })

  it('shows +N when tags exceed 3', () => {
    const wrapper = mountCard({
      prim: {
        id: 'prim-1',
        source: 'modulo',
        primitive_type: 'workflow',
        name: 'Test',
        description: null,
        tags: ['a', 'b', 'c', 'd', 'e'],
        forked_from: null,
        auto_update: false,
      },
    })
    expect(wrapper.text()).toContain('+2')
  })

  it('renders the auto-update toggle only when enabled and forked_from exists', async () => {
    const withoutToggle = mountCard()
    expect(withoutToggle.find('[data-testid="auto-update-toggle-prim-1"]').exists()).toBe(false)

    const withToggle = mountCard({
      showAutoUpdate: true,
      prim: { id: 'prim-1', source: 'modulo', primitive_type: 'workflow', name: 'Test', description: null, tags: [], forked_from: 'parent-1', auto_update: false },
    })
    expect(withToggle.find('[data-testid="auto-update-toggle-prim-1"]').exists()).toBe(true)
  })

  it('does not render auto-update toggle when forked_from is null even if showAutoUpdate is true', () => {
    const wrapper = mountCard({
      showAutoUpdate: true,
      prim: { id: 'prim-1', source: 'modulo', primitive_type: 'workflow', name: 'Test', description: null, tags: [], forked_from: null, auto_update: false },
    })
    expect(wrapper.find('[data-testid="auto-update-toggle-prim-1"]').exists()).toBe(false)
  })

  it('emits toggle-auto-update when toggle button is clicked', async () => {
    const wrapper = mountCard({
      showAutoUpdate: true,
      prim: { id: 'prim-1', source: 'modulo', primitive_type: 'workflow', name: 'Test', description: null, tags: [], forked_from: 'parent-1', auto_update: false },
    })

    await wrapper.find('[data-testid="auto-update-toggle-prim-1"]').trigger('click')
    expect(wrapper.emitted('toggle-auto-update')).toHaveLength(1)
    expect(wrapper.emitted('toggle-auto-update')?.[0]).toEqual([expect.objectContaining({ id: 'prim-1' })])
  })

  it('emits create-pipeline and view-details events', async () => {
    const wrapper = mountCard()
    await wrapper.find('[data-testid="library-view-details"]').trigger('click')
    expect(wrapper.emitted('view-details')).toHaveLength(1)
  })

  it('renders create-pipeline button for pipeline_template type', () => {
    const wrapper = mountCard({ prim: { id: 'prim-1', source: 'modulo', primitive_type: 'pipeline_template', name: 'Template', description: null, tags: [], forked_from: null, auto_update: false } })
    expect(wrapper.find('[data-testid="library-create-pipeline"]').exists()).toBe(true)
  })

  it('renders create-pipeline button for composite type', () => {
    const wrapper = mountCard({ prim: { id: 'prim-1', source: 'modulo', primitive_type: 'composite', name: 'Composite', description: null, tags: [], forked_from: null, auto_update: false } })
    expect(wrapper.find('[data-testid="library-create-pipeline"]').exists()).toBe(true)
  })

  it('does not render the create-pipeline button for non-pipeline primitive types', () => {
    const wrapper = mountCard({ prim: { id: 'prim-1', source: 'modulo', primitive_type: 'schema', name: 'S', description: null, tags: [], forked_from: null, auto_update: false } })
    expect(wrapper.find('[data-testid="library-create-pipeline"]').exists()).toBe(false)
  })

  it('emits create-pipeline when button is clicked for pipeline_template', async () => {
    const wrapper = mountCard({ prim: { id: 'prim-1', source: 'modulo', primitive_type: 'pipeline_template', name: 'Template', description: null, tags: [], forked_from: null, auto_update: false } })
    await wrapper.find('[data-testid="library-create-pipeline"]').trigger('click')
    expect(wrapper.emitted('create-pipeline')).toHaveLength(1)
  })

  it('emits create-pipeline when button is clicked for composite', async () => {
    const wrapper = mountCard({ prim: { id: 'prim-1', source: 'modulo', primitive_type: 'composite', name: 'Composite', description: null, tags: [], forked_from: null, auto_update: false } })
    await wrapper.find('[data-testid="library-create-pipeline"]').trigger('click')
    expect(wrapper.emitted('create-pipeline')).toHaveLength(1)
  })

  it('does not render lifecycle-map creation controls in ordinary library views', () => {
    const wrapper = mountCard({ prim: { id: 'prim-1', source: 'modulo', primitive_type: 'lifecycle_map', name: 'SDLC Map', description: null, tags: [], forked_from: null, auto_update: false } })
    expect(wrapper.find('[data-testid="library-create-lifecycle-map"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="library-create-pipeline"]').exists()).toBe(false)
    expect(wrapper.emitted('create-lifecycle-map')).toBeUndefined()
  })

  it('does not render the copy-to-adapt button for non-lifecycle-map primitives', () => {
    const wrapper = mountCard()
    expect(wrapper.find('[data-testid="library-create-lifecycle-map"]').exists()).toBe(false)
  })

  it('does not expose lifecycle-map adapting state in ordinary library views', () => {
    const prim = { id: 'prim-1', source: 'modulo', primitive_type: 'lifecycle_map', name: 'SDLC Map', description: null, tags: [], forked_from: null, auto_update: false }
    const wrapper = mountCard({ prim, adapting: { 'prim-1': true } })
    expect(wrapper.find('[data-testid="library-create-lifecycle-map"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('Copying...')
  })

  it('renders correct type badge class for each primitive type', () => {
    const types = [
      { type: 'pipeline_template', expectedClass: 'badge-context-blue' },
      { type: 'workflow', expectedClass: 'badge-context-teal' },
      { type: 'agent', expectedClass: 'badge-context-purple' },
      { type: 'schema', expectedClass: 'badge-context-amber' },
      { type: 'integration', expectedClass: 'badge-context-cyan' },
      { type: 'test_fixture', expectedClass: 'badge-context-pink' },
      { type: 'composite', expectedClass: 'badge-context-green' },
      { type: 'lifecycle_map', expectedClass: 'badge-context-blue' },
    ]

    for (const { type, expectedClass } of types) {
      const wrapper = mountCard({ prim: { id: 'prim-1', source: 'modulo', primitive_type: type, name: 'Test', description: null, tags: [], forked_from: null, auto_update: false } })
      const badge = wrapper.find('span').classes()
      expect(badge).toContain(expectedClass)
    }
  })

  it('falls back to slate badge for unknown primitive type', () => {
    const wrapper = mountCard({ prim: { id: 'prim-1', source: 'modulo', primitive_type: 'unknown_type', name: 'Test', description: null, tags: [], forked_from: null, auto_update: false } })
    const badge = wrapper.find('span').classes()
    expect(badge).toContain('badge-context-slate')
  })

  it('respects toggleLoading state and disables toggle', () => {
    const wrapper = mountCard({
      showAutoUpdate: true,
      prim: { id: 'prim-1', source: 'modulo', primitive_type: 'workflow', name: 'Test', description: null, tags: [], forked_from: 'parent-1', auto_update: false },
      toggleLoading: { 'prim-1': true },
    })
    const toggle = wrapper.find('[data-testid="auto-update-toggle-prim-1"]')
    expect(toggle.attributes('disabled')).toBeDefined()
  })

  it('shows auto-update label', () => {
    const wrapper = mountCard({
      showAutoUpdate: true,
      prim: { id: 'prim-1', source: 'modulo', primitive_type: 'workflow', name: 'Test', description: null, tags: [], forked_from: 'parent-1', auto_update: false },
    })
    expect(wrapper.text()).toContain('Auto update')
  })

  it('renders toggle with correct aria-checked state', () => {
    const offWrapper = mountCard({
      showAutoUpdate: true,
      prim: { id: 'prim-1', source: 'modulo', primitive_type: 'workflow', name: 'Test', description: null, tags: [], forked_from: 'parent-1', auto_update: false },
    })
    expect(offWrapper.find('[data-testid="auto-update-toggle-prim-1"]').attributes('aria-checked')).toBe('false')

    const onWrapper = mountCard({
      showAutoUpdate: true,
      prim: { id: 'prim-1', source: 'modulo', primitive_type: 'workflow', name: 'Test', description: null, tags: [], forked_from: 'parent-1', auto_update: true },
    })
    expect(onWrapper.find('[data-testid="auto-update-toggle-prim-1"]').attributes('aria-checked')).toBe('true')
  })

  it('renders toggle with correct translate-x class based on auto_update state', () => {
    const offWrapper = mountCard({
      showAutoUpdate: true,
      prim: { id: 'prim-1', source: 'modulo', primitive_type: 'workflow', name: 'Test', description: null, tags: [], forked_from: 'parent-1', auto_update: false },
    })
    const offToggle = offWrapper.find('[data-testid="auto-update-toggle-prim-1"] span')
    expect(offToggle.classes()).toContain('translate-x-[2px]')

    const onWrapper = mountCard({
      showAutoUpdate: true,
      prim: { id: 'prim-1', source: 'modulo', primitive_type: 'workflow', name: 'Test', description: null, tags: [], forked_from: 'parent-1', auto_update: true },
    })
    const onToggle = onWrapper.find('[data-testid="auto-update-toggle-prim-1"] span')
    expect(onToggle.classes()).toContain('translate-x-[18px]')
  })

  it('handles empty description gracefully', () => {
    const wrapper = mountCard({ prim: { id: 'prim-1', source: 'modulo', primitive_type: 'workflow', name: 'Test', description: null, tags: [], forked_from: null, auto_update: false } })
    expect(wrapper.text()).toContain('Test')
    expect(wrapper.find('p').exists()).toBe(false)
  })

  it('handles empty tags array gracefully', () => {
    const wrapper = mountCard({ prim: { id: 'prim-1', source: 'modulo', primitive_type: 'workflow', name: 'Test', description: null, tags: [], forked_from: null, auto_update: false } })
    expect(wrapper.find('[data-testid="library-item-prim-1"]').exists()).toBe(true)
  })
})
