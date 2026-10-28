import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
const { profileState, postMock, authHeadersMock, routerMock } = vi.hoisted(() => ({
  profileState: {
    profiles: [] as Array<Record<string, unknown>>,
    isLoading: false,
    error: null as string | null,
    fetchProfiles: vi.fn().mockResolvedValue(undefined),
    deleteProfile: vi.fn().mockResolvedValue(undefined),
  },
  postMock: vi.fn(),
  authHeadersMock: vi.fn().mockReturnValue({ Authorization: 'Bearer token' }),
  routerMock: { push: vi.fn() },
}))

vi.mock('../stores/environmentProfiles', () => ({
  useEnvironmentProfilesStore: () => profileState,
}))

vi.mock('../lib/api/client', () => ({
  api: { POST: postMock },
  getAuthHeaders: authHeadersMock,
}))

vi.mock('vue-router', () => ({
  useRouter: () => routerMock,
}))

import RunnersProfilesTab from '../views/runners/RunnersProfilesTab.vue'

const baseProfile = {
  id: 'profile-1',
  name: 'Runner one',
  description: 'Primary runner',
  provider_type: 'runner_docker',
  image_ref: 'registry.example/runner:1',
  capabilities: [],
  status: 'active',
  created_at: '2026-01-01T00:00:00Z',
}

function statusFor(overrides: Record<string, unknown> = {}) {
  return {
    aggregate_state: 'healthy' as const,
    probe_interval_seconds: 60,
    staleness_threshold_seconds: 300,
    profiles: [],
    machines: [],
    concurrency: { sandbox_concurrency_limit: null, is_default: true, preflight: { state: 'unknown' as const, detail: null, engine_cpu_count: null, engine_mem_total_mb: null, needed_cpu: null, needed_mem_mb: null } },
    ...overrides,
  }
}

async function mountView(status = statusFor()) {
  const wrapper = mount(RunnersProfilesTab, {
    props: {
      status,
      reloadStatus: vi.fn().mockResolvedValue(undefined),
    },
    global: { mocks: { $router: routerMock } },
  })
  await flushPromises()
  return wrapper
}

describe('RunnersProfilesTab', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    profileState.profiles = []
    profileState.isLoading = false
    profileState.error = null
    postMock.mockResolvedValue({ data: null, error: undefined })
    vi.stubGlobal('fetch', vi.fn())
  })

  it('renders the empty state and navigates to create', async () => {
    const wrapper = await mountView()
    expect(wrapper.text()).toContain('No runner profiles')
    await wrapper.find('[data-testid="envprofile-list-empty-create"]').trigger('click')
    expect(routerMock.push).toHaveBeenCalledWith('/admin/runners/profiles/new')
  })

  it('filters cards and renders health, image, drift, and template details', async () => {
    profileState.profiles = [
      { ...baseProfile },
      { ...baseProfile, id: 'profile-2', name: 'External', provider_type: 'e2b', description: null, status: 'paused' },
    ]
    const wrapper = await mountView(statusFor({
      profiles: [
        { id: 'profile-1', available: true, health_state: 'healthy', drift: { is_seeded: true, drifted: true, drifted_fields: ['image_ref'] }, config_json: { memory_mb: 512, cpu_limit: 2 }, network_policy: 'restricted', persistence_policy: 'ephemeral', active_workspaces: 3 },
        { id: 'profile-2', available: false, health_state: 'engine_unreachable', drift: { is_seeded: false, drifted: false, drifted_fields: [] }, config_json: {} },
      ],
      machines: [{ probe_error: 'probe failed' }],
    }))

    expect(wrapper.text()).toContain('Runner one')
    expect(wrapper.text()).toContain('External')
    expect(wrapper.text()).toContain('512')
    expect(wrapper.text()).toContain('probe failed')
    expect(wrapper.find('[data-testid="runner-profile-drift"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="runner-profile-image-pull-hint"]').exists()).toBe(false)

    await wrapper.find('[data-testid="envprofile-list-search"]').setValue('external')
    expect(wrapper.findAll('[data-testid="envprofile-list-name"]')).toHaveLength(1)
    await wrapper.find('[data-testid="envprofile-list-search"]').setValue('missing')
    await flushPromises()
    expect(wrapper.findAll('[data-testid="envprofile-list-name"]')).toHaveLength(0)
    expect(wrapper.text()).toContain('New Profile')
  })

  it('applies a template and reports an API error', async () => {
    profileState.profiles = [{ ...baseProfile }]
    const reloadStatus = vi.fn().mockResolvedValue(undefined)
    const wrapper = mount(RunnersProfilesTab, { props: { status: statusFor({ profiles: [{ id: 'profile-1', available: true, health_state: 'healthy', drift: { is_seeded: true, drifted: true, drifted_fields: [] }, config_json: {} }] }), reloadStatus } })
    await flushPromises()
    postMock.mockResolvedValueOnce({ data: null, error: { detail: 'apply rejected' } })
    await wrapper.find('[data-testid="runners-profiles-apply"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('apply rejected')

    postMock.mockResolvedValueOnce({ data: null, error: undefined })
    await wrapper.find('[data-testid="runners-profiles-apply"]').trigger('click')
    await flushPromises()
    expect(profileState.fetchProfiles).toHaveBeenCalled()
    expect(reloadStatus).toHaveBeenCalled()
  })

  it('confirms and deletes a profile, and handles delete failures', async () => {
    profileState.profiles = [{ ...baseProfile, provider_type: 'e2b' }]
    const wrapper = await mountView(statusFor({ profiles: [{ id: 'profile-1', available: true, health_state: 'healthy', drift: { is_seeded: false, drifted: false, drifted_fields: [] }, config_json: {} }] }))
    await wrapper.find('[data-testid="envprofile-list-delete"]').trigger('click')
    expect(wrapper.text()).toContain('The profile is soft-deleted')
    await wrapper.find('[data-testid="envprofile-list-delete-cancel"]').trigger('click')
    expect(wrapper.find('[data-testid="envprofile-list-delete-confirm"]').exists()).toBe(false)

    await wrapper.find('[data-testid="envprofile-list-delete"]').trigger('click')
    await wrapper.find('[data-testid="envprofile-list-delete-confirm"]').trigger('click')
    await flushPromises()
    expect(profileState.deleteProfile).toHaveBeenCalledWith('profile-1')

    profileState.profiles = [{ ...baseProfile, provider_type: 'e2b' }]
    profileState.deleteProfile.mockRejectedValueOnce(new Error('delete failed'))
    await wrapper.find('[data-testid="envprofile-list-delete"]').trigger('click')
    await wrapper.find('[data-testid="envprofile-list-delete-confirm"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('delete failed')
  })

  it('renders loading and store-error states', async () => {
    profileState.isLoading = true
    const loadingWrapper = await mountView()
    expect(loadingWrapper.find('.animate-spin').exists()).toBe(true)

    profileState.isLoading = false
    profileState.error = 'profiles unavailable'
    const errorWrapper = await mountView()
    expect(errorWrapper.text()).toContain('profiles unavailable')
  })

  it('streams connection test events, malformed data, and failure responses', async () => {
    profileState.profiles = [{ ...baseProfile, provider_type: 'e2b' }]
    const wrapper = await mountView(statusFor({ profiles: [{ id: 'profile-1', available: true, health_state: 'healthy', drift: { is_seeded: false, drifted: false, drifted_fields: [] }, config_json: {} }] }))
    const reader = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('event: provisioning\ndata: {"event":"provisioning","detail":"started"}\n\ndata: plain text\n\n'))
        controller.close()
      },
    }).getReader()
    vi.mocked(fetch).mockResolvedValueOnce(new Response(null, { status: 200 }))
    await wrapper.find('[data-testid="envprofile-test"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('No response body')

    vi.mocked(fetch).mockResolvedValueOnce(new Response(null, { status: 503 }))
    await wrapper.find('[data-testid="envprofile-test"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('HTTP 503')

    vi.mocked(fetch).mockResolvedValueOnce({ ok: true, status: 200, body: { getReader: () => reader } } as Response)
    await wrapper.find('[data-testid="envprofile-test"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('provisioning')
    expect(wrapper.text()).toContain('plain text')
    await wrapper.find('[data-testid="envprofile-test-dismiss"]').trigger('click')
    expect(wrapper.find('[data-testid="envprofile-test-panel"]').exists()).toBe(false)
  })
})
