import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const mockManifest = vi.hoisted(() => ({
  sidebar_groups: {
    build: { label: 'BUILD', order: 1, default_expanded: true },
    admin: { label: 'ADMIN', order: 2, default_expanded: false, system_admin_only: true },
    monitor: { label: 'MONITOR', order: 3, default_expanded: true },
  },
  routes: {
    '/': { name: 'dashboard', breadcrumb: 'Dashboard', sidebar_group: 'build', sidebar_order: 1, type: 'page', required_tier: null, required_roles: null, required_permissions: null, exact: true },
    '/pipelines': { name: 'pipeline-list', breadcrumb: 'Pipelines', sidebar_group: 'build', sidebar_order: 2, type: 'list_page', required_tier: 'team', required_roles: ['admin'], required_permissions: null, exact: true },
    '/runs': { name: 'runs-list', breadcrumb: 'Runs', sidebar_group: 'build', sidebar_order: 3, type: 'list_page', required_tier: null, required_roles: null, required_permissions: null, exact: true },
    '/settings/license': { name: 'settings-license', breadcrumb: 'License', sidebar_group: 'admin', sidebar_order: 1, type: 'form_page', required_tier: null, required_roles: null, required_permissions: null },
    '/evals/editor': { name: 'eval-editor', breadcrumb: 'Evals', sidebar_group: 'monitor', sidebar_order: 1, type: 'form_page', required_tier: null, required_roles: null, required_permissions: null, visibility: 'private_preview' },
    '/my-profile': { name: 'my-profile', breadcrumb: 'My Profile', sidebar_group: null, sidebar_order: null, type: 'page', required_tier: null, required_roles: null, required_permissions: null },
    '/runs/:id': { name: 'run-detail', breadcrumb: 'Run Detail', sidebar_group: 'build', sidebar_order: 4, type: 'detail_page', required_tier: null, required_roles: null, required_permissions: null },
  },
}))

vi.mock('@/manifest.yaml', () => ({
  default: mockManifest,
}))

import SidebarNav from '../../components/SidebarNav.vue'
import { usePlanStore } from '../../stores/planStore'
import { setDemoSession } from '../../lib/api/auth'

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
    // useSidebar's useStorage refs are module singletons — localStorage.clear()
    // alone leaves the in-memory refs polluted across tests (a prior test may
    // have collapsed a group). Dispatch a synthetic storage event so
    // @vueuse/core's listener resets the 'sidebar-group-prefs' ref to its
    // default empty state.
    window.dispatchEvent(
      new StorageEvent('storage', {
        key: 'sidebar-group-prefs',
        newValue: '{}',
        storageArea: localStorage,
      }),
    )
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

  it('hides private_preview items during a demo session even when dev mode is enabled', async () => {
    // FAR-535: a demo visitor on an instance with devMode ON must not see
    // private_preview surfaces in the nav (server-side denial still holds).
    setDemoSession(true)
    const { wrapper, store } = mountSidebar()
    store.devMode = true
    await flushPromises()
    const hrefs = wrapper.findAll('a[data-testid="router-link-stub"]').map((l) => l.attributes('href'))
    expect(hrefs).not.toContain('/evals/editor')
    expect(wrapper.text()).not.toContain('MONITOR')

    // Without the demo flag the dev-mode behaviour is unchanged.
    setDemoSession(false)
    const { wrapper: devWrapper, store: devStore } = mountSidebar()
    devStore.devMode = true
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

  it('keeps groups with unrestricted items visible when tier info is not loaded', async () => {
    const { wrapper, store } = mountSidebar()
    store.tierRanks = {}
    store.devMode = false
    await flushPromises()
    // core's dashboard/runs are unrestricted, so BUILD still renders even though
    // tier info has not loaded and dev mode is off
    const labels = wrapper.findAll('.sidebar-group-header').map((h) => h.text())
    expect(labels).toContain('BUILD')
  })

  it('persists group collapse state and honours it across mounts', async () => {
    const { wrapper } = mountSidebar()
    await flushPromises()
    // BUILD is default-expanded
    expect(wrapper.find('.sidebar-group-header').attributes('aria-expanded')).toBe('true')
    await wrapper.find('.sidebar-group-header').trigger('click')
    await flushPromises()
    expect(wrapper.find('.sidebar-group-header').attributes('aria-expanded')).toBe('false')
    expect(localStorage.getItem('sidebar-group-prefs')).toContain('build')
  })

  it('renders a collapsed rail with per-group disclosure controls instead of a flat list', async () => {
    const { wrapper } = mountSidebar({ collapsed: true })
    await flushPromises()
    // no full sidebar group headers in the rail
    expect(wrapper.findAll('.sidebar-group-header').length).toBe(0)
    // one disclosure button per visible group (BUILD is the only visible group for a viewer)
    const toggles = wrapper.findAll('.sidebar-group-rail-toggle')
    expect(toggles.length).toBe(1)
    const toggle = toggles[0]
    // BUILD is default-expanded -> aria-expanded true
    expect(toggle.attributes('aria-expanded')).toBe('true')
    expect(toggle.attributes('aria-controls')).toBe('sidebar-group-rail-build')
    expect(toggle.attributes('title')).toBeTruthy()
    expect(toggle.attributes('aria-label')).toBeTruthy()
    // expanded groups render their item links (dashboard and runs for a viewer)
    const links = wrapper.findAll('.sidebar-link')
    expect(links.length).toBe(2)
    expect(links.map((l) => l.attributes('href'))).toEqual(expect.arrayContaining(['/', '/runs']))
    expect(links.every((l) => l.attributes('title'))).toBe(true)
  })

  it('collapses groups in the rail, hiding their items, and persists the toggle', async () => {
    const { wrapper } = mountSidebar({ collapsed: true })
    await flushPromises()
    const toggle = wrapper.find('.sidebar-group-rail-toggle')
    expect(toggle.attributes('aria-expanded')).toBe('true')
    await toggle.trigger('click')
    await flushPromises()
    expect(wrapper.find('.sidebar-group-rail-toggle').attributes('aria-expanded')).toBe('false')
    // a collapsed group renders no item links in the rail
    expect(wrapper.findAll('.sidebar-link').length).toBe(0)
    // persisted for the next mount
    expect(localStorage.getItem('sidebar-group-prefs')).toContain('build')
  })

  it('honours per-group collapse state for system admins in the rail', async () => {
    const { wrapper } = mountSidebar({ collapsed: true, isSystemAdmin: true })
    await flushPromises()
    const toggles = wrapper.findAll('.sidebar-group-rail-toggle')
    expect(toggles.length).toBe(2)
    const byControls = Object.fromEntries(
      toggles.map((b) => [b.attributes('aria-controls'), b.attributes('aria-expanded')]),
    )
    // BUILD is default-expanded, ADMIN is default-collapsed
    expect(byControls['sidebar-group-rail-build']).toBe('true')
    expect(byControls['sidebar-group-rail-admin']).toBe('false')
    // expanded group's links render; collapsed group's links do not
    expect(wrapper.findAll('.sidebar-link').map((l) => l.attributes('href'))).toEqual(
      expect.arrayContaining(['/', '/runs']),
    )
    expect(wrapper.findAll('.sidebar-link').map((l) => l.attributes('href'))).not.toContain(
      '/settings/license',
    )
  })
})
