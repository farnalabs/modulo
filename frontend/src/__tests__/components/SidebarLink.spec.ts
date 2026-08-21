import { describe, it, expect, vi, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { useRoute } from 'vue-router'
import SidebarLink from '../../components/SidebarLink.vue'

function routeFor(path: string) {
  return {
    path,
    fullPath: path,
    params: {} as Record<string, string>,
    query: {} as Record<string, string>,
    hash: '',
    matched: [],
    name: undefined,
    redirectedFrom: undefined,
    meta: {},
  }
}

function mountLink(props = {}) {
  return mount(SidebarLink, {
    props: {
      to: '/pipelines',
      icon: 'GitFork',
      label: 'Pipelines',
      ...props,
    },
    global: {
      stubs: { SvgIcon: { template: '<span class="svg-stub" />' } },
    },
  })
}

describe('SidebarLink', () => {
  afterEach(() => {
    vi.mocked(useRoute).mockImplementation(() => routeFor('/'))
  })

  it('renders a router-link to the target route', () => {
    const wrapper = mountLink()
    expect(wrapper.find('a').attributes('href')).toBe('/pipelines')
    expect(wrapper.text()).toContain('Pipelines')
  })

  it('renders preview badges by visibility', () => {
    const publicPreview = mountLink({ visibility: 'public_preview' })
    expect(publicPreview.text()).toContain('Preview')

    const privatePreview = mountLink({ visibility: 'private_preview' })
    expect(privatePreview.text()).toContain('Dev Preview')

    const inDev = mountLink({ visibility: 'in_dev' })
    expect(inDev.text()).toContain('In Dev')

    const none = mountLink()
    expect(none.text()).not.toContain('Preview')
  })

  it('marks a non-exact parent route active for a child path', () => {
    vi.mocked(useRoute).mockReturnValue(routeFor('/pipelines/123'))
    const wrapper = mountLink({ to: '/pipelines' })
    expect(wrapper.find('a').attributes('aria-current')).toBe('page')
  })

  it('does not mark a non-exact parent route active for an unrelated path', () => {
    vi.mocked(useRoute).mockReturnValue(routeFor('/'))
    const wrapper = mountLink({ to: '/pipelines' })
    expect(wrapper.find('a').attributes('aria-current')).toBeUndefined()
  })

  it('renders icon-only when collapsed, with title and aria-label and no label text', () => {
    const wrapper = mountLink({ collapsed: true })
    expect(wrapper.find('a').attributes('title')).toBe('Pipelines')
    expect(wrapper.find('a').attributes('aria-label')).toBe('Pipelines')
    expect(wrapper.text()).not.toContain('Pipelines')
    expect(wrapper.find('a').classes()).toContain('sidebar-link')
  })

  it('keeps the active class and aria-current when collapsed on a matching route', () => {
    vi.mocked(useRoute).mockReturnValue(routeFor('/pipelines/123'))
    const wrapper = mountLink({ collapsed: true })
    expect(wrapper.find('a').attributes('aria-current')).toBe('page')
    expect(wrapper.find('a').classes()).toContain('active')
  })
})
