import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'

const reloadStatus = vi.fn().mockResolvedValue(undefined)

vi.mock('../../lib/api/client', async (importOriginal) => {
  const actual = (await importOriginal()) as Record<string, unknown>
  return {
    ...actual,
    api: {
      GET: vi.fn().mockResolvedValue({ data: null, error: null }),
      POST: vi.fn().mockResolvedValue({ data: null, error: null }),
      PUT: vi.fn().mockResolvedValue({ data: null, error: null }),
    },
    getAuthHeaders: vi.fn().mockReturnValue({}),
  }
})

vi.mock('../../stores/environmentProfiles', async (importOriginal) => {
  const actual = (await importOriginal()) as Record<string, unknown>
  return { ...actual }
})

import { useEnvironmentProfilesStore } from '../../stores/environmentProfiles'
import { api } from '../../lib/api/client'
import RunnersProfilesTab from '../../views/runners/RunnersProfilesTab.vue'
import type { RunnersStatus } from '../../lib/runnersStatus'

const BUNDLED_PROFILE = {
  id: 'p-bundled',
  name: 'Bundled Runner (Docker)',
  description: 'seeded',
  provider_type: 'runner_docker',
  image_ref: 'modulo-runner:opencode',
  capabilities: [],
  status: 'active',
  created_at: '2026-01-01T00:00:00Z',
}

const E2B_PROFILE = {
  id: 'p-e2b',
  name: 'E2B',
  description: null,
  provider_type: 'e2b',
  image_ref: null,
  capabilities: [],
  status: 'active',
  created_at: '2026-01-01T00:00:00Z',
}

function makeStatus(profileOverrides: Record<string, unknown> = {}): RunnersStatus {
  return {
    aggregate_state: 'healthy',
    probe_interval_seconds: 60,
    staleness_threshold_seconds: 120,
    machines: [
      {
        machine_id: 'machine-1',
        state: 'healthy',
        engine_reachable: true,
        images_present: true,
        probed_at: new Date().toISOString(),
        age_seconds: 20,
        engine_info: { cpu_count: 8, mem_total_mb: 16384 },
        image_checks: {},
        probe_error: null,
      },
    ],
    profiles: [
      {
        id: 'p-bundled',
        name: 'Bundled Runner (Docker)',
        description: 'seeded',
        provider_type: 'runner_docker',
        image_ref: 'modulo-runner:opencode',
        config_json: { memory_mb: 1024, cpu_limit: 1 },
        network_policy: 'outbound',
        persistence_policy: 'ephemeral',
        status: 'active',
        health_state: 'healthy',
        available: true,
        placeholder_digest: false,
        drift: { is_seeded: true, drifted: false, drifted_fields: [] },
        active_workspaces: 3,
        ...profileOverrides,
      },
      {
        id: 'p-e2b',
        name: 'E2B',
        description: null,
        provider_type: 'e2b',
        image_ref: null,
        config_json: {},
        network_policy: 'outbound',
        persistence_policy: 'ephemeral',
        status: 'active',
        health_state: null,
        available: true,
        placeholder_digest: false,
        drift: { is_seeded: false, drifted: false, drifted_fields: [] },
        active_workspaces: 0,
      },
    ],
    concurrency: {
      sandbox_concurrency_limit: 4,
      is_default: true,
      preflight: {
        state: 'ok',
        detail: null,
        engine_cpu_count: 8,
        engine_mem_total_mb: 16384,
        needed_cpu: 4,
        needed_mem_mb: 4096,
      },
    },
  }
}

async function mountWithStore(profiles: unknown[]) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useEnvironmentProfilesStore()
  store.$patch({ profiles: profiles as never })
  const wrapper = mount(RunnersProfilesTab, {
    props: { status: makeStatus(), reloadStatus },
  })
  await nextTick()
  await flushPromises()
  await nextTick()
  return wrapper
}

describe('RunnersProfilesTab', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders profile rows with tier and health badges', async () => {
    const wrapper = await mountWithStore([BUNDLED_PROFILE, E2B_PROFILE])
    const badges = wrapper.findAll('[data-testid="envprofile-list-tier-badge"]')
    expect(badges).toHaveLength(2)
    expect(badges[0].text()).toBe('Bundled Runner (Docker)')
    expect(badges[1].text()).toBe('External Runner (E2B)')
    expect(wrapper.find(`[data-testid="runner-profile-health-p-bundled"]`).text()).toBe('healthy')
  })

  it('marks the unreachable bundled runner unavailable', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useEnvironmentProfilesStore()
    store.$patch({ profiles: [BUNDLED_PROFILE] as never })
    const status = makeStatus({ health_state: 'engine_unreachable', available: false })
    const wrapper = mount(RunnersProfilesTab, { props: { status, reloadStatus } })
    await nextTick()
    await flushPromises()

    expect(wrapper.find(`[data-testid="runner-profile-health-p-bundled"]`).text()).toBe('engine unreachable')
    expect(wrapper.find(`[data-testid="runner-profile-health-p-bundled"]`).classes()).toContain('text-destructive')
  })

  it('surfaces template drift per row with an Apply action', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useEnvironmentProfilesStore()
    store.$patch({ profiles: [BUNDLED_PROFILE] as never })
    const status = makeStatus({
      image_ref: 'modulo-runner:old',
      drift: { is_seeded: true, drifted: true, drifted_fields: ['image_ref'] },
    })
    const wrapper = mount(RunnersProfilesTab, { props: { status, reloadStatus } })
    await nextTick()
    await flushPromises()

    const drift = wrapper.find('[data-testid="runner-profile-drift"]')
    expect(drift.text()).toContain('Shipped template updated')
    expect(wrapper.find('[data-testid="runners-profiles-apply"]').exists()).toBe(true)
  })

  it('applies the shipped template through the runners API and reloads', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useEnvironmentProfilesStore()
    store.$patch({ profiles: [BUNDLED_PROFILE] as never })
    const status = makeStatus({
      image_ref: 'modulo-runner:old',
      drift: { is_seeded: true, drifted: true, drifted_fields: ['image_ref'] },
    })
    const wrapper = mount(RunnersProfilesTab, { props: { status, reloadStatus } })
    await nextTick()
    await flushPromises()

    await wrapper.find('[data-testid="runners-profiles-apply"]').trigger('click')
    await flushPromises()

    expect(api.POST).toHaveBeenCalledWith('/api/v1/runners/profiles/{profile_id}/apply-template', {
      params: { path: { profile_id: 'p-bundled' } },
    })
    expect(reloadStatus).toHaveBeenCalled()
  })

  it('protects the seeded template row from deletion', async () => {
    const wrapper = await mountWithStore([BUNDLED_PROFILE])
    const deleteBtn = wrapper.find('[data-testid="envprofile-list-delete"]')
    expect(deleteBtn.attributes('disabled')).toBeDefined()
  })

  it('keeps non-template rows deletable', async () => {
    const wrapper = await mountWithStore([E2B_PROFILE])
    const deleteBtn = wrapper.find('[data-testid="envprofile-list-delete"]')
    expect(deleteBtn.attributes('disabled')).toBeUndefined()
  })

  it('renders the bundled detail with resolved config and remediation pointer', async () => {
    const wrapper = await mountWithStore([BUNDLED_PROFILE])
    const detail = wrapper.find('[data-testid="runner-profile-detail"]')
    expect(detail.text()).toContain('modulo-runner:opencode')
    expect(detail.text()).toContain('1 CPU / 1024 MiB per container')
    expect(detail.text()).toContain('bundled-runner-operator-guide.md')
  })

  it('surfaces the active workspace count on the bundled profile detail', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useEnvironmentProfilesStore()
    store.$patch({ profiles: [BUNDLED_PROFILE] as never })
    const wrapper = mount(RunnersProfilesTab, { props: { status: makeStatus({ active_workspaces: 3 }), reloadStatus } })
    await nextTick()
    await flushPromises()
    await nextTick()

    const dd = wrapper.find('[data-testid="runner-profile-active-workspaces"]')
    expect(dd.exists()).toBe(true)
    expect(dd.text()).toBe('3')
  })

  it('shows zero active workspaces when the count is absent from the status payload', async () => {
    const status = makeStatus()
    delete (status.profiles?.[0] as Record<string, unknown>)['active_workspaces']
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useEnvironmentProfilesStore()
    store.$patch({ profiles: [BUNDLED_PROFILE] as never })
    const wrapper = mount(RunnersProfilesTab, { props: { status, reloadStatus } })
    await nextTick()
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="runner-profile-active-workspaces"]').text()).toBe('0')
  })

  it('search filters rows by query', async () => {
    const wrapper = await mountWithStore([BUNDLED_PROFILE, E2B_PROFILE])
    const searchInput = wrapper.find('[data-testid="envprofile-list-search"]')
    await searchInput.setValue('e2b')
    await nextTick()
    expect(wrapper.findAll('[data-testid="envprofile-list-name"]')).toHaveLength(1)
    expect(wrapper.text()).toContain('E2B')

    await searchInput.setValue('zzz-no-match')
    await nextTick()
    expect(wrapper.findAll('[data-testid="envprofile-list-name"]')).toHaveLength(0)
    // The header (search box + new-profile button) stays rendered for the
    // non-matching query; the grid simply yields zero rows.
    expect(wrapper.find('[data-testid="envprofile-list-search"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="envprofile-list-new"]').exists()).toBe(true)
  })

  it('surfaces a load failure via the inline ErrorAlert', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useEnvironmentProfilesStore()
    store.fetchProfiles = vi.fn()
    store.$patch({ error: 'backend down', profiles: [] })
    const wrapper = mount(RunnersProfilesTab, { props: { status: makeStatus(), reloadStatus } })
    await nextTick()
    await flushPromises()
    await nextTick()

    const alert = wrapper.find('.border-destructive\\/50')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('backend down')
  })

  it('streams SSE test-connection events into the panel and dismisses', async () => {
    const encoder = new TextEncoder()
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('data: {"event":"provisioning","detail":"booting","timestamp":"t1"}\n\n'))
        controller.enqueue(encoder.encode('data: {"event":"provisioned","detail":"ready","timestamp":"t2"}\n\n'))
        controller.close()
      },
    })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, body: stream }))

    const wrapper = await mountWithStore([BUNDLED_PROFILE])
    await wrapper.find('[data-testid="envprofile-test"]').trigger('click')
    await flushPromises()
    await nextTick()
    await flushPromises()

    const panel = wrapper.find('[data-testid="envprofile-test-panel"]')
    expect(panel.exists()).toBe(true)
    expect(panel.text()).toContain('provisioning')
    expect(panel.text()).toContain('booting')
    expect(panel.text()).toContain('provisioned')
    expect(vi.mocked(fetch)).toHaveBeenCalledWith(
      '/api/v1/environment-profiles/p-bundled/test',
      expect.objectContaining({ method: 'POST' }),
    )

    await wrapper.find('[data-testid="envprofile-test-dismiss"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="envprofile-test-panel"]').exists()).toBe(false)
  })

  it('delete flow confirms, calls the store delete, and cancels without deleting', async () => {
    const wrapper = await mountWithStore([E2B_PROFILE])
    const store = useEnvironmentProfilesStore()
    const delSpy = vi.spyOn(store, 'deleteProfile').mockResolvedValue(undefined)

    await wrapper.find('[data-testid="envprofile-list-delete"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Delete "E2B"?')

    await wrapper.find('[data-testid="envprofile-list-delete-cancel"]').trigger('click')
    await nextTick()
    expect(delSpy).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('Delete "E2B"?')

    await wrapper.find('[data-testid="envprofile-list-delete"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="envprofile-list-delete-confirm"]').trigger('click')
    await flushPromises()
    expect(delSpy).toHaveBeenCalledWith('p-e2b')
  })

  it('a failed delete surfaces the error in the confirm panel', async () => {
    const wrapper = await mountWithStore([E2B_PROFILE])
    const store = useEnvironmentProfilesStore()
    vi.spyOn(store, 'deleteProfile').mockRejectedValue(new Error('delete forbidden'))

    await wrapper.find('[data-testid="envprofile-list-delete"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="envprofile-list-delete-confirm"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('delete forbidden')
  })
})
