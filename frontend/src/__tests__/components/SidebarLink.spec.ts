import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import SidebarLink from '../../components/SidebarLink.vue'

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

  it('does not mark a non-exact parent route active for a child path', () => {
    // setup.ts mocks useRoute() with path '/'
    const wrapper = mountLink({ to: '/pipelines', exact: true })
    expect(wrapper.find('a').attributes('aria-current')).toBeUndefined()
  })
})
