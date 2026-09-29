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
      if (url === '/api/v1/admin/billing/overview') {
        return Promise.resolve({
          data: {
            total_users: 5,
            total_teams: 2,
            total_pipelines: 12,
            plan_tier: 'community',
            plan_id: 'community',
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/admin/org') {
        return Promise.resolve({
          data: {
            id: '00000000-0000-0000-0000-000000000001',
            name: 'Test Org',
            slug: 'test-org',
            created_at: '2025-01-15T00:00:00+00:00',
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/admin/org/export') {
        return Promise.resolve({
          data: {
            exported_at: '2025-06-30T12:00:00+00:00',
          },
          error: undefined,
        })
      }
      return Promise.resolve({ data: null, error: 'Unknown route' })
    }),
    POST: vi.fn(),
    PUT: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
    PATCH: vi.fn(),
    DELETE: vi.fn().mockResolvedValue({
      data: { message: 'Organisation has been permanently deleted.', deleted_organisation_id: '00000000-0000-0000-0000-000000000001', hard_deleted_runs: 0 },
      error: undefined,
    }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

const mockPush = vi.fn()
vi.mock('vue-router', async () => {
  const actual = await vi.importActual('vue-router')
  return {
    ...actual as any,
    useRouter: () => ({ push: mockPush }),
    useRoute: () => ({ path: '/admin/org' }),
  }
})

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
    mockPush.mockClear()
    communityState.enabled = true
    communityState.error = undefined
    communityState.reject = false
    hitlWindowState.data = { hitl_review_window_seconds: null, is_default: true }
    hitlWindowState.error = undefined
    hitlWindowState.reject = false
    hitlWindowState.deferred = null
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
    expect(saved.attributes('role')).toBe('status')
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
    expect(mockPush).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('shows a loading state while the org window request is still in flight', async () => {
    let resolveGet: (value: unknown) => void = () => {}
    hitlWindowState.deferred = new Promise((resolve) => {
      resolveGet = resolve
    })
    const wrapper = await mountWindowView()

    expect(wrapper.find('[data-testid="org-hitl-review-window-value"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('Loading review window')

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
    expect(mockPush).not.toHaveBeenCalled()
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

  it('renders the delete organisation section', async () => {
    const wrapper = await mountView()
    expect(wrapper.text()).toContain('Delete Organisation')
    expect(wrapper.text()).toContain('Permanently delete')
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

  it('enables delete confirm button when correct org name is typed', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { team_rbac: true }, currentTier: 'team' })
    const wrapper = mount(AdminOrgSettingsView, {
      global: { plugins: [pinia], stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
      attachTo: document.body,
    })
    for (let i = 0; i < 10; i++) {
      await nextTick()
    }

    const deleteBtn = wrapper.findAll('button').filter(b => b.text().includes('Delete Organisation'))
    expect(deleteBtn.length).toBeGreaterThan(0)
    await deleteBtn[0].trigger('click')
    await nextTick()

    const input = document.querySelector('input[data-testid="org-delete-confirm-input"]') as HTMLInputElement
    expect(input).not.toBeNull()

    input.value = 'Wrong Name'
    input.dispatchEvent(new Event('input'))
    await nextTick()

    const confirmBtn = Array.from(document.querySelectorAll('button')).find(button => button.textContent?.includes('Permanently Delete')) as HTMLButtonElement
    expect(confirmBtn.disabled).toBe(true)

    input.value = 'Test Org'
    input.dispatchEvent(new Event('input'))
    await nextTick()

    expect(confirmBtn.disabled).toBe(false)
    wrapper.unmount()
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
})
