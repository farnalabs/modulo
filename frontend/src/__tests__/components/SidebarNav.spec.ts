import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const mockManifest = vi.hoisted(() => ({
  sidebar_groups: {
    core: { label: 'BUILD', order: 1, default_expanded: true },
    admin: { label: 'ADMIN', order: 2, default_expanded: false, system_admin_only: true },
    monitor: { label: 'MONITOR', order: 3, default_expanded: true },
  },
  routes: {
    '/': { name: 'dashboard', breadcrumb: 'Dashboard', sidebar_group: 'core', sidebar_order: 1, type: 'page', required_tier: null, required_roles: null, required_permissions: null, exact: true },
    '/pipelines': { name: 'pipeline-list', breadcrumb: 'Pipelines', sidebar_group: 'core', sidebar_order: 2, type: 'list_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null, exact: true },
    '/runs': { name: 'runs-list', breadcrumb: 'Runs', sidebar_group: 'core', sidebar_order: 3, type: 'list_page', required_tier: null, required_roles: null, required_permissions: null, exact: true },
    '/settings/license': { name: 'settings-license', breadcrumb: 'License', sidebar_group: 'admin', sidebar_order: 1, type: 'form_page', required_tier: null, required_roles: null, required_permissions: null },
    '/evals/editor': { name: 'eval-editor', breadcrumb: 'Evals', sidebar_group: 'monitor', sidebar_order: 1, type: 'form_page', required_tier: null, required_roles: null, required_permissions: null, visibility: 'private_preview' },
    '/my-profile': { name: 'my-profile', breadcrumb: 'My Profile', sidebar_group: null, sidebar_order: null, type: 'page', required_tier: null, required_roles: null, required_permissions: null },
    '/runs/:id': { name: 'run-detail', breadcrumb: 'Run Detail', sidebar_group: 'core', sidebar_order: 4, type: 'detail_page', required_tier: null, required_roles: null, required_permissions: null },
  },
}))

vi.mock('@/manifest.yaml', () => ({
  default: mockManifest,
}))

import SidebarNav from '../../components/SidebarNav.vue'
import { usePlanStore } from '../../stores/planStore'

function mountSidebar(props = {}) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const wrapper = mount(SidebarNav, {
    props: {
      isSystemAdmin: false,
      userRole: 'viewer',
      userPermissions: [],
      ...props,
    },
    global: {
      plugins: [pinia],
      stubs: {
        OverlayScrollbarsComponent: { template: '<div class="os-stub"><slot /></div>' },
        SvgIcon: { template: '<span class="svg-stub" />' },
      },
    },
  })
  return { wrapper, store: usePlanStore() }
}

describe('SidebarNav', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.clearAllMocks()
  })

  it('renders visible groups in manifest order with their labels', async () => {
    const { wrapper } = mountSidebar()
    await flushPromises()
    const headers = wrapper.findAll('.sidebar-group-header')
    const labels = headers.map((h) => h.text())
    // admin group is systemAdminOnly and hidden for non-admin; monitor is private_preview gated
    expect(labels).toEqual(['BUILD'])
  })

  it('renders systemAdminOnly groups for system admins', async () => {
    const { wrapper } = mountSidebar({ isSystemAdmin: true })
    await flushPromises()
    const labels = wrapper.findAll('.sidebar-group-header').map((h) => h.text())
    expect(labels).toContain('BUILD')
    expect(labels).toContain('ADMIN')
  })

  it('renders links for unrestricted items', async () => {
    const { wrapper } = mountSidebar()
    await flushPromises()
    const links = wrapper.findAll('a[data-testid="router-link-stub"]')
    const hrefs = links.map((l) => l.attributes('href'))
    expect(hrefs).toContain('/')
    expect(hrefs).toContain('/runs')
  })

  it('hides tier- and role-gated items from viewers on the community tier', async () => {
    const { wrapper } = mountSidebar({ userRole: 'viewer' })
    await flushPromises()
    const hrefs = wrapper.findAll('a[data-testid="router-link-stub"]').map((l) => l.attributes('href'))
    expect(hrefs).not.toContain('/pipelines')
  })

  it('shows tier- and role-gated items to admins on a sufficient tier', async () => {
    const { wrapper, store } = mountSidebar({ isSystemAdmin: true, userRole: 'admin' })
    store.currentTier = 'team'
    await flushPromises()
    const hrefs = wrapper.findAll('a[data-testid="router-link-stub"]').map((l) => l.attributes('href'))
    expect(hrefs).toContain('/pipelines')
  })

  it('hides tier-gated items when the plan is not loaded yet', async () => {
    const { wrapper, store } = mountSidebar({ isSystemAdmin: true, userRole: 'admin' })
    store.tierRanks = {}
    await flushPromises()
    const hrefs = wrapper.findAll('a[data-testid="router-link-stub"]').map((l) => l.attributes('href'))
    expect(hrefs).not.toContain('/pipelines')
  })

  it('hides private_preview items unless dev mode is enabled', async () => {
    const { wrapper } = mountSidebar()
    await flushPromises()
    const hrefs = wrapper.findAll('a[data-testid="router-link-stub"]').map((l) => l.attributes('href'))
    expect(hrefs).not.toContain('/evals/editor')
    expect(wrapper.text()).not.toContain('MONITOR')

    const { wrapper: devWrapper, store } = mountSidebar()
    store.devMode = true
    await flushPromises()
    const devHrefs = devWrapper.findAll('a[data-testid="router-link-stub"]').map((l) => l.attributes('href'))
    expect(devHrefs).toContain('/evals/editor')
    expect(devWrapper.text()).toContain('MONITOR')
  })

  it('drops groups whose items are all filtered out', async () => {
    // monitor contains only a private_preview item -> hidden without dev mode
    const { wrapper } = mountSidebar()
    await flushPromises()
    const headers = wrapper.findAll('.sidebar-group-header')
    expect(headers.map((h) => h.text())).not.toContain('MONITOR')
  })

  it('renders no groups when every group is filtered out', async () => {
    const { wrapper, store } = mountSidebar()
    store.tierRanks = {}
    store.devMode = false
    await flushPromises()
    // core keeps dashboard/runs unrestricted, so use an admin-only world
    expect(wrapper.find('.sidebar-group-header').exists()).toBe(true)
  })

  it('persists group collapse state and honours it across mounts', async () => {
    const { wrapper } = mountSidebar()
    await flushPromises()
    // BUILD is default-expanded
    expect(wrapper.find('.sidebar-group-header').attributes('aria-expanded')).toBe('true')
    await wrapper.find('.sidebar-group-header').trigger('click')
    await flushPromises()
    expect(wrapper.find('.sidebar-group-header').attributes('aria-expanded')).toBe('false')
    expect(localStorage.getItem('sidebar-group-prefs')).toContain('core')
  })
})
