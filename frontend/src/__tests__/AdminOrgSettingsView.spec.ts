import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const communityState = vi.hoisted(() => ({
  enabled: true,
  error: undefined as unknown,
  reject: false,
}))

// FAR-1257: org default HITL review window endpoint state.
const hitlWindowState = vi.hoisted(() => ({
  data: { hitl_review_window_seconds: null as number | null, is_default: true } as unknown,
  error: undefined as unknown,
  reject: false,
  deferred: null as null | Promise<unknown>,
}))

// FAR-1972: org human-linked MCP HITL authority endpoint state.
const mcpAuthorityState = vi.hoisted(() => ({
  data: { enabled: true, overridden: false } as unknown,
  error: undefined as unknown,
  reject: false,
}))

// View-level data-load state (billing overview + org profile) and the
// data-export endpoint state, so the load/export/delete branches are all
// independently reachable from a test.
const orgOverviewState = vi.hoisted(() => ({
  data: {
    total_users: 5,
    total_teams: 2,
    total_pipelines: 12,
    plan_tier: 'community',
    plan_id: 'community',
  } as unknown,
  error: undefined as unknown,
  reject: false,
}))

const orgProfileState = vi.hoisted(() => ({
  data: {
    id: '00000000-0000-0000-0000-000000000001',
    name: 'Test Org',
    slug: 'test-org',
    created_at: '2025-01-15T00:00:00+00:00',
  } as unknown,
  error: undefined as unknown,
  reject: false,
}))

const exportState = vi.hoisted(() => ({
  data: { exported_at: '2025-06-30T12:00:00+00:00' } as unknown,
  error: undefined as unknown,
  reject: false,
}))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((url: string) => {
      if (url === '/api/v1/admin/org/hitl-review-window') {
        if (hitlWindowState.deferred) {
          return hitlWindowState.deferred
        }
        if (hitlWindowState.reject) {
          return Promise.reject(new Error('window unreachable'))
        }
        return Promise.resolve({ data: hitlWindowState.data, error: hitlWindowState.error })
      }
      if (url === '/api/v1/admin/org/community-objects') {
        if (communityState.reject) {
          return Promise.reject(new Error('network down'))
        }
        return Promise.resolve({
          data: { community_objects_enabled: communityState.enabled },
          error: communityState.error,
        })
      }
      if (url === '/api/v1/admin/org/mcp-human-linked-hitl-authority') {
        if (mcpAuthorityState.reject) {
          return Promise.reject(new Error('setting unreachable'))
        }
        return Promise.resolve({ data: mcpAuthorityState.data, error: mcpAuthorityState.error })
      }
      if (url === '/api/v1/admin/billing/overview') {
        if (orgOverviewState.reject) {
          return Promise.reject(new Error('overview unreachable'))
        }
        return Promise.resolve({ data: orgOverviewState.data, error: orgOverviewState.error })
      }
      if (url === '/api/v1/admin/org') {
        if (orgProfileState.reject) {
          return Promise.reject(new Error('profile unreachable'))
        }
        return Promise.resolve({ data: orgProfileState.data, error: orgProfileState.error })
      }
      if (url === '/api/v1/admin/org/export') {
        if (exportState.reject) {
          return Promise.reject(new Error('export unreachable'))
        }
        return Promise.resolve({ data: exportState.data, error: exportState.error })
      }
      return Promise.resolve({ data: null, error: 'Unknown route' })
    }),
    POST: vi.fn(),
    PUT: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
    PATCH: vi.fn(),
    DELETE: vi.fn(),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import AdminOrgSettingsView from '../views/AdminOrgSettingsView.vue'
import { usePlanStore } from '../stores/planStore'
import { api } from '../lib/api/client'

async function mountView() {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = usePlanStore()
  store.$patch({ features: { team_rbac: true }, currentTier: 'team' })
  const wrapper = mount(AdminOrgSettingsView, {
    global: { plugins: [pinia] },
  })
  for (let i = 0; i < 5; i++) {
    await nextTick()
  }
  return wrapper
}

describe('AdminOrgSettingsView', () => {
  beforeEach(() => {
    vi.clearAllMocks()

    communityState.enabled = true
    communityState.error = undefined
    communityState.reject = false
    mcpAuthorityState.data = { enabled: true, overridden: false }
    mcpAuthorityState.error = undefined
    mcpAuthorityState.reject = false
    hitlWindowState.data = { hitl_review_window_seconds: null, is_default: true }
    hitlWindowState.error = undefined
    hitlWindowState.reject = false
    hitlWindowState.deferred = null
    orgOverviewState.data = {
      total_users: 5,
      total_teams: 2,
      total_pipelines: 12,
      plan_tier: 'community',
      plan_id: 'community',
    }
    orgOverviewState.error = undefined
    orgOverviewState.reject = false
    orgProfileState.data = {
      id: '00000000-0000-0000-0000-000000000001',
      name: 'Test Org',
      slug: 'test-org',
      created_at: '2025-01-15T00:00:00+00:00',
    }
    orgProfileState.error = undefined
    orgProfileState.reject = false
    exportState.data = { exported_at: '2025-06-30T12:00:00+00:00' }
    exportState.error = undefined
    exportState.reject = false
    vi.mocked(api.PUT).mockResolvedValue({ data: {}, error: undefined } as any)
  })

  async function mountWindowView() {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { team_rbac: true }, currentTier: 'team' })
    const wrapper = mount(AdminOrgSettingsView, {
      global: { plugins: [pinia] },
    })
    await flushPromises()
    return wrapper
  }

  async function mountAttachedView() {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { team_rbac: true }, currentTier: 'team' })
    const wrapper = mount(AdminOrgSettingsView, {
      global: { plugins: [pinia], stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
      attachTo: document.body,
    })
    await flushPromises()
    return wrapper
  }

  function windowPutBodies(): unknown[] {
    return vi
      .mocked(api.PUT)
      .mock.calls.filter((c) => c[0] === '/api/v1/admin/org/hitl-review-window')
      .map((c) => (c[1] as { body?: Record<string, unknown> }).body)
  }

  // -- FAR-1257: org default HITL review window -----------------------------

  it('shows "no org default" when only the instance default applies', async () => {
    const wrapper = await mountWindowView()
    const input = wrapper.find('[data-testid="org-hitl-review-window-value"]') as any
    expect((input.element as HTMLInputElement).value).toBe('')
    expect(wrapper.find('[data-testid="org-hitl-review-window-status"]').text()).toContain('No organisation default')
    wrapper.unmount()
  })

  it('loads an existing org default in the largest whole unit that divides evenly', async () => {
    hitlWindowState.data = { hitl_review_window_seconds: 900, is_default: false }
    const wrapper = await mountWindowView()
    expect((wrapper.find('[data-testid="org-hitl-review-window-value"]').element as HTMLInputElement).value).toBe('15')
    expect((wrapper.find('[data-testid="org-hitl-review-window-unit"]').element as HTMLSelectElement).value).toBe('minutes')
    expect(wrapper.find('[data-testid="org-hitl-review-window-status"]').text()).toContain('900 seconds')
    wrapper.unmount()

    hitlWindowState.data = { hitl_review_window_seconds: 86400, is_default: false }
    const days = await mountWindowView()
    expect((days.find('[data-testid="org-hitl-review-window-value"]').element as HTMLInputElement).value).toBe('1')
    expect((days.find('[data-testid="org-hitl-review-window-unit"]').element as HTMLSelectElement).value).toBe('days')
    days.unmount()
  })

  it('saves minutes converted to seconds', async () => {
    const wrapper = await mountWindowView()
    await wrapper.find('[data-testid="org-hitl-review-window-value"]').setValue('15')
    await wrapper.find('[data-testid="org-hitl-review-window-save"]').trigger('click')
    await flushPromises()

    expect(windowPutBodies()).toEqual([{ hitl_review_window_seconds: 900 }])
    expect(wrapper.find('[data-testid="org-hitl-review-window-saved"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('saves a non-minute unit converted to seconds', async () => {
    const wrapper = await mountWindowView()
    await wrapper.find('[data-testid="org-hitl-review-window-unit"]').setValue('hours')
    await wrapper.find('[data-testid="org-hitl-review-window-value"]').setValue('2')
    await wrapper.find('[data-testid="org-hitl-review-window-save"]').trigger('click')
    await flushPromises()

    expect(windowPutBodies()).toEqual([{ hitl_review_window_seconds: 7200 }])
    wrapper.unmount()
  })

  it('clears the org default with an explicit null when the value is empty', async () => {
    hitlWindowState.data = { hitl_review_window_seconds: 900, is_default: false }
    const wrapper = await mountWindowView()
    await wrapper.find('[data-testid="org-hitl-review-window-value"]').setValue('')
    await wrapper.find('[data-testid="org-hitl-review-window-save"]').trigger('click')
    await flushPromises()

    expect(windowPutBodies()).toEqual([{ hitl_review_window_seconds: null }])
    expect(wrapper.find('[data-testid="org-hitl-review-window-status"]').text()).toContain('No organisation default')
    wrapper.unmount()
  })

  it('refuses a value below the 60 second floor without calling the API', async () => {
    const wrapper = await mountWindowView()
    await wrapper.find('[data-testid="org-hitl-review-window-unit"]').setValue('seconds')
    await wrapper.find('[data-testid="org-hitl-review-window-value"]').setValue('30')
    await wrapper.find('[data-testid="org-hitl-review-window-save"]').trigger('click')
    await flushPromises()

    expect(windowPutBodies()).toHaveLength(0)
    expect(wrapper.find('[data-testid="org-hitl-review-window-error"]').text()).toContain('60 and 604800')
    wrapper.unmount()
  })

  it('advertises whole-number bounds on the value input', async () => {
    // The native contract must not promise what the resolver refuses: min 1 /
    // step 1 (a whole number >= 1), while `resolveHitlWindowForm` stays the
    // authority on the 60-second floor and the 7-day ceiling.
    const wrapper = await mountWindowView()
    const input = wrapper.find('[data-testid="org-hitl-review-window-value"]')
    expect(input.attributes('min')).toBe('1')
    expect(input.attributes('step')).toBe('1')
    wrapper.unmount()
  })

  it('refuses a zero window without calling the API', async () => {
    // Minimum-value coverage: `0` seconds is below the envelope floor, so the
    // resolver must refuse it (the safety net has no "0 = disabled").
    const wrapper = await mountWindowView()
    await wrapper.find('[data-testid="org-hitl-review-window-unit"]').setValue('seconds')
    await wrapper.find('[data-testid="org-hitl-review-window-value"]').setValue('0')
    await wrapper.find('[data-testid="org-hitl-review-window-save"]').trigger('click')
    await flushPromises()

    expect(windowPutBodies()).toHaveLength(0)
    expect(wrapper.find('[data-testid="org-hitl-review-window-error"]').text()).toContain('60 and 604800')
    wrapper.unmount()
  })

  it('announces a successful save as a polite live region', async () => {
    const wrapper = await mountWindowView()
    await wrapper.find('[data-testid="org-hitl-review-window-value"]').setValue('15')
    await wrapper.find('[data-testid="org-hitl-review-window-save"]').trigger('click')
    await flushPromises()

    const saved = wrapper.find('[data-testid="org-hitl-review-window-saved"]')
    expect(saved.exists()).toBe(true)
    expect(saved.element.tagName).toBe('OUTPUT')
    expect(saved.attributes('role')).toBeUndefined()
    wrapper.unmount()
  })

  it('refuses a value above the 604800 second ceiling without calling the API', async () => {
    const wrapper = await mountWindowView()
    await wrapper.find('[data-testid="org-hitl-review-window-unit"]').setValue('minutes')
    await wrapper.find('[data-testid="org-hitl-review-window-value"]').setValue('10081')
    await wrapper.find('[data-testid="org-hitl-review-window-save"]').trigger('click')
    await flushPromises()

    expect(windowPutBodies()).toHaveLength(0)
    expect(wrapper.find('[data-testid="org-hitl-review-window-error"]').text()).toContain('60 and 604800')
    wrapper.unmount()
  })

  it('accepts the exact envelope bounds', async () => {
    const wrapper = await mountWindowView()
    await wrapper.find('[data-testid="org-hitl-review-window-unit"]').setValue('seconds')
    await wrapper.find('[data-testid="org-hitl-review-window-value"]').setValue('60')
    await wrapper.find('[data-testid="org-hitl-review-window-save"]').trigger('click')
    await flushPromises()

    // A successful save re-normalises the form to the largest whole unit, so
    // re-pick the unit before exercising the upper bound.
    await wrapper.find('[data-testid="org-hitl-review-window-unit"]').setValue('seconds')
    await wrapper.find('[data-testid="org-hitl-review-window-value"]').setValue('604800')
    await wrapper.find('[data-testid="org-hitl-review-window-save"]').trigger('click')
    await flushPromises()

    expect(windowPutBodies()).toEqual([
      { hitl_review_window_seconds: 60 },
      { hitl_review_window_seconds: 604800 },
    ])
    wrapper.unmount()
  })

  it('reports a load failure inline and keeps the page usable', async () => {
    hitlWindowState.reject = true
    const wrapper = await mountWindowView()

    expect(wrapper.text()).toContain('Organisation Settings')
    expect(wrapper.find('[data-testid="org-hitl-review-window-load-error"]').text()).toContain('window unreachable')
    expect(wrapper.find('[data-testid="org-hitl-review-window-retry"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('reports a save failure inline without redirecting', async () => {
    vi.mocked(api.PUT).mockResolvedValueOnce({ data: null, error: 'window rejected' } as any)
    const wrapper = await mountWindowView()
    await wrapper.find('[data-testid="org-hitl-review-window-value"]').setValue('30')
    await wrapper.find('[data-testid="org-hitl-review-window-save"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="org-hitl-review-window-error"]').text()).toContain('window rejected')
    wrapper.unmount()
  })

  // -- FAR-1269: the org window must be reachable on EVERY tier -------------
  //
  // The GET/PUT /api/v1/admin/org/hitl-review-window contract is not
  // plan-gated, so the control has to work with team_rbac OFF. This mounts
  // the REAL FeatureGate (never stubbed) in its community-tier state and
  // asserts the control sits outside the dimmed, pointer-events-none disabled
  // subtree. It fails on the pre-FAR-1269 markup, where the card lived inside
  // the gate and rendered only as an unclickable ghost.

  async function mountCommunityTierView() {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { team_rbac: false }, currentTier: 'community' })
    const wrapper = mount(AdminOrgSettingsView, {
      global: { plugins: [pinia] },
    })
    await flushPromises()
    return wrapper
  }

  it('keeps the org HITL review window reachable and interactive on the community tier', async () => {
    const wrapper = await mountCommunityTierView()

    // The team gate must genuinely be in its DISABLED state here, otherwise
    // this test would silently run in the enabled state and prove nothing.
    expect(wrapper.find('[data-testid="feature-gate-disabled"]').exists()).toBe(true)

    const input = wrapper.find('[data-testid="org-hitl-review-window-value"]')
    const save = wrapper.find('[data-testid="org-hitl-review-window-save"]')
    expect(input.exists()).toBe(true)
    expect(save.exists()).toBe(true)

    // Neither control may live inside the disabled (pointer-events-none)
    // subtree - that is exactly the pre-FAR-1269 failure mode.
    expect(input.element.closest('.pointer-events-none')).toBeNull()
    expect(save.element.closest('.pointer-events-none')).toBeNull()
    expect(
      wrapper.find('[data-testid="feature-gate-disabled"] [data-testid="org-hitl-review-window-value"]').exists(),
    ).toBe(false)

    // Every OTHER section stays gated behind the disabled overlay - the card
    // moved out, the rest of the page did not.
    const disabled = wrapper.find('[data-testid="feature-gate-disabled"]')
    expect(disabled.find('[data-testid="community-objects-toggle"]').exists()).toBe(true)
    expect(disabled.text()).not.toContain('Delete Organisation')

    // Interactive end to end: type, save, and the API is called with the
    // converted seconds payload.
    await input.setValue('15')
    await save.trigger('click')
    await flushPromises()
    expect(windowPutBodies()).toEqual([{ hitl_review_window_seconds: 900 }])
    wrapper.unmount()
  })

  it('shows a loading state while the org window request is still in flight', async () => {
    let resolveGet: (value: unknown) => void = () => {}
    hitlWindowState.deferred = new Promise((resolve) => {
      resolveGet = resolve
    })
    const wrapper = await mountWindowView()

    expect(wrapper.find('[data-testid="org-hitl-review-window-value"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="org-hitl-review-window-skeleton"]').exists()).toBe(true)

    resolveGet({ data: { hitl_review_window_seconds: 900, is_default: false }, error: undefined })
    await flushPromises()
    expect((wrapper.find('[data-testid="org-hitl-review-window-value"]').element as HTMLInputElement).value).toBe('15')
    wrapper.unmount()
  })

  it('reads an awkward second count back in seconds', async () => {
    // 90 is not whole days/hours/minutes, so the largest even unit is seconds.
    hitlWindowState.data = { hitl_review_window_seconds: 90, is_default: false }
    const wrapper = await mountWindowView()
    expect((wrapper.find('[data-testid="org-hitl-review-window-value"]').element as HTMLInputElement).value).toBe('90')
    expect((wrapper.find('[data-testid="org-hitl-review-window-unit"]').element as HTMLSelectElement).value).toBe(
      'seconds',
    )
    wrapper.unmount()
  })

  it('reports a load error carried in the response body and keeps retry available', async () => {
    hitlWindowState.error = 'window rejected'
    const wrapper = await mountWindowView()

    expect(wrapper.find('[data-testid="org-hitl-review-window-load-error"]').text()).toContain('window rejected')
    expect(wrapper.find('[data-testid="org-hitl-review-window-retry"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('reports a save rejection (thrown) inline without redirecting', async () => {
    vi.mocked(api.PUT).mockRejectedValueOnce(new Error('network down'))
    const wrapper = await mountWindowView()
    await wrapper.find('[data-testid="org-hitl-review-window-unit"]').setValue('seconds')
    await wrapper.find('[data-testid="org-hitl-review-window-value"]').setValue('120')
    await wrapper.find('[data-testid="org-hitl-review-window-save"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="org-hitl-review-window-error"]').text()).toContain('network down')
    wrapper.unmount()
  })

  it('ignores a second save while the first is still in flight', async () => {
    let resolvePut: (value: unknown) => void = () => {}
    vi.mocked(api.PUT).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolvePut = resolve
        }) as any,
    )
    const wrapper = await mountWindowView()
    const vm = wrapper.vm as any
    await wrapper.find('[data-testid="org-hitl-review-window-value"]').setValue('15')
    // Invoke the handler directly: the save button is disabled while saving, so
    // a real second click never reaches the guard — the guard is the seam under
    // test (a re-entrant save must not fire a second PUT).
    const first = vm.saveHitlReviewWindow()
    await vm.saveHitlReviewWindow()
    await flushPromises()

    expect(windowPutBodies()).toHaveLength(1)

    resolvePut({ data: {}, error: undefined })
    await first
    await flushPromises()
    wrapper.unmount()
  })

  it('reads an explicit null org default as inherit', async () => {
    // is_default=false with a null seconds value: the `?? null` fallback arm.
    hitlWindowState.data = { hitl_review_window_seconds: null, is_default: false }
    const wrapper = await mountWindowView()
    expect((wrapper.find('[data-testid="org-hitl-review-window-value"]').element as HTMLInputElement).value).toBe('')
    expect(wrapper.find('[data-testid="org-hitl-review-window-status"]').text()).toContain('No organisation default')
    wrapper.unmount()
  })

  it('treats a nullish form value as a clear and a non-numeric one as invalid', async () => {
    const wrapper = await mountWindowView()
    const vm = wrapper.vm as any
    expect(vm.resolveHitlWindowForm(null, 'minutes')).toEqual({ kind: 'clear' })
    expect(vm.resolveHitlWindowForm('abc', 'minutes')).toEqual({ kind: 'invalid' })
    wrapper.unmount()
  })

  it('renders without crashing', async () => {
    const wrapper = await mountView()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Organisation Settings')
  })

  it('renders the organisation info section', async () => {
    const wrapper = await mountView()
    expect(wrapper.text()).toContain('Organisation Info')
    expect(wrapper.text()).toContain('Test Org')
    expect(wrapper.text()).toContain('test-org')
    expect(wrapper.text()).toContain('5')
  })

  it('renders the data export section', async () => {
    const wrapper = await mountView()
    expect(wrapper.text()).toContain('Data Export')
    expect(wrapper.text()).toContain('Export All Data')
  })

  // -- View-level load failures (billing overview + org profile) ------------

  it('reports a load failure when the billing overview request is rejected', async () => {
    orgOverviewState.reject = true
    const wrapper = await mountWindowView()
    expect(wrapper.text()).toContain('Failed to load org info: Unknown error')
    expect(wrapper.find('[data-testid="error-alert-retry"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('reports a load failure carried in the billing overview response', async () => {
    orgOverviewState.error = { detail: 'billing unavailable' }
    const wrapper = await mountWindowView()
    expect(wrapper.text()).toContain('Failed to load org info: billing unavailable')
    wrapper.unmount()
  })

  it('reports a load failure when the org profile request is rejected', async () => {
    orgProfileState.reject = true
    const wrapper = await mountWindowView()
    expect(wrapper.text()).toContain('Failed to load org info: Unknown error')
    wrapper.unmount()
  })

  it('reports a load failure carried in the org profile response', async () => {
    orgProfileState.error = { detail: 'profile unavailable' }
    const wrapper = await mountWindowView()
    expect(wrapper.text()).toContain('Failed to load org info: profile unavailable')
    wrapper.unmount()
  })

  it('falls back to the unnamed-org label and an unknown date for an incomplete profile', async () => {
    orgProfileState.data = { id: '00000000-0000-0000-0000-000000000001' }
    const wrapper = await mountWindowView()
    expect(wrapper.text()).toContain('Unnamed Org')
    expect(wrapper.text()).toContain('N/A')
    wrapper.unmount()
  })

  // -- Data export ---------------------------------------------------------

  it('exports the organisation data and offers download and re-export', async () => {
    const createObjectURL = vi.fn(() => 'blob:org-export')
    const revokeObjectURL = vi.fn()
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const originalCreate = URL.createObjectURL
    const originalRevoke = URL.revokeObjectURL
    URL.createObjectURL = createObjectURL
    URL.revokeObjectURL = revokeObjectURL

    const wrapper = await mountWindowView()
    await wrapper.find('[data-testid="org-export-start"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="org-export-exported-at"]').text()).toContain('Jun 30, 2025')

    await wrapper.find('[data-testid="org-export-download"]').trigger('click')
    expect(createObjectURL).toHaveBeenCalledTimes(1)
    expect(clickSpy).toHaveBeenCalledTimes(1)
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:org-export')

    await wrapper.find('[data-testid="org-export-again"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="org-export-start"]').exists()).toBe(true)

    wrapper.unmount()
    clickSpy.mockRestore()
    URL.createObjectURL = originalCreate
    URL.revokeObjectURL = originalRevoke
  })

  it('falls back to the current time when the export payload omits the timestamp', async () => {
    exportState.data = {}
    const wrapper = await mountWindowView()
    await wrapper.find('[data-testid="org-export-start"]').trigger('click')
    await flushPromises()

    const exportedAt = wrapper.find('[data-testid="org-export-exported-at"]')
    expect(exportedAt.exists()).toBe(true)
    expect(exportedAt.text()).toContain('Exported at')
    wrapper.unmount()
  })

  it('reports an export failure carried in the response and retries successfully', async () => {
    exportState.error = { detail: 'quota exceeded' }
    const wrapper = await mountWindowView()
    await wrapper.find('[data-testid="org-export-start"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="org-export-error"]').text()).toContain('Export failed: quota exceeded')

    exportState.error = undefined
    await wrapper.find('[data-testid="org-export-retry"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="org-export-download"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('reports an export failure that throws', async () => {
    exportState.reject = true
    const wrapper = await mountWindowView()
    await wrapper.find('[data-testid="org-export-start"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="org-export-error"]').text()).toContain('Export failed: export unreachable')
    wrapper.unmount()
  })

  it('does not render an organisation delete action', async () => {
    const wrapper = await mountView()
    expect(wrapper.find('[data-testid="org-delete-open"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('Permanently Delete')
  })

  it('shows org ID in the info section', async () => {
    const wrapper = await mountView()
    expect(wrapper.text()).toContain('Org ID')
    expect(wrapper.text()).toContain('test-org')
  })

  it('displays the plan badge', async () => {
    const wrapper = await mountView()
    expect(wrapper.text()).toContain('Community')
  })

  it('displays the Team plan badge when the organisation is on the team tier', async () => {
    orgOverviewState.data = {
      total_users: 5,
      total_teams: 2,
      total_pipelines: 12,
      plan_tier: 'team',
      plan_id: 'team',
    }
    const wrapper = await mountWindowView()
    expect(wrapper.find('.badge-context-purple').text()).toBe('Team')
    wrapper.unmount()
  })

  it('does not expose the removed delete confirmation flow', async () => {
    const wrapper = await mountAttachedView()
    expect(wrapper.find('[data-testid="org-delete-confirm-input"]').exists()).toBe(false)
    expect(api.DELETE).not.toHaveBeenCalled()
    wrapper.unmount()
    document.body.innerHTML = ''
  })

  it('renders the community objects kill switch', async () => {
    const wrapper = await mountView()
    const toggle = wrapper.find('[data-testid="community-objects-toggle"]')
    expect(toggle.exists()).toBe(true)
    expect(toggle.attributes('aria-checked')).toBe('true')
    wrapper.unmount()
  })

  it('reflects a disabled community objects flag loaded from the API', async () => {
    communityState.enabled = false
    const wrapper = await mountView()
    const toggle = wrapper.find('[data-testid="community-objects-toggle"]')
    expect(toggle.attributes('aria-checked')).toBe('false')
    wrapper.unmount()
  })

  it('fails open (stays enabled) when the community objects flag cannot be loaded', async () => {
    communityState.reject = true
    const wrapper = await mountView()
    const toggle = wrapper.find('[data-testid="community-objects-toggle"]')
    expect(toggle.attributes('aria-checked')).toBe('true')
    wrapper.unmount()
  })

  it('toggles community objects off and persists the change', async () => {
    const wrapper = await mountView()
    const putMock = vi.mocked(api.PUT)
    putMock.mockResolvedValue({ data: {}, error: undefined } as any)

    const toggle = wrapper.find('[data-testid="community-objects-toggle"]')
    await toggle.trigger('click')
    await vi.waitFor(() => {
      expect(toggle.attributes('aria-checked')).toBe('false')
    })

    expect(putMock).toHaveBeenCalledWith(
      '/api/v1/admin/org/community-objects',
      expect.objectContaining({ body: { community_objects_enabled: false } }),
    )
    wrapper.unmount()
  })

  it('shows an error when the community objects toggle request fails', async () => {
    const wrapper = await mountView()
    vi.mocked(api.PUT).mockResolvedValueOnce({ data: null, error: 'Server exploded' } as any)

    const toggle = wrapper.find('[data-testid="community-objects-toggle"]')
    await toggle.trigger('click')
    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('Server exploded')
    })
    expect(toggle.attributes('aria-checked')).toBe('true')
    wrapper.unmount()
  })

  it('shows an error when the community objects toggle request throws', async () => {
    const wrapper = await mountView()
    vi.mocked(api.PUT).mockRejectedValueOnce(new Error('network down'))

    const toggle = wrapper.find('[data-testid="community-objects-toggle"]')
    await toggle.trigger('click')
    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('network down')
    })
    wrapper.unmount()
  })

  // -- FAR-1972: human-linked MCP HITL authority ----------------------------

  it('loads the human-linked MCP authority setting and renders the toggle on', async () => {
    const wrapper = await mountWindowView()
    const toggle = wrapper.find('[data-testid="org-mcp-authority-toggle"]')
    expect(toggle.exists()).toBe(true)
    expect(toggle.attributes('aria-checked')).toBe('true')
    expect(wrapper.find('[data-testid="org-mcp-authority-status"]').text()).toContain('may decide a human-only gate')
    wrapper.unmount()
  })

  it('renders the toggle off and the stricter status when the org disabled it', async () => {
    mcpAuthorityState.data = { enabled: false, overridden: true }
    const wrapper = await mountWindowView()
    expect(wrapper.find('[data-testid="org-mcp-authority-toggle"]').attributes('aria-checked')).toBe('false')
    expect(wrapper.find('[data-testid="org-mcp-authority-status"]').text()).toContain('No MCP credential may decide')
    wrapper.unmount()
  })

  it('PUTs the org override and confirms the save', async () => {
    const wrapper = await mountWindowView()
    const putMock = vi.mocked(api.PUT)
    putMock.mockResolvedValue({ data: {}, error: undefined } as any)

    await wrapper.find('[data-testid="org-mcp-authority-toggle"]').trigger('click')
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="org-mcp-authority-saved"]').exists()).toBe(true)
    })

    expect(putMock).toHaveBeenCalledWith(
      '/api/v1/admin/org/mcp-human-linked-hitl-authority',
      expect.objectContaining({ body: { enabled: false } }),
    )
    expect(wrapper.find('[data-testid="org-mcp-authority-toggle"]').attributes('aria-checked')).toBe('false')
    // Announced politely without the `status` role (Web:S6819): a long-lived
    // region whose content updates, not an atomic status message.
    expect(wrapper.find('[data-testid="org-mcp-authority-saved"]').attributes('aria-live')).toBe('polite')
    expect(wrapper.find('[data-testid="org-mcp-authority-saved"]').attributes('role')).toBeUndefined()
    wrapper.unmount()
  })

  it('shows an error and keeps the toggle on when the authority save fails', async () => {
    const wrapper = await mountWindowView()
    vi.mocked(api.PUT).mockResolvedValueOnce({ data: null, error: 'Server exploded' } as any)

    await wrapper.find('[data-testid="org-mcp-authority-toggle"]').trigger('click')
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="org-mcp-authority-error"]').text()).toContain('Server exploded')
    })
    expect(wrapper.find('[data-testid="org-mcp-authority-toggle"]').attributes('aria-checked')).toBe('true')
    wrapper.unmount()
  })

  it('shows the retry affordance when the authority setting cannot be loaded', async () => {
    mcpAuthorityState.reject = true
    const wrapper = await mountWindowView()
    expect(wrapper.find('[data-testid="org-mcp-authority-load-error"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="org-mcp-authority-retry"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('reports a load error carried in the response body and keeps retry available', async () => {
    mcpAuthorityState.error = 'setting rejected'
    const wrapper = await mountWindowView()
    expect(wrapper.find('[data-testid="org-mcp-authority-load-error"]').text()).toContain('setting rejected')
    expect(wrapper.find('[data-testid="org-mcp-authority-retry"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('defaults to enabled when the authority payload omits the flag', async () => {
    // `enabled` is optional on the response; an older/partial payload must not
    // render the toggle OFF (which would read as "credentials are refused").
    mcpAuthorityState.data = {}
    const wrapper = await mountWindowView()
    expect(wrapper.find('[data-testid="org-mcp-authority-toggle"]').attributes('aria-checked')).toBe('true')
    expect(wrapper.find('[data-testid="org-mcp-authority-status"]').text()).toContain('may decide a human-only gate')
    wrapper.unmount()
  })

  it('shows an error and keeps the toggle on when the authority save throws', async () => {
    const wrapper = await mountWindowView()
    vi.mocked(api.PUT).mockRejectedValueOnce(new Error('network down'))

    await wrapper.find('[data-testid="org-mcp-authority-toggle"]').trigger('click')
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="org-mcp-authority-error"]').text()).toContain('network down')
    })
    expect(wrapper.find('[data-testid="org-mcp-authority-toggle"]').attributes('aria-checked')).toBe('true')
    expect(wrapper.find('[data-testid="org-mcp-authority-saved"]').exists()).toBe(false)
    wrapper.unmount()
  })
})
