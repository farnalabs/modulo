import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick, type Component } from 'vue'

vi.mock('vue-router', () => ({
  useRouter: vi.fn(() => ({ push: vi.fn(), isReady: vi.fn(() => Promise.resolve()) })),
  useRoute: vi.fn(() => ({ name: 'login', params: { id: 'test-id' }, path: '/', query: {} })),
  RouterLink: { template: '<a><slot /></a>' },
}))

vi.mock('../composables/useApi', () => ({
  useApi: vi.fn(() => ({
    get: vi.fn().mockResolvedValue({ items: [] }),
    post: vi.fn().mockResolvedValue({}),
    put: vi.fn().mockResolvedValue({}),
    del: vi.fn().mockResolvedValue({}),
  })),
}))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockResolvedValue({ data: { items: [], total: 0 }, error: null }),
    POST: vi.fn().mockResolvedValue({ data: {}, error: null }),
    PUT: vi.fn().mockResolvedValue({ data: {}, error: null }),
    DELETE: vi.fn().mockResolvedValue({ response: { status: 204, ok: true }, error: null }),
  },
  getAccessToken: vi.fn(() => null),
  setAccessToken: vi.fn(),
  onAuthChange: vi.fn(),
}))

vi.mock('../stores/planStore', () => ({
  usePlanStore: vi.fn(() => ({
    fetchPlan: vi.fn(),
    currentTier: 'community',
    features: {},
    isTeam: false,
    isLoading: false,
    isFree: true,
    featureEnabled: vi.fn(() => true),
  })),
}))

import ABTestModelsView from '../views/ABTestModelsView.vue'
import AdminAuditView from '../views/AdminAuditView.vue'
import AdminFeatureFlagsView from '../views/AdminFeatureFlagsView.vue'
import AdminSpendLimitsView from '../views/AdminSpendLimitsView.vue'
import AdminUsersView from '../views/AdminUsersView.vue'
import DashboardView from '../views/DashboardView.vue'
import EvalEditorView from '../views/EvalEditorView.vue'
import FeedbackInboxView from '../views/FeedbackInboxView.vue'
import LibraryPipelineWizard from '../views/LibraryPipelineWizard.vue'
import LibraryView from '../views/LibraryView.vue'
import LoginView from '../views/LoginView.vue'
import MyProfileView from '../views/MyProfileView.vue'
import OnboardingWizard from '../views/OnboardingWizard.vue'
import PipelineEditorView from '../views/PipelineEditorView.vue'
import RunDetailView from '../views/RunDetailView.vue'
import SchemaInferenceView from '../views/SchemaInferenceView.vue'
import SettingsNotificationLogView from '../views/SettingsNotificationLogView.vue'
import SettingsObservabilityView from '../views/SettingsObservabilityView.vue'
import SettingsRateLimitsView from '../views/SettingsRateLimitsView.vue'
import SettingsRuntimeConfigView from '../views/SettingsRuntimeConfigView.vue'
import SettingsSsoView from '../views/SettingsSsoView.vue'
import SettingsTeamsView from '../views/SettingsTeamsView.vue'
import SettingsTriggerEventLogView from '../views/SettingsTriggerEventLogView.vue'
import VariantCompareView from '../views/VariantCompareView.vue'

const viewModules: Record<string, Component> = {
  ABTestModelsView,
  AdminAuditView,
  AdminFeatureFlagsView,
  AdminSpendLimitsView,
  AdminUsersView,
  DashboardView,
  EvalEditorView,
  FeedbackInboxView,
  LibraryPipelineWizard,
  LibraryView,
  LoginView,
  MyProfileView,
  OnboardingWizard,
  PipelineEditorView,
  RunDetailView,
  SchemaInferenceView,
  SettingsNotificationLogView,
  SettingsObservabilityView,
  SettingsRateLimitsView,
  SettingsRuntimeConfigView,
  SettingsSsoView,
  SettingsTeamsView,
  SettingsTriggerEventLogView,
  VariantCompareView,
}

const viewsWithAgentTheme = [
  'AdminSpendLimitsView',
  'AdminFeatureFlagsView',
  'SettingsRateLimitsView',
]

function globalStubs() {
  return {
    stubs: {
      RouterLink: true,
      VueFlow: { template: '<div><slot /></div>' },
      Background: true,
      Controls: true,
      FeatureGate: { template: '<div><slot /></div>' },
      LockIcon: true,
      Card: { template: '<div><slot /></div>' },
      CardHeader: { template: '<div><slot /></div>' },
      CardTitle: { template: '<div><slot /></div>' },
      CardDescription: { template: '<div><slot /></div>' },
      CardContent: { template: '<div><slot /></div>' },
      Input: { template: '<input v-bind="$attrs" />' },
      Button: { template: '<button type="button"><slot /></button>' },
      OwnershipPicker: true,
      SsoProviderForm: true,
      EmptyState: { template: '<div><slot /></div>' },
    },
  }
}

describe('agent-theme-audit', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  for (const [name, component] of Object.entries(viewModules)) {
    describe(name, () => {
      it('renders without crashing', async () => {
        const wrapper = mount(component, { global: globalStubs() })
        await nextTick()
        expect(wrapper.exists()).toBe(true)
      })

      it('has data-testid on interactive elements', async () => {
        const wrapper = mount(component, { global: globalStubs() })
        await nextTick()
        const interactives = wrapper.findAll(
          'button, a, input, select, textarea, [data-testid]',
        )
        for (const el of interactives) {
          if (el.isVisible()) {
            const tag = el.element.tagName.toLowerCase()
            if (tag === 'button' || tag === 'a' || tag === 'input' || tag === 'select' || tag === 'textarea') {
              expect(el.attributes('data-testid'), `${name}: ${tag} is missing data-testid`).toBeTruthy()
            }
          }
        }
      })

      if (viewsWithAgentTheme.includes(name)) {
        it('has data-theme="agent" on root element', () => {
          const wrapper = mount(component, { global: globalStubs() })
          expect(wrapper.find('[data-theme="agent"]').exists()).toBe(true)
        })
      }
    })
  }
})
