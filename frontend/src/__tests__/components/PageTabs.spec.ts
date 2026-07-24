import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { createI18n } from 'vue-i18n'
import enUS from '../../locales/en-US.js'

const i18n = createI18n({
  legacy: false,
  locale: 'en-US',
  messages: { 'en-US': enUS },
})

vi.mock('vue-router', () => ({
  useRoute: vi.fn(() => ({
    path: '/evals/editor',
    fullPath: '/evals/editor',
    params: {},
    query: {},
    hash: '',
    matched: [],
    name: null,
    redirectedFrom: undefined,
  })),
  useRouter: vi.fn(() => ({
    push: vi.fn(),
    replace: vi.fn(),
    resolve: vi.fn(),
    go: vi.fn(),
    back: vi.fn(),
    forward: vi.fn(),
  })),
}))

import PageTabs from '../../components/PageTabs.vue'

function createWrapper(tabs: any[], routePath = '/evals/editor') {
  const { useRoute } = require('vue-router')
  ;(useRoute as any).mockReturnValue({
    path: routePath,
    fullPath: routePath,
    params: {},
    query: {},
    hash: '',
    matched: [],
    name: null,
    redirectedFrom: undefined,
  })

  return mount(PageTabs as any, {
    props: { tabs },
    global: {
      plugins: [i18n],
      stubs: {
        'router-link': {
          template: '<a :href="to" data-testid="router-link-stub"><slot /></a>',
          props: ['to'],
        },
      },
    },
  })
}

describe('PageTabs', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders all tabs with labels', () => {
    const tabs = [
      { label: 'Evals', to: '/evals/editor' },
      { label: 'Proposals', to: '/evals/proposals' },
    ]
    const wrapper = createWrapper(tabs)
    const links = wrapper.findAll('[data-testid="router-link-stub"]')
    expect(links).toHaveLength(2)
    expect(links[0].text()).toContain('Evals')
    expect(links[1].text()).toContain('Proposals')
  })

  it('marks the matching route tab as active', () => {
    const tabs = [
      { label: 'Evals', to: '/evals/editor' },
      { label: 'Proposals', to: '/evals/proposals' },
    ]
    const wrapper = createWrapper(tabs, '/evals/editor')
    const links = wrapper.findAll('[data-testid="router-link-stub"]')
    expect(links[0].classes()).toContain('active')
    expect(links[1].classes()).not.toContain('active')
  })

  it('sets aria-current on active tab', () => {
    const tabs = [
      { label: 'Evals', to: '/evals/editor' },
      { label: 'Proposals', to: '/evals/proposals' },
    ]
    const wrapper = createWrapper(tabs, '/evals/editor')
    const links = wrapper.findAll('[data-testid="router-link-stub"]')
    expect(links[0].attributes('aria-current')).toBe('page')
    expect(links[1].attributes('aria-current')).toBeUndefined()
  })

  it('sets aria-selected on each tab', () => {
    const tabs = [
      { label: 'Evals', to: '/evals/editor' },
      { label: 'Proposals', to: '/evals/proposals' },
    ]
    const wrapper = createWrapper(tabs, '/evals/editor')
    const links = wrapper.findAll('[data-testid="router-link-stub"]')
    expect(links[0].attributes('aria-selected')).toBe('true')
    expect(links[1].attributes('aria-selected')).toBe('false')
  })

  it('renders an empty state when no tabs provided', () => {
    const wrapper = createWrapper([])
    const links = wrapper.findAll('[data-testid="router-link-stub"]')
    expect(links).toHaveLength(0)
  })

  it('renders icon component when provided', async () => {
    const IconComp = { template: '<svg data-testid="tab-icon" />' }
    const tabs = [
      { label: 'Dashboard', to: '/dashboard', icon: IconComp },
    ]
    const wrapper = createWrapper(tabs)
    await nextTick()
    const icon = wrapper.find('[data-testid="tab-icon"]')
    expect(icon.exists()).toBe(true)
  })

  it('renders badge when provided', () => {
    const tabs = [
      { label: 'Alerts', to: '/alerts', badge: 5 },
    ]
    const wrapper = createWrapper(tabs)
    expect(wrapper.text()).toContain('5')
  })

  it('does not render badge div when badge is undefined', () => {
    const tabs = [
      { label: 'Alerts', to: '/alerts' },
    ]
    const wrapper = createWrapper(tabs)
    const badges = wrapper.findAll('.page-tab-badge')
    expect(badges).toHaveLength(0)
  })

  it('renders badge with default primary class when no variant given', () => {
    const tabs = [
      { label: 'Alerts', to: '/alerts', badge: 'New' },
    ]
    const wrapper = createWrapper(tabs)
    const badge = wrapper.find('.page-tab-badge')
    expect(badge.exists()).toBe(true)
    expect(badge.classes()).toContain('badge-primary')
  })

  it('renders badge with warning class', () => {
    const tabs = [
      { label: 'Alerts', to: '/alerts', badge: '3', badgeVariant: 'warning' },
    ]
    const wrapper = createWrapper(tabs)
    const badge = wrapper.find('.page-tab-badge')
    expect(badge.classes()).toContain('badge-warning')
  })

  it('renders badge with destructive class', () => {
    const tabs = [
      { label: 'Alerts', to: '/alerts', badge: '3', badgeVariant: 'destructive' },
    ]
    const wrapper = createWrapper(tabs)
    const badge = wrapper.find('.page-tab-badge')
    expect(badge.classes()).toContain('badge-destructive')
  })

  it('handles nested route paths correctly for active state', () => {
    const tabs = [
      { label: 'Evals', to: '/evals/editor' },
      { label: 'Proposals', to: '/evals/proposals' },
    ]
    const wrapper = createWrapper(tabs, '/evals/proposals')
    const links = wrapper.findAll('[data-testid="router-link-stub"]')
    expect(links[0].classes()).not.toContain('active')
    expect(links[1].classes()).toContain('active')
  })

  it('sets aria-label on nav element', () => {
    const tabs = [
      { label: 'Evals', to: '/evals/editor' },
    ]
    const wrapper = createWrapper(tabs)
    const nav = wrapper.find('nav')
    expect(nav.attributes('aria-label')).toBeTruthy()
  })
})
