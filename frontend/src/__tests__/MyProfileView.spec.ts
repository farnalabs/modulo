import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const { mockPut, mockGet } = vi.hoisted(() => {
  const mockPut = vi.fn().mockResolvedValue({ data: {}, error: undefined })
  const mockGet = vi.fn().mockImplementation((url: string) => {
    if (url === '/api/v1/teams/my') {
      return Promise.resolve({
        data: [
          { team_id: 't1', team_name: 'Engineering', role: 'operator' },
          { team_id: 't2', team_name: 'Design', role: 'viewer' },
        ],
        error: undefined,
      })
    }
    return Promise.resolve({
      data: {
        id: '1',
        email: 'user@example.com',
        display_name: 'Test User',
        org_role: 'admin',
        active: true,
        created_at: '2025-01-01T00:00:00Z',
        is_system_admin: false,
      },
      error: undefined,
    })
  })
  return { mockPut, mockGet }
})

vi.mock('../lib/api/client', () => ({
  api: {
    GET: mockGet,
    PUT: mockPut,
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import MyProfileView from '../views/MyProfileView.vue'
import AppSelect from '../components/shared/AppSelect.vue'
import { usePlanStore } from '../stores/planStore'

describe('MyProfileView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const wrapper = mount(MyProfileView)
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('My Profile')
  })

  it('renders the My Teams section with team names and roles', async () => {
    usePlanStore().currentTier = 'team'
    const wrapper = mount(MyProfileView)
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('My Teams')
    const teamRows = wrapper.findAll('[data-testid="my-profile-my-team"]')
    expect(teamRows.length).toBe(2)
    expect(teamRows[0].text()).toContain('Engineering')
    expect(teamRows[0].text()).toContain('Operator')
    expect(teamRows[1].text()).toContain('Design')
    expect(teamRows[1].text()).toContain('Viewer')
  })

  it('shows empty state when the user belongs to no teams', async () => {
    usePlanStore().currentTier = 'team'
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/teams/my') return Promise.resolve({ data: [], error: undefined })
      return Promise.resolve({
        data: {
          id: '1',
          email: 'user@example.com',
          display_name: 'Test User',
          org_role: 'admin',
          active: true,
          created_at: '2025-01-01T00:00:00Z',
          is_system_admin: false,
        },
        error: undefined,
      })
    })
    const wrapper = mount(MyProfileView)
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('My Teams')
    expect(wrapper.text()).toContain('not a member of any team')
  })

  it('gates the My Teams card behind the team_rbac feature on community tier', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/teams/my') {
        return Promise.resolve({
          data: undefined,
          error: { detail: 'Feature not available on your plan' },
        })
      }
      return Promise.resolve({
        data: {
          id: '1',
          email: 'user@example.com',
          display_name: 'Test User',
          org_role: 'admin',
          active: true,
          created_at: '2025-01-01T00:00:00Z',
          is_system_admin: false,
        },
        error: undefined,
      })
    })
    const wrapper = mount(MyProfileView)
    await nextTick()
    await nextTick()

    expect(wrapper.find('[data-testid="feature-gate-disabled"]').exists()).toBe(true)
  })

  it('shows the My Teams error card only when the feature is enabled', async () => {
    usePlanStore().currentTier = 'team'
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/teams/my') {
        return Promise.resolve({
          data: undefined,
          error: { detail: 'Failed to load teams' },
        })
      }
      return Promise.resolve({
        data: {
          id: '1',
          email: 'user@example.com',
          display_name: 'Test User',
          org_role: 'admin',
          active: true,
          created_at: '2025-01-01T00:00:00Z',
          is_system_admin: false,
        },
        error: undefined,
      })
    })
    const wrapper = mount(MyProfileView)
    await nextTick()
    await nextTick()

    expect(wrapper.find('[data-testid="feature-gate-disabled"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('Failed to load teams')
    expect(wrapper.findAll('[data-testid="error-alert-retry"]').length).toBe(1)
  })

  it('shows error when passwords do not match', async () => {
    const wrapper = mount(MyProfileView)
    await nextTick()

    const currentInput = wrapper.find('[data-testid="change-password-current"]')
    const newInput = wrapper.find('[data-testid="change-password-new"]')
    const confirmInput = wrapper.find('[data-testid="change-password-confirm"]')

    await currentInput.setValue('old-password')
    await newInput.setValue('new-password')
    await confirmInput.setValue('different-password')

    await wrapper.find('[data-testid="change-password-submit"]').trigger('submit')

    expect(wrapper.text()).toContain('Passwords do not match')
    expect(mockPut).not.toHaveBeenCalled()
  })

  it('shows error when new password is too short', async () => {
    const wrapper = mount(MyProfileView)
    await nextTick()

    const currentInput = wrapper.find('[data-testid="change-password-current"]')
    const newInput = wrapper.find('[data-testid="change-password-new"]')
    const confirmInput = wrapper.find('[data-testid="change-password-confirm"]')

    await currentInput.setValue('old-password')
    await newInput.setValue('short')
    await confirmInput.setValue('short')

    await wrapper.find('[data-testid="change-password-submit"]').trigger('submit')

    expect(wrapper.text()).toContain('at least 8 characters')
    expect(mockPut).not.toHaveBeenCalled()
  })

  it('shows error when new password lacks a required character class', async () => {
    const wrapper = mount(MyProfileView)
    await nextTick()

    const currentInput = wrapper.find('[data-testid="change-password-current"]')
    const newInput = wrapper.find('[data-testid="change-password-new"]')
    const confirmInput = wrapper.find('[data-testid="change-password-confirm"]')

    await currentInput.setValue('old-password')
    await newInput.setValue('all-lowercase-1')
    await confirmInput.setValue('all-lowercase-1')

    await wrapper.find('[data-testid="change-password-submit"]').trigger('submit')

    expect(wrapper.text()).toContain('Password must contain at least one uppercase letter')
    expect(mockPut).not.toHaveBeenCalled()
  })

  it('shows error when new password is the same as the current password', async () => {
    const wrapper = mount(MyProfileView)
    await nextTick()

    const currentInput = wrapper.find('[data-testid="change-password-current"]')
    const newInput = wrapper.find('[data-testid="change-password-new"]')
    const confirmInput = wrapper.find('[data-testid="change-password-confirm"]')

    await currentInput.setValue('same-password')
    await newInput.setValue('same-password')
    await confirmInput.setValue('same-password')

    await wrapper.find('[data-testid="change-password-submit"]').trigger('submit')

    expect(wrapper.text()).toContain('New password must be different')
    expect(mockPut).not.toHaveBeenCalled()
  })

  it('successfully changes password', async () => {
    const wrapper = mount(MyProfileView)
    await nextTick()

    const currentInput = wrapper.find('[data-testid="change-password-current"]')
    const newInput = wrapper.find('[data-testid="change-password-new"]')
    const confirmInput = wrapper.find('[data-testid="change-password-confirm"]')

    await currentInput.setValue('old-password')
    await newInput.setValue('New-Strong-Password-42')
    await confirmInput.setValue('New-Strong-Password-42')

    await wrapper.find('[data-testid="change-password-submit"]').trigger('submit')
    await nextTick()

    expect(mockPut).toHaveBeenCalledWith('/api/v1/me/password', {
      body: {
        current_password: 'old-password',
        new_password: 'New-Strong-Password-42',
      },
    })
    expect(wrapper.text()).toContain('Password changed successfully')
  })

  it('shows API error message on failure', async () => {
    mockPut.mockResolvedValueOnce({ data: undefined, error: 'Current password is incorrect' })

    const wrapper = mount(MyProfileView)
    await nextTick()

    const currentInput = wrapper.find('[data-testid="change-password-current"]')
    const newInput = wrapper.find('[data-testid="change-password-new"]')
    const confirmInput = wrapper.find('[data-testid="change-password-confirm"]')

    await currentInput.setValue('wrong-password')
    await newInput.setValue('New-Strong-Password-42')
    await confirmInput.setValue('New-Strong-Password-42')

    await wrapper.find('[data-testid="change-password-submit"]').trigger('submit')
    await nextTick()

    expect(wrapper.text()).toContain('Current password is incorrect')
  })

  it('surfaces network failures and resets the saving state', async () => {
    mockPut.mockRejectedValueOnce(new Error('Network Error'))

    const wrapper = mount(MyProfileView)
    await nextTick()

    const currentInput = wrapper.find('[data-testid="change-password-current"]')
    const newInput = wrapper.find('[data-testid="change-password-new"]')
    const confirmInput = wrapper.find('[data-testid="change-password-confirm"]')

    await currentInput.setValue('old-password')
    await newInput.setValue('New-Strong-Password-42')
    await confirmInput.setValue('New-Strong-Password-42')

    await wrapper.find('[data-testid="change-password-submit"]').trigger('submit')
    await nextTick()

    expect(wrapper.text()).toContain('Network Error')
    expect(wrapper.text()).not.toContain('Password changed successfully')
    const button = wrapper.find('[data-testid="change-password-submit"]')
    expect(button.attributes('disabled')).toBeUndefined()
  })

  it('does not show success when the request resolves without data or error', async () => {
    mockPut.mockResolvedValueOnce({ data: undefined, error: undefined })

    const wrapper = mount(MyProfileView)
    await nextTick()

    const currentInput = wrapper.find('[data-testid="change-password-current"]')
    const newInput = wrapper.find('[data-testid="change-password-new"]')
    const confirmInput = wrapper.find('[data-testid="change-password-confirm"]')

    await currentInput.setValue('old-password')
    await newInput.setValue('New-Strong-Password-42')
    await confirmInput.setValue('New-Strong-Password-42')

    await wrapper.find('[data-testid="change-password-submit"]').trigger('submit')
    await nextTick()

    expect(mockPut).toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('Password changed successfully')
  })
})

describe('MyProfileView HITL email alerts', () => {
  const PROFILE = {
    id: '1',
    email: 'user@example.com',
    display_name: 'Test User',
    org_role: 'admin',
    active: true,
    created_at: '2025-01-01T00:00:00Z',
    is_system_admin: false,
  }

  const PIPELINES = {
    items: [
      { id: 'p1', name: 'Pipeline One' },
      { id: 'p2', name: 'Pipeline Two' },
    ],
  }

  function mockPrefsGet(prefs: unknown, prefsError?: unknown) {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/me/hitl-email-preferences') {
        return Promise.resolve(prefsError ? { data: undefined, error: prefsError } : { data: prefs, error: undefined })
      }
      if (url === '/api/v1/pipelines') {
        return Promise.resolve({ data: PIPELINES, error: undefined })
      }
      return Promise.resolve({ data: PROFILE, error: undefined })
    })
  }

  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('renders stored preferences in the controls', async () => {
    mockPrefsGet({ default: true, pipeline_overrides: { p1: true } })
    const wrapper = mount(MyProfileView)

    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="my-profile-hitl-email-default"]').exists()).toBe(true)
    })
    await nextTick()

    expect(
      (wrapper.find('[data-testid="my-profile-hitl-email-default"]').element as HTMLInputElement).checked
    ).toBe(true)
    const selects = wrapper.findAllComponents(AppSelect)
    expect(selects.length).toBe(2)
    expect(selects[0].props('modelValue')).toBe('on')
    expect(selects[1].props('modelValue')).toBe('default')
  })

  it('renders defaults when no preference is stored', async () => {
    mockPrefsGet({ default: false, pipeline_overrides: {} })
    const wrapper = mount(MyProfileView)

    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="my-profile-hitl-email-default"]').exists()).toBe(true)
    })
    await nextTick()

    expect(
      (wrapper.find('[data-testid="my-profile-hitl-email-default"]').element as HTMLInputElement).checked
    ).toBe(false)
    const selects = wrapper.findAllComponents(AppSelect)
    expect(selects.length).toBe(2)
    expect(selects[0].props('modelValue')).toBe('default')
    expect(selects[1].props('modelValue')).toBe('default')
  })

  it('ignores a non-string override value', async () => {
    mockPrefsGet({ default: false, pipeline_overrides: {} })
    const wrapper = mount(MyProfileView)

    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="my-profile-hitl-email-default"]').exists()).toBe(true)
    })
    await nextTick()

    const selects = wrapper.findAllComponents(AppSelect)
    await selects[0].vm.$emit('update:modelValue', undefined)
    await nextTick()

    // A non-string event value is ignored; the stored choice is unchanged.
    expect(selects[0].props('modelValue')).toBe('default')
  })

  it('round-trips the saved preferences, sending only explicit overrides', async () => {
    mockPrefsGet({ default: false, pipeline_overrides: { p1: true } })
    mockPut.mockResolvedValue({ data: { default: true, pipeline_overrides: { p1: true } }, error: undefined })
    const wrapper = mount(MyProfileView)

    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="my-profile-hitl-email-save"]').exists()).toBe(true)
    })
    await nextTick()

    await wrapper.find('[data-testid="my-profile-hitl-email-default"]').setValue(true)
    await wrapper.find('[data-testid="my-profile-hitl-email-save"]').trigger('click')
    await nextTick()

    expect(mockPut).toHaveBeenCalledWith('/api/v1/me/hitl-email-preferences', {
      body: {
        default: true,
        pipeline_overrides: { p1: true },
      },
    })
    expect(wrapper.text()).toContain('HITL email preferences saved.')

    const selects = wrapper.findAllComponents(AppSelect)
    await selects[1].vm.$emit('update:modelValue', 'off')
    await wrapper.find('[data-testid="my-profile-hitl-email-save"]').trigger('click')
    await nextTick()

    expect(mockPut).toHaveBeenLastCalledWith('/api/v1/me/hitl-email-preferences', {
      body: {
        default: true,
        pipeline_overrides: { p1: true, p2: false },
      },
    })
  })

  it('clears an override back to the default before saving', async () => {
    mockPrefsGet({ default: false, pipeline_overrides: { p1: true } })
    mockPut.mockResolvedValue({ data: { default: false, pipeline_overrides: {} }, error: undefined })
    const wrapper = mount(MyProfileView)

    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="my-profile-hitl-email-save"]').exists()).toBe(true)
    })
    await nextTick()

    const selects = wrapper.findAllComponents(AppSelect)
    await selects[0].vm.$emit('update:modelValue', 'default')
    await wrapper.find('[data-testid="my-profile-hitl-email-save"]').trigger('click')
    await nextTick()

    expect(mockPut).toHaveBeenCalledWith('/api/v1/me/hitl-email-preferences', {
      body: {
        default: false,
        pipeline_overrides: {},
      },
    })
  })

  it('shows an error and hides the controls when loading preferences fails', async () => {
    mockPrefsGet(undefined, { detail: 'Failed to load preferences' })
    const wrapper = mount(MyProfileView)

    await vi.waitFor(() => {
      expect(wrapper.findAll('[data-testid="error-alert-retry"]').length).toBe(1)
    })

    expect(wrapper.text()).toContain('Failed to load preferences')
    const preferenceError = wrapper.find('[data-testid="my-profile-hitl-email-section"] > div.my-profile-hitl-email-load-error')
    expect(preferenceError.attributes('role')).toBe('alert')
    expect(preferenceError.attributes('aria-live')).toBe('assertive')
    expect(preferenceError.findAll('[data-testid="error-alert-retry"]').length).toBe(1)
    expect(preferenceError.find('[data-testid="my-profile-hitl-email-retry"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="my-profile-hitl-email-save"]').exists()).toBe(false)
  })

  it('shows an actionable error and recovers when pipelines fail to load', async () => {
    let pipelineAttempts = 0
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/me/hitl-email-preferences') {
        return Promise.resolve({ data: { default: false, pipeline_overrides: {} }, error: undefined })
      }
      if (url === '/api/v1/pipelines') {
        pipelineAttempts += 1
        if (pipelineAttempts === 1) return Promise.resolve({ data: undefined, error: { detail: 'Failed to load pipelines' } })
        return Promise.resolve({ data: PIPELINES, error: undefined })
      }
      return Promise.resolve({ data: PROFILE, error: undefined })
    })
    const wrapper = mount(MyProfileView)

    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('Failed to load pipelines')
    })

    expect(wrapper.text()).toContain('Failed to load pipelines')
    expect(wrapper.text()).not.toContain('No pipelines available.')
    const pipelinesError = wrapper.find('.my-profile-hitl-email-pipelines-error')
    expect(pipelinesError.attributes('role')).toBe('alert')
    expect(pipelinesError.attributes('aria-live')).toBe('assertive')
    await pipelinesError.find('[data-testid="error-alert-retry"]').trigger('click')

    await vi.waitFor(() => {
      expect(wrapper.findAll('[data-testid="my-profile-hitl-email-pipeline"]').length).toBe(2)
    })
    expect(pipelineAttempts).toBe(2)
  })

  it('shows an actionable error when the pipelines response carries no data', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/me/hitl-email-preferences') {
        return Promise.resolve({ data: { default: false, pipeline_overrides: {} }, error: undefined })
      }
      if (url === '/api/v1/pipelines') return Promise.resolve({ data: undefined, error: undefined })
      return Promise.resolve({ data: PROFILE, error: undefined })
    })
    const wrapper = mount(MyProfileView)

    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('Could not load pipelines for HITL email overrides.')
    })

    const pipelinesError = wrapper.find('.my-profile-hitl-email-pipelines-error')
    expect(pipelinesError.exists()).toBe(true)
    expect(pipelinesError.attributes('role')).toBe('alert')
    expect(pipelinesError.find('[data-testid="error-alert-retry"]').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('No pipelines available.')
  })

  it('shows an actionable error when the pipelines request throws', async () => {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/me/hitl-email-preferences') {
        return Promise.resolve({ data: { default: false, pipeline_overrides: {} }, error: undefined })
      }
      if (url === '/api/v1/pipelines') return Promise.reject(new Error('Pipeline network error'))
      return Promise.resolve({ data: PROFILE, error: undefined })
    })
    const wrapper = mount(MyProfileView)

    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('Pipeline network error')
    })

    const pipelinesError = wrapper.find('.my-profile-hitl-email-pipelines-error')
    expect(pipelinesError.exists()).toBe(true)
    expect(pipelinesError.attributes('role')).toBe('alert')
    expect(pipelinesError.find('[data-testid="error-alert-retry"]').exists()).toBe(true)
  })

  it('shows an error and re-enables the save button when saving fails', async () => {
    mockPrefsGet({ default: false, pipeline_overrides: {} })
    mockPut.mockResolvedValueOnce({ data: undefined, error: { detail: 'Save failed' } })
    const wrapper = mount(MyProfileView)

    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="my-profile-hitl-email-save"]').exists()).toBe(true)
    })
    await nextTick()

    await wrapper.find('[data-testid="my-profile-hitl-email-save"]').trigger('click')
    await nextTick()

    const saveError = wrapper.find('[data-testid="my-profile-hitl-email-error"]')
    expect(saveError.text()).toContain('Save failed')
    expect(saveError.attributes('role')).toBe('alert')
    expect(saveError.attributes('aria-live')).toBe('assertive')
    expect(wrapper.text()).not.toContain('HITL email preferences saved.')
    const save = wrapper.find('[data-testid="my-profile-hitl-email-save"]')
    expect(save.attributes('disabled')).toBeUndefined()
  })
})

describe('MyProfileView loading placeholders and fallbacks', () => {
  const PROFILE = {
    id: '1',
    email: 'user@example.com',
    display_name: 'Test User',
    org_role: 'admin',
    active: true,
    created_at: '2025-01-01T00:00:00Z',
    is_system_admin: false,
  }

  function mockLoadedView(profile: Record<string, unknown>) {
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/teams/my') return Promise.resolve({ data: [], error: undefined })
      if (url === '/api/v1/me/hitl-email-preferences') {
        return Promise.resolve({ data: { default: false, pipeline_overrides: {} }, error: undefined })
      }
      if (url === '/api/v1/pipelines') return Promise.resolve({ data: { items: [] }, error: undefined })
      return Promise.resolve({ data: profile, error: undefined })
    })
  }

  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('renders accessible skeleton placeholders while both sections load', async () => {
    usePlanStore().currentTier = 'team'
    mockGet.mockImplementation(() => new Promise(() => {}))

    const wrapper = mount(MyProfileView)
    await nextTick()

    const teamsLoading = wrapper.find('[data-testid="my-profile-my-teams-loading"]')
    const hitlLoading = wrapper.find('[data-testid="my-profile-hitl-email-loading"]')
    expect(teamsLoading.exists()).toBe(true)
    expect(hitlLoading.exists()).toBe(true)
    // The live region is a visually-hidden native <output> (implicit `status`
    // role, so no explicit role attribute — Web:S6819) and the skeleton blocks
    // that are layout live beside it, not inside it (phrasing-content model).
    const teamsOutput = teamsLoading.find('output')
    const hitlOutput = hitlLoading.find('output')
    expect(teamsOutput.exists()).toBe(true)
    expect(hitlOutput.exists()).toBe(true)
    expect(teamsOutput.text()).toBe('Loading')
    expect(hitlOutput.text()).toBe('Loading')
    // aria-busy sits on the placeholder layout, never on the live region's
    // ancestor: a busy ancestor defers the announcement until it clears.
    expect(teamsOutput.attributes('aria-busy')).toBeUndefined()
    expect(hitlOutput.attributes('aria-busy')).toBeUndefined()
    expect(teamsLoading.find('[aria-busy="true"]').exists()).toBe(true)
    expect(hitlLoading.find('[aria-busy="true"]').exists()).toBe(true)
  })

  it('shows a profile skeleton until the profile request resolves', async () => {
    usePlanStore().currentTier = 'team'
    let resolveProfile: (value: unknown) => void = () => {}
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/auth/me') {
        return new Promise<unknown>((resolve) => { resolveProfile = resolve })
      }
      if (url === '/api/v1/teams/my') return Promise.resolve({ data: [], error: undefined })
      if (url === '/api/v1/me/hitl-email-preferences') {
        return Promise.resolve({ data: { default: false, pipeline_overrides: {} }, error: undefined })
      }
      return Promise.resolve({ data: { items: [] }, error: undefined })
    })

    const wrapper = mount(MyProfileView)
    await nextTick()

    const skeleton = wrapper.find('[data-testid="my-profile-profile-loading"]')
    expect(skeleton.exists()).toBe(true)
    expect(skeleton.find('output').text()).toBe('Loading')
    expect(skeleton.find('[aria-busy="true"]').exists()).toBe(true)
    // The blank profile card must not flash before the data arrives.
    expect(wrapper.text()).not.toContain('Member since')

    resolveProfile({ data: PROFILE, error: undefined })
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="my-profile-profile-loading"]').exists()).toBe(false)
    })
    expect(wrapper.text()).toContain('user@example.com')
  })

  it('shows an actionable error and recovers when profile loading fails', async () => {
    usePlanStore().currentTier = 'team'
    let profileAttempts = 0
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/auth/me') {
        profileAttempts += 1
        if (profileAttempts === 1) return Promise.resolve({ data: undefined, error: { detail: 'Failed to load profile' } })
        return Promise.resolve({ data: PROFILE, error: undefined })
      }
      if (url === '/api/v1/teams/my') return Promise.resolve({ data: [], error: undefined })
      if (url === '/api/v1/me/hitl-email-preferences') {
        return Promise.resolve({ data: { default: false, pipeline_overrides: {} }, error: undefined })
      }
      return Promise.resolve({ data: { items: [] }, error: undefined })
    })
    const wrapper = mount(MyProfileView)

    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('Failed to load profile')
    })

    expect(wrapper.text()).toContain('Failed to load profile')
    const profileError = wrapper.find('.my-profile-profile-error')
    expect(profileError.attributes('role')).toBe('alert')
    expect(profileError.attributes('aria-live')).toBe('assertive')
    expect(profileError.find('[data-testid="error-alert-retry"]').exists()).toBe(true)
    expect(wrapper.find('.card p.text-lg').exists()).toBe(false)
    expect(wrapper.text()).toContain('HITL email alerts')

    await profileError.find('[data-testid="error-alert-retry"]').trigger('click')

    await vi.waitFor(() => {
      expect(wrapper.find('.card p.text-lg').text()).toContain('Test User')
    })
    expect(profileAttempts).toBe(2)
  })

  it('shows an actionable error when the profile request throws', async () => {
    usePlanStore().currentTier = 'team'
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/auth/me') return Promise.reject(new Error('Profile network error'))
      if (url === '/api/v1/teams/my') return Promise.resolve({ data: [], error: undefined })
      if (url === '/api/v1/me/hitl-email-preferences') {
        return Promise.resolve({ data: { default: false, pipeline_overrides: {} }, error: undefined })
      }
      return Promise.resolve({ data: { items: [] }, error: undefined })
    })
    const wrapper = mount(MyProfileView)

    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('Profile network error')
    })

    const profileError = wrapper.find('.my-profile-profile-error')
    expect(profileError.exists()).toBe(true)
    expect(profileError.attributes('role')).toBe('alert')
    expect(profileError.find('[data-testid="error-alert-retry"]').exists()).toBe(true)
    expect(wrapper.find('.card p.text-lg').exists()).toBe(false)
  })

  it('falls back to an em dash when org_role is missing', async () => {
    usePlanStore().currentTier = 'team'
    mockLoadedView({ ...PROFILE, org_role: '' })

    const wrapper = mount(MyProfileView)

    await vi.waitFor(() => {
      expect(wrapper.find('span.mt-1').text()).toBe('—')
    })
    expect(wrapper.text()).toContain('Member since')
  })

  it('falls back to an em dash when created_at is missing', async () => {
    usePlanStore().currentTier = 'team'
    mockLoadedView({ ...PROFILE, created_at: '' })

    const wrapper = mount(MyProfileView)

    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('Member since —')
    })
  })
})
