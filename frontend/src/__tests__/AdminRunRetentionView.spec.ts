import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { usePlanStore } from '../stores/planStore'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((path: string) => {
      if (path === '/api/v1/admin/runs/retention') {
        return Promise.resolve({ data: { retention_days: 90 }, error: null })
      }
      if (path === '/api/v1/admin/runs/storage') {
        return Promise.resolve({
          data: {
            total_runs: 150,
            status_breakdown: { completed: 100, failed: 30, running: 20 },
            estimated_saved_bytes: 524288000,
          },
          error: null,
        })
      }
      if (path === '/api/v1/admin/feature-flags') {
        return Promise.resolve({
          data: {
            license: { tier: 'enterprise', has_license_key: true, is_valid: true },
            flags: [{ name: 'admin_run_retention', description: '', tier: 'enterprise', currently_active: true, depends_on: null }],
            would_activate: [],
          },
          error: null,
        })
      }
      return Promise.resolve({ data: null, error: null })
    }),
    PUT: vi.fn().mockResolvedValue({ data: null, error: null }),
    POST: vi.fn().mockResolvedValue({ data: { deleted_count: 42 }, error: null }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import AdminRunRetentionView from '../views/AdminRunRetentionView.vue'

describe('AdminRunRetentionView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const store = usePlanStore()
    store.$patch({ features: { admin_run_retention: true } })

    const wrapper = mount(AdminRunRetentionView, {
      global: { plugins: [createPinia()] },
    })

    await nextTick()
    await nextTick()
    await nextTick()

    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Run Retention')
  })

  it('displays the current retention period from API', async () => {
    const store = usePlanStore()
    store.$patch({ features: { admin_run_retention: true } })

    const wrapper = mount(AdminRunRetentionView, {
      global: { plugins: [createPinia()] },
    })

    await nextTick()
    await nextTick()
    await nextTick()

    const input = wrapper.find('[data-testid="admin-run-retention-days"]') as any
    expect(input.element.value).toBe('90')
  })

  it('shows storage info with total runs count', async () => {
    const store = usePlanStore()
    store.$patch({ features: { admin_run_retention: true } })

    const wrapper = mount(AdminRunRetentionView, {
      global: { plugins: [createPinia()] },
    })

    await nextTick()
    await nextTick()
    await nextTick()

    const totalRuns = wrapper.find('[data-testid="admin-run-retention-total-runs"]')
    expect(totalRuns.text()).toBe('150')
  })

  it('renders manual purge section with age input and button', async () => {
    const store = usePlanStore()
    store.$patch({ features: { admin_run_retention: true } })

    const wrapper = mount(AdminRunRetentionView, {
      global: { plugins: [createPinia()] },
    })

    await nextTick()
    await nextTick()
    await nextTick()

    const ageInput = wrapper.find('[data-testid="admin-run-retention-purge-age"]')
    expect(ageInput.exists()).toBe(true)

    const purgeButton = wrapper.find('[data-testid="admin-run-retention-purge-now"]')
    expect(purgeButton.exists()).toBe(true)
    expect(purgeButton.text()).toContain('Purge Now')
  })

  it('shows error when purge age is empty', async () => {
    const store = usePlanStore()
    store.$patch({ features: { admin_run_retention: true } })

    const wrapper = mount(AdminRunRetentionView, {
      global: { plugins: [createPinia()] },
    })

    await nextTick()
    await nextTick()
    await nextTick()

    const purgeButton = wrapper.find('[data-testid="admin-run-retention-purge-now"]')
    await purgeButton.trigger('click')
    await nextTick()

    expect(wrapper.text()).toContain('Please enter a valid number of days.')
  })

  it('calls save retention API on save button click', async () => {
    const store = usePlanStore()
    store.$patch({ features: { admin_run_retention: true } })

    const wrapper = mount(AdminRunRetentionView, {
      global: { plugins: [createPinia()] },
    })

    await nextTick()
    await nextTick()
    await nextTick()

    const saveButton = wrapper.find('[data-testid="admin-run-retention-save"]')
    await saveButton.trigger('click')
    await nextTick()

    const { api } = await import('../lib/api/client')
    expect(api.PUT).toHaveBeenCalledWith('/api/v1/admin/runs/retention', {
      body: { retention_days: 90 },
    })
  })

  it('shows estimated storage saved in human-readable format', async () => {
    const store = usePlanStore()
    store.$patch({ features: { admin_run_retention: true } })

    const wrapper = mount(AdminRunRetentionView, {
      global: { plugins: [createPinia()] },
    })

    await nextTick()
    await nextTick()
    await nextTick()

    const estimatedSaved = wrapper.find('[data-testid="admin-run-retention-estimated-saved"]')
    expect(estimatedSaved.text()).toBe('500.0 MB')
  })
})
