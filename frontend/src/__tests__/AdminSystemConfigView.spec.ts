import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'

const mockGet = vi.fn()

vi.mock('../lib/api/client', () => ({
  api: {
    GET: (...args: unknown[]) => mockGet(...args),
  },
}))

import AdminSystemConfigView from '../views/AdminSystemConfigView.vue'

const loadingStub = { template: '<div data-testid="loading-spinner">loading</div>' }
const errorAlertStub = {
  template: '<div data-testid="error-alert">{{ message }}</div>',
  props: ['message', 'onRetry'],
}
const emptyStateStub = {
  template: '<div data-testid="empty-state">{{ title }}</div>',
  props: ['title'],
}
const jsonViewerStub = {
  template: '<div data-testid="json-viewer">{{ data }}</div>',
  props: ['data', 'showToolbar', 'maxHeight'],
}

function mountView() {
  return mount(AdminSystemConfigView, {
    global: {
      stubs: {
        FeatureGate: { template: '<div data-testid="feature-gate"><slot /></div>' },
        LoadingSpinner: loadingStub,
        ErrorAlert: errorAlertStub,
        EmptyState: emptyStateStub,
        JsonViewer: jsonViewerStub,
        PageHeader: {
          template: '<div><h1>{{ title }}</h1><p>{{ subtitle }}</p></div>',
          props: ['title', 'subtitle'],
        },
      },
    },
  })
}

describe('AdminSystemConfigView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockGet.mockResolvedValue({ data: [], error: undefined })
  })

  it('renders the page header', async () => {
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('System Admin')
    expect(wrapper.text()).toContain('Deployment-wide system configuration')
  })

  it('shows loading skeleton rows inside table while fetching', async () => {
    mockGet.mockReturnValue(new Promise(() => {}))
    const wrapper = mountView()
    await nextTick()
    // The loading skeleton is rendered inside the table's tbody
    expect(wrapper.find('table').exists()).toBe(true)
    expect(wrapper.find('[data-testid="empty-state"]').exists()).toBe(false)
  })

  it('shows error alert on API failure', async () => {
    mockGet.mockResolvedValue({
      data: undefined,
      error: { status: 500, detail: 'Internal Server Error' },
    })
    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="error-alert"]').exists()).toBe(true)
    })
    expect(wrapper.find('[data-testid="error-alert"]').text()).toContain('Internal Server Error')
  })

  it('shows empty state when no config entries', async () => {
    mockGet.mockResolvedValue({ data: [], error: undefined })
    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="empty-state"]').exists()).toBe(true)
    })
  })

  it('renders config entries in a table', async () => {
    mockGet.mockResolvedValue({
      data: [
        { key: 'LOG_LEVEL', value: 'info', updated_at: '2026-01-15' },
        { key: 'MAX_WORKERS', value: 4, updated_at: '2026-01-16' },
      ],
      error: undefined,
    })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    expect(wrapper.find('table').exists()).toBe(true)
    expect(wrapper.text()).toContain('LOG_LEVEL')
    expect(wrapper.text()).toContain('MAX_WORKERS')
  })

  it('shows table headers', async () => {
    mockGet.mockResolvedValue({ data: [], error: undefined })
    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="empty-state"]').exists()).toBe(true)
    })
    // Empty state is shown instead of table when data is empty
    // With data, headers appear:
    mockGet.mockResolvedValueOnce({
      data: [{ key: 'K', value: 'v', updated_at: null }],
      error: undefined,
    })
    const wrapper2 = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper2.text()).toContain('Key')
    expect(wrapper2.text()).toContain('Value')
    expect(wrapper2.text()).toContain('Updated')
  })

  it('renders updated_at as dash when null', async () => {
    mockGet.mockResolvedValue({
      data: [
        { key: 'NO_DATE', value: 'test', updated_at: null },
      ],
      error: undefined,
    })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('—')
  })

  it('refresh button calls loadConfig', async () => {
    mockGet.mockResolvedValue({ data: [], error: undefined })
    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="empty-state"]').exists()).toBe(true)
    })

    mockGet.mockClear()
    mockGet.mockResolvedValue({
      data: [{ key: 'REFRESHED', value: 'ok', updated_at: null }],
      error: undefined,
    })

    await wrapper.find('[data-testid="admin-system-config-refresh"]').trigger('click')
    await flushPromises()

    expect(mockGet).toHaveBeenCalled()
  })

  it('refresh button is disabled while loading', async () => {
    mockGet.mockReturnValue(new Promise(() => {}))
    const wrapper = mountView()
    await nextTick()

    const refreshBtn = wrapper.find('[data-testid="admin-system-config-refresh"]')
    expect((refreshBtn.element as HTMLButtonElement).disabled).toBe(true)
  })

  it('renders JsonViewer with entry value', async () => {
    mockGet.mockResolvedValue({
      data: [
        { key: 'COMPLEX', value: { nested: true }, updated_at: null },
      ],
      error: undefined,
    })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    const viewers = wrapper.findAll('[data-testid="json-viewer"]')
    expect(viewers.length).toBeGreaterThanOrEqual(1)
  })

  it('renders entries with code elements for key names', async () => {
    mockGet.mockResolvedValue({
      data: [
        { key: 'API_KEY', value: 'secret', updated_at: '2026-01-15' },
      ],
      error: undefined,
    })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    expect(wrapper.find('code').exists()).toBe(true)
    expect(wrapper.find('code').text()).toBe('API_KEY')
  })

  it('shows empty state instead of table when data is empty', async () => {
    mockGet.mockResolvedValue({ data: [], error: undefined })
    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="empty-state"]').exists()).toBe(true)
    })
    expect(wrapper.find('table').exists()).toBe(false)
  })

  it('shows table with loading skeleton when loading', async () => {
    mockGet.mockReturnValue(new Promise(() => {}))
    const wrapper = mountView()
    await nextTick()
    // The table is rendered with skeleton rows, not hidden
    expect(wrapper.find('table').exists()).toBe(true)
    expect(wrapper.find('[data-testid="empty-state"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="error-alert"]').exists()).toBe(false)
  })

  it('FeatureGate wraps the entire view', async () => {
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="feature-gate"]').exists()).toBe(true)
  })

  it('renders multiple entries with distinct keys', async () => {
    mockGet.mockResolvedValue({
      data: [
        { key: 'KEY_A', value: 'a', updated_at: '2026-01-01' },
        { key: 'KEY_B', value: 'b', updated_at: '2026-01-02' },
        { key: 'KEY_C', value: 'c', updated_at: null },
      ],
      error: undefined,
    })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('KEY_A')
    expect(wrapper.text()).toContain('KEY_B')
    expect(wrapper.text()).toContain('KEY_C')
  })

  it('renders updated_at values when present', async () => {
    mockGet.mockResolvedValue({
      data: [
        { key: 'DATED', value: 'v', updated_at: '2026-03-10 14:00' },
      ],
      error: undefined,
    })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('2026-03-10 14:00')
  })
})
