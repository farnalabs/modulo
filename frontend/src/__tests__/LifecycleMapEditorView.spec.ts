import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import LifecycleMapEditorView from '../views/lifecycle-map/LifecycleMapEditorView.vue'

const routeState = vi.hoisted(() => ({ params: { id: 'map-42' } }))

vi.mock('vue-router', () => ({
  useRoute: () => routeState,
}))

vi.mock('../components/lifecycle-map/editor/LifecycleMapEditor.vue', () => ({
  default: {
    name: 'LifecycleMapEditor',
    props: ['mapId'],
    emits: ['saved'],
    template: '<button data-testid="editor" @click="$emit(\'saved\')">{{ mapId }}</button>',
  },
}))

vi.mock('../components/BackLink.vue', () => ({
  default: {
    name: 'BackLink',
    props: ['to', 'label'],
    template: '<a :href="to" data-testid="back-link">{{ label }}</a>',
  },
}))

describe('LifecycleMapEditorView', () => {
  beforeEach(() => {
    routeState.params = { id: 'map-42' }
  })

  it('passes the route map id to the editor and renders the list back link', async () => {
    const wrapper = mount(LifecycleMapEditorView)

    expect(wrapper.find('[data-testid="back-link"]').attributes('href')).toBe('/lifecycle-maps')
    expect(wrapper.find('[data-testid="back-link"]').text()).toBe('Back to Lifecycle Maps')
    expect(wrapper.find('[data-testid="editor"]').text()).toBe('map-42')

    await wrapper.find('[data-testid="editor"]').trigger('click')
    expect(wrapper.find('[data-testid="editor"]').exists()).toBe(true)
  })

  it('uses the route parameter when the view is mounted for another map', () => {
    routeState.params = { id: 'map-99' }
    const wrapper = mount(LifecycleMapEditorView)

    expect(wrapper.find('[data-testid="editor"]').text()).toBe('map-99')
  })
})
