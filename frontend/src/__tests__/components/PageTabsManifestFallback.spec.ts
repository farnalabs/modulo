import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import { setActivePinia, createPinia } from 'pinia'
import enUS from '../../locales/en-US.js'

const i18n = createI18n({
  legacy: false,
  locale: 'en-US',
  messages: { 'en-US': enUS },
})

// FAR-47: PageTabs resolves tier-locked state from `manifest.routes`, guarded
// by a defensive `?? {}` for a manifest that omits `routes` entirely (e.g. a
// stripped or malformed bundle). That fallback is unreachable with the real
// manifest, so it needs its own mock to stay covered.
vi.mock('@/manifest.yaml', () => ({ default: {} }))

vi.mock('vue-router', () => ({
  useRoute: vi.fn(() => ({
    path: '/admin/costs',
    fullPath: '/admin/costs',
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
import { usePlanStore } from '../../stores/planStore'

function createWrapper(tabs: any[]) {
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

describe('PageTabs — manifest without routes (FAR-47 fallback)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setActivePinia(createPinia())
  })

  it('renders every tab unlocked when the manifest provides no route metadata', () => {
    // Resolved community plan: were `manifestRoutes` not defensively defaulted
    // to `{}`, these team-tier tabs would resolve as locked. Asserting them
    // unlocked therefore proves the `?? {}` fallback, not merely an unresolved
    // plan (which also renders everything unlocked).
    const store = usePlanStore()
    store.$patch({ currentTier: 'community', loaded: true })
    const wrapper = createWrapper([
      { label: 'Overview', to: '/admin/costs' },
      { label: 'Spend Limits', to: '/admin/costs/limits' },
    ])

    const links = wrapper.findAll('.page-tab')
    expect(links).toHaveLength(2)
    // No route metadata means no tab can be resolved as plan-locked…
    expect(wrapper.findAll('.page-tab.locked')).toHaveLength(0)
    expect(wrapper.findAll('[data-testid="lock-icon"]')).toHaveLength(0)
    // …and every tab keeps its own destination rather than the upgrade path.
    expect(links.map((l) => l.attributes('href'))).toEqual([
      '/admin/costs',
      '/admin/costs/limits',
    ])
  })
})
