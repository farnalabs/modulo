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
          create_pipeline: 'Create Pipeline',
          view_details: 'View Details',
          auto_update: 'Auto update',
          copy_to_adapt: 'Copy to Adapt',
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

  it('renders tags and hides them when disabled', async () => {
    const withTags = mountCard()
    expect(withTags.text()).toContain('tag-a')

    await withTags.setProps({ showTags: false })
    expect(withTags.text()).not.toContain('tag-a')
  })

  it('renders the auto-update toggle only when enabled', async () => {
    const withoutToggle = mountCard()
    expect(withoutToggle.find('[data-testid="auto-update-toggle-prim-1"]').exists()).toBe(false)

    const withToggle = mountCard({
      showAutoUpdate: true,
      prim: { id: 'prim-1', source: 'modulo', primitive_type: 'workflow', name: 'Test', description: null, tags: [], forked_from: 'parent-1', auto_update: false },
    })
    expect(withToggle.find('[data-testid="auto-update-toggle-prim-1"]').exists()).toBe(true)
  })

  it('emits create-pipeline and view-details events', async () => {
    const wrapper = mountCard()
    await wrapper.find('[data-testid="library-view-details"]').trigger('click')
    expect(wrapper.emitted('view-details')).toHaveLength(1)
  })

  it('does not render the create-pipeline button for non-pipeline primitive types', () => {
    const wrapper = mountCard({ prim: { id: 'prim-1', source: 'modulo', primitive_type: 'schema', name: 'S', description: null, tags: [], forked_from: null, auto_update: false } })
    expect(wrapper.find('[data-testid="library-create-pipeline"]').exists()).toBe(false)
  })

  it('renders a copy-to-adapt button for lifecycle_map primitives', () => {
    const wrapper = mountCard({ prim: { id: 'prim-1', source: 'modulo', primitive_type: 'lifecycle_map', name: 'SDLC Map', description: null, tags: [], forked_from: null, auto_update: false } })
    expect(wrapper.find('[data-testid="library-create-lifecycle-map"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="library-create-pipeline"]').exists()).toBe(false)
  })

  it('emits create-lifecycle-map when the copy-to-adapt button is clicked', async () => {
    const prim = { id: 'prim-1', source: 'modulo', primitive_type: 'lifecycle_map', name: 'SDLC Map', description: null, tags: [], forked_from: null, auto_update: false }
    const wrapper = mountCard({ prim })
    await wrapper.find('[data-testid="library-create-lifecycle-map"]').trigger('click')
    expect(wrapper.emitted('create-lifecycle-map')).toHaveLength(1)
    expect(wrapper.emitted('create-lifecycle-map')![0]).toEqual([prim])
  })

  it('does not render the copy-to-adapt button for non-lifecycle-map primitives', () => {
    const wrapper = mountCard()
    expect(wrapper.find('[data-testid="library-create-lifecycle-map"]').exists()).toBe(false)
  })
})
