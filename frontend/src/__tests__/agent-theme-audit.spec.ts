import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'

import ABTestModelsView from '../views/ABTestModelsView.vue'
import AdminAuditView from '../views/AdminAuditView.vue'
import AdminFeatureFlagsView from '../views/AdminFeatureFlagsView.vue'
import AdminSpendLimitsView from '../views/AdminSpendLimitsView.vue'
import AdminUsersView from '../views/AdminUsersView.vue'
import ApiChangelogView from '../views/ApiChangelogView.vue'
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
import TeamComparisonView from '../views/TeamComparisonView.vue'
import VariantCompareView from '../views/VariantCompareView.vue'

const viewModules = {
  ABTestModelsView,
  AdminAuditView,
  AdminFeatureFlagsView,
  AdminSpendLimitsView,
  AdminUsersView,
  ApiChangelogView,
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
  TeamComparisonView,
  VariantCompareView,
}

const viewsWithAgentTheme: string[] = [
  'AdminSpendLimitsView',
  'AdminFeatureFlagsView',
  'ApiChangelogView',
  'SettingsRateLimitsView',
]

describe('agent-theme-audit', () => {
  for (const [name, component] of Object.entries(viewModules)) {
    describe(name, () => {
      it('renders without crashing', () => {
        const wrapper = mount(component)
        expect(wrapper.exists()).toBe(true)
      })

      it('has data-testid on interactive elements', () => {
        const wrapper = mount(component)
        const interactives = wrapper.findAll(
          'button, a, input, select, textarea, [role="button"], [role="switch"], [role="checkbox"]',
        )
        for (const el of interactives) {
          if (el.isVisible()) {
            expect(el.attributes('data-testid'), `${name}: ${el.element.tagName} is missing data-testid`).toBeTruthy()
          }
        }
      })

      if (viewsWithAgentTheme.includes(name)) {
        it('has data-theme="agent" on root element', () => {
          const wrapper = mount(component)
          const root = wrapper.element
          expect(root.getAttribute('data-theme')).toBe('agent')
        })
      }
    })
  }
})
