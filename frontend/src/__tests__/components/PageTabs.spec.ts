import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { createI18n } from 'vue-i18n'
import { setActivePinia, createPinia } from 'pinia'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
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

import { useRoute } from 'vue-router'
import { usePlanStore } from '../../stores/planStore'
import PageTabs from '../../components/PageTabs.vue'

function mountPageTabs(tabs: any[]) {
  return mount(PageTabs as any, {
    props: { tabs },
    global: {
      plugins: [i18n],
      stubs: {
        'router-link': {
          template: '<a :href="to" data-testid="router-link-stub" @click.prevent="$emit(\'click\', $event)"><slot /></a>',
          props: ['to'],
        },
      },
    },
  })
}

function createWrapper(tabs: any[], routePath = '/evals/editor', query: Record<string, unknown> = {}) {
  ;(useRoute as any).mockReturnValue({
    path: routePath,
    fullPath: Object.keys(query).length > 0 ? `${routePath}?${new URLSearchParams(query as Record<string, string>)}` : routePath,
    params: {},
    query,
    hash: '',
    matched: [],
    name: null,
    redirectedFrom: undefined,
  })

  return mountPageTabs(tabs)
}

/**
 * Mount against an arbitrary route shape, including the partial route objects
 * some views are mounted with (no `query` at all).
 */
function createWrapperForRoute(tabs: any[], route: Record<string, unknown>) {
  ;(useRoute as any).mockReturnValue(route)
  return mountPageTabs(tabs)
}

describe('PageTabs', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setActivePinia(createPinia())
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

  it('activates only the tab matching the current query section', () => {
    const tabs = [
      { label: 'Native', to: '/library?section=native' },
      { label: 'Community', to: '/library?section=community' },
      { label: 'Collections', to: '/library?section=collections' },
    ]
    const wrapper = createWrapper(tabs, '/library', { section: 'community' })
    const links = wrapper.findAll('[data-testid="router-link-stub"]')
    expect(links[0].classes()).not.toContain('active')
    expect(links[1].classes()).toContain('active')
    expect(links[2].classes()).not.toContain('active')
  })

  it('sets aria-label on nav element', () => {
    const tabs = [
      { label: 'Evals', to: '/evals/editor' },
    ]
    const wrapper = createWrapper(tabs)
    const nav = wrapper.find('nav')
    expect(nav.attributes('aria-label')).toBeTruthy()
  })

  it('enforces a minimum 24px-tall tab target at the mobile breakpoint', () => {
    // jsdom does not evaluate @media queries or apply SFC scoped styles
    // (vitest css:false stubs them), so assert the class-level CSS that
    // enforces the WCAG 2.5.8 target size at the mobile breakpoint.
    const mobile = mobileStyleBlock()
    expect(mobile).toMatch(/min-height:\s*(2rem|32px)/)
  })

  it('spaces adjacent mobile tabs at least 0.5rem apart', () => {
    const mobile = mobileStyleBlock()
    expect(mobile).toMatch(/gap:\s*0\.5rem/)
  })

  // ── FAR-2260: generic query matching ──────────────────────────────────────
  //
  // The active tab is resolved from the tab's own destination — path first,
  // then every query pair it declares — with no knowledge of any particular
  // view's query vocabulary.

  describe('query matching (FAR-2260)', () => {
    it('does not crash when the route has no query object', () => {
      const tabs = [
        { label: 'Browse', to: '/schemas' },
        { label: 'Editor', to: '/schemas/editor' },
      ]
      const wrapper = createWrapperForRoute(tabs, { path: '/schemas' })
      const links = wrapper.findAll('.page-tab')
      expect(links).toHaveLength(2)
      expect(links[0].classes()).toContain('active')
      expect(links[1].classes()).not.toContain('active')
    })

    it('keeps a query-bearing tab inactive when the route has no query object and the tab is not a default', () => {
      const wrapper = createWrapperForRoute(
        [{ label: 'Native', to: '/library?section=native' }],
        { path: '/library' },
      )
      expect(wrapper.find('.page-tab').classes()).not.toContain('active')
    })

    it('tolerates null query values', () => {
      const wrapper = createWrapper(
        [{ label: 'Native', to: '/library?section=native', defaultQuery: true }],
        '/library',
        { section: null },
      )
      expect(wrapper.find('.page-tab').classes()).toContain('active')
    })

    it('matches arbitrary query pairs, not a hardcoded section key', () => {
      const tabs = [
        { label: 'List', to: '/reports?view=list' },
        { label: 'High', to: '/reports?view=errors&severity=high' },
        { label: 'Low', to: '/reports?view=errors&severity=low' },
      ]
      const wrapper = createWrapper(tabs, '/reports', { view: 'errors', severity: 'low' })
      const links = wrapper.findAll('.page-tab')
      expect(links[0].classes()).not.toContain('active')
      expect(links[1].classes()).not.toContain('active')
      expect(links[2].classes()).toContain('active')
    })

    it('ignores unrelated query params when matching a tab', () => {
      const tabs = [
        { label: 'Errors', to: '/reports?view=errors' },
        { label: 'List', to: '/reports?view=list' },
      ]
      const wrapper = createWrapper(tabs, '/reports', { view: 'errors', page: '2', sort: 'asc' })
      const links = wrapper.findAll('.page-tab')
      expect(links[0].classes()).toContain('active')
      expect(links[1].classes()).not.toContain('active')
    })

    it('matches a pair when the route holds an array of values for the key', () => {
      const tabs = [
        { label: 'Alpha', to: '/reports?tag=alpha' },
        { label: 'Beta', to: '/reports?tag=beta' },
      ]
      const wrapper = createWrapper(tabs, '/reports', { tag: ['beta', 'gamma'] })
      const links = wrapper.findAll('.page-tab')
      expect(links[0].classes()).not.toContain('active')
      expect(links[1].classes()).toContain('active')
    })

    it('activates a default tab when its query keys are absent from the route', () => {
      const tabs = [
        { label: 'Native', to: '/library?section=native', defaultQuery: true },
        { label: 'Community', to: '/library?section=community' },
      ]
      const wrapper = createWrapper(tabs, '/library', {})
      const links = wrapper.findAll('.page-tab')
      expect(links[0].classes()).toContain('active')
      expect(links[1].classes()).not.toContain('active')
    })

    it('hands the default tab over as soon as its key is present on the route', () => {
      const tabs = [
        { label: 'Native', to: '/library?section=native', defaultQuery: true },
        { label: 'Community', to: '/library?section=community' },
      ]
      const wrapper = createWrapper(tabs, '/library', { section: 'community' })
      const links = wrapper.findAll('.page-tab')
      expect(links[0].classes()).not.toContain('active')
      expect(links[1].classes()).toContain('active')
    })

    it('prefers the tab with more matching query pairs on the same path', () => {
      const tabs = [
        { label: 'Errors', to: '/reports?view=errors' },
        { label: 'High', to: '/reports?view=errors&severity=high' },
      ]
      const wrapper = createWrapper(tabs, '/reports', { view: 'errors', severity: 'high' })
      const links = wrapper.findAll('.page-tab')
      expect(links[0].classes()).not.toContain('active')
      expect(links[1].classes()).toContain('active')
    })
  })

  // ── FAR-47: tier-locked tabs ──────────────────────────────────────────────
  //
  // A tier-gated tab is never hidden: it keeps its place in the bar, leads
  // with a padlock, carries the "Available on the <plan> plan" tooltip, and
  // links to the license page. The locked state comes from the manifest's
  // route metadata + the plan store, so no view wiring is involved.

  describe('tier-locked tabs (FAR-47)', () => {
    const costTabs = [
      { label: 'Overview', to: '/admin/costs' },
      { label: 'Spend Limits', to: '/admin/costs/limits' },
      { label: 'Browse', to: '/schemas' },
    ]

    it('locks team-tier tabs on the community plan with padlock + plan tooltip, keeping the bar shape', () => {
      const store = usePlanStore()
      store.$patch({ currentTier: 'community', loaded: true })
      const wrapper = createWrapper(costTabs, '/admin/costs')

      // Never hidden: every tab still occupies space in the bar.
      const links = wrapper.findAll('.page-tab')
      expect(links).toHaveLength(3)

      // Team-tier tabs lock (Overview + Spend Limits)…
      const locked = links[1]
      expect(locked.classes()).toContain('locked')
      expect(locked.attributes('href')).toBe('/settings/license')
      expect(locked.attributes('title')).toBe('Available on the Team plan')
      expect(locked.find('[data-testid="lock-icon"]').exists()).toBe(true)
      expect(links[0].classes()).toContain('locked')
      expect(links[0].attributes('href')).toBe('/settings/license')

      // …while the community-tier sibling keeps its own destination, unlocked.
      expect(links[2].classes()).not.toContain('locked')
      expect(links[2].attributes('href')).toBe('/schemas')
      expect(links[2].find('[data-testid="lock-icon"]').exists()).toBe(false)
    })

    it('gives a locked tab an accessible name that carries the label and the plan', () => {
      const store = usePlanStore()
      store.$patch({ currentTier: 'community', loaded: true })
      const wrapper = createWrapper(costTabs, '/admin/costs')

      const lockedTabs = wrapper.findAll('.page-tab.locked')
      expect(lockedTabs).toHaveLength(2)
      const spendLimits = lockedTabs[1]
      expect(spendLimits.attributes('data-testid')).toBe('page-tab-locked')
      expect(spendLimits.attributes('aria-label')).toBe('Spend Limits (Available on the Team plan)')
      // Focusable trigger with tab semantics preserved.
      expect(spendLimits.attributes('role')).toBe('tab')
      expect(spendLimits.attributes('aria-selected')).toBe('false')
    })

    it('keeps the badge on a locked tab so the bar keeps its shape', () => {
      const store = usePlanStore()
      store.$patch({ currentTier: 'community', loaded: true })
      const wrapper = createWrapper(
        [{ label: 'Spend Limits', to: '/admin/costs/limits', badge: 3, badgeVariant: 'warning' as const }],
        '/admin/costs',
      )

      const locked = wrapper.find('.page-tab.locked')
      expect(locked.find('.page-tab-badge').text()).toBe('3')
      expect(locked.find('.page-tab-badge').classes()).toContain('badge-warning')
    })

    it('does not lock team-tier tabs when the org is on the team plan', () => {
      const store = usePlanStore()
      store.$patch({ currentTier: 'team', loaded: true })
      const wrapper = createWrapper(costTabs, '/admin/costs')

      expect(wrapper.findAll('.page-tab.locked')).toHaveLength(0)
      expect(wrapper.findAll('[data-testid="lock-icon"]')).toHaveLength(0)
      const links = wrapper.findAll('.page-tab')
      expect(links.map((l) => l.attributes('href'))).toEqual([
        '/admin/costs',
        '/admin/costs/limits',
        '/schemas',
      ])
      expect(links[1].attributes('title')).toBeUndefined()
      expect(links[1].attributes('aria-label')).toBeUndefined()
    })

    it('never locks a community-tier or ungated tab, even on the community plan', () => {
      const store = usePlanStore()
      store.$patch({ currentTier: 'community', loaded: true })
      const wrapper = createWrapper(
        [
          { label: 'Browse', to: '/schemas' },
          { label: 'Evals', to: '/evals/editor' },
          { label: 'Ad hoc', to: '/no-such-route' },
        ],
        '/schemas',
      )

      expect(wrapper.findAll('.page-tab.locked')).toHaveLength(0)
      expect(wrapper.findAll('[data-testid="lock-icon"]')).toHaveLength(0)
      const links = wrapper.findAll('.page-tab')
      expect(links.map((l) => l.attributes('href'))).toEqual([
        '/schemas',
        '/evals/editor',
        '/no-such-route',
      ])
    })

    it('does not lock a team-tier tab while the plan is still resolving (no locked-flash)', () => {
      // Before the plan sources resolve, the active tier defaults to
      // `community`, so isAtMinimumTier cannot yet tell an entitled user from
      // an under-tier one. Rendering a lock here would flash at a
      // tier-entitled user; the tab must stay unlocked until the plan is known.
      const store = usePlanStore()
      store.$patch({ currentTier: 'community', loaded: false })
      const wrapper = createWrapper(
        [{ label: 'Spend Limits', to: '/admin/costs/limits' }],
        '/admin/runners',
      )

      const link = wrapper.find('.page-tab')
      expect(link.classes()).not.toContain('locked')
      expect(link.find('[data-testid="lock-icon"]').exists()).toBe(false)
      expect(link.attributes('href')).toBe('/admin/costs/limits')
    })

    it('locks a team-tier tab once the plan resolves as community', () => {
      // The complement of the case above: the same under-tier user, once the
      // plan has resolved, does get the lock treatment.
      const store = usePlanStore()
      store.$patch({ currentTier: 'community', loaded: true })
      const wrapper = createWrapper(
        [{ label: 'Spend Limits', to: '/admin/costs/limits' }],
        '/admin/runners',
      )

      const link = wrapper.find('.page-tab')
      expect(link.classes()).toContain('locked')
      expect(link.attributes('href')).toBe('/settings/license')
    })
  })
})

function mobileStyleBlock(): string {
  // vitest runs with cwd = the frontend/ vite root; resolve against it so the
  // assertion works regardless of import.meta.url's scheme under transform.
  const source = readFileSync(join(process.cwd(), 'src/components/PageTabs.vue'), 'utf-8')
  const style = source.match(/<style[^>]*>([\s\S]*?)<\/style>/)
  if (!style) throw new Error('PageTabs.vue has no <style> block')
  const s = style[1]
  const startMatch = s.match(/@media\s*\(max-width:\s*640px\)\s*\{/)
  if (!startMatch) throw new Error('PageTabs.vue has no max-width: 640px media block')
  const start = (startMatch.index ?? 0) + startMatch[0].length
  let depth = 1
  let i = start
  while (i < s.length && depth > 0) {
    if (s[i] === '{') depth++
    else if (s[i] === '}') depth--
    i++
  }
  if (depth !== 0) throw new Error('unbalanced braces in PageTabs.vue style')
  return s.slice(start, i - 1)
}
