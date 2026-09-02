import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import type { Mock } from 'vitest'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn(),
    POST: vi.fn(),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
  getAuthHeaders: vi.fn().mockReturnValue({}),
}))

import Select from 'primevue/select'

import AdminRunRetentionView from '../views/AdminRunRetentionView.vue'
import { api } from '../lib/api/client'
import { RUN_STATUS } from '../constants/filters'

const mockPipelines = {
  items: [
    { id: 'pipeline-1', name: 'Alpha Pipeline' },
    { id: 'pipeline-2', name: 'Beta Pipeline' },
  ],
  total: 2,
  page: 1,
  page_size: 100,
  has_more: false,
}

const mockCandidates = {
  runs: [
    { id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeffffffff', created_at: '2026-08-01T00:00:00Z', status: 'complete', pipeline_id: 'pipeline-1', thread_id: 'thread-1', estimated_bytes: 26214400 },
    { id: '11111111-2222-3333-4444-555566667777', created_at: '2026-08-02T00:00:00Z', status: 'failed', pipeline_id: 'pipeline-1', thread_id: 'thread-2', estimated_bytes: 26214400 },
    { id: '99999999-8888-7777-6666-555544443333', created_at: '2026-08-03T00:00:00Z', status: 'running', pipeline_id: 'pipeline-2', thread_id: 'thread-3', estimated_bytes: 10485760 },
  ],
  total_count: 3,
  total_estimated_bytes: 62914560,
  terminal_total: 2,
  terminal_estimated_bytes: 52428800,
}

function setupDefaultMock() {
  ;(api.GET as Mock).mockImplementation(async (url: string) => {
    if (url === '/api/v1/pipelines') {
      return { data: mockPipelines, error: undefined }
    }
    if (url === '/api/v1/admin/run-retention/candidates') {
      return { data: mockCandidates, error: undefined }
    }
    return { data: null, error: undefined }
  })
  ;(api.POST as Mock).mockResolvedValue({
    data: { purged_runs: 2, purged_checkpoints: 6, freed_estimated_bytes: 52428800 },
    error: undefined,
  })
}

async function mountView() {
  const pinia = createPinia()
  setActivePinia(pinia)
  const wrapper = mount(AdminRunRetentionView, {
    global: {
      plugins: [pinia],
      stubs: { FeatureGate: { template: '<div><slot /></div>' } },
    },
  })
  for (let i = 0; i < 10; i++) {
    await flushPromises()
  }
  return wrapper
}

describe('AdminRunRetentionView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    setupDefaultMock()
  })

  it('renders without crashing', async () => {
    const wrapper = await mountView()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Run Retention')
  })

  it('shows candidate summary from the API', async () => {
    const wrapper = await mountView()
    expect(wrapper.find('[data-testid="admin-run-retention-total-runs"]').text()).toBe('3')
    expect(wrapper.find('[data-testid="admin-run-retention-total-bytes"]').text()).toBe('50.0 MB')
    expect(wrapper.find('[data-testid="admin-run-retention-terminal-runs"]').text()).toBe('2')
  })

  it('lists candidates with pipeline names resolved', async () => {
    const wrapper = await mountView()
    expect(wrapper.find('[data-testid="admin-run-retention-candidates-table"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Alpha Pipeline')
  })

  it('shows a warning that in-flight runs are never purged', async () => {
    const wrapper = await mountView()
    const warning = wrapper.find('[data-testid="admin-run-retention-warning"]')
    expect(warning.exists()).toBe(true)
    expect(warning.text().toLowerCase()).toContain('never')
  })

  it('calls purge endpoint with confirm:true after confirming the dialog', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="admin-run-retention-purge"]').trigger('click')
    await flushPromises()
    await flushPromises()

    const confirmBtn = document.body.querySelector('[data-testid="admin-run-retention-purge-confirm"]') as HTMLElement
    expect(confirmBtn).not.toBeNull()
    confirmBtn.click()
    for (let i = 0; i < 5; i++) await flushPromises()

    const postMock = api.POST as Mock
    const purgeCall = postMock.mock.calls.find((c: unknown[]) => c[0] === '/api/v1/admin/run-retention/purge')
    expect(purgeCall).toBeDefined()
    const body = (purgeCall![1] as { body: { confirm: boolean } }).body
    expect(body.confirm).toBe(true)

    const result = wrapper.find('[data-testid="admin-run-retention-purge-result"]')
    expect(result.text()).toContain('Purged 2 run(s)')
  })

  it('clears the stale purge-result banner when candidates are reloaded', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="admin-run-retention-purge"]').trigger('click')
    await flushPromises()
    await flushPromises()

    const confirmBtn = document.body.querySelector('[data-testid="admin-run-retention-purge-confirm"]') as HTMLElement
    confirmBtn.click()
    for (let i = 0; i < 5; i++) await flushPromises()

    expect(wrapper.find('[data-testid="admin-run-retention-purge-result"]').exists()).toBe(true)

    await wrapper.find('[data-testid="admin-run-retention-refresh"]').trigger('click')
    for (let i = 0; i < 5; i++) await flushPromises()

    expect(wrapper.find('[data-testid="admin-run-retention-purge-result"]').exists()).toBe(false)
  })

  it('disables the purge button when no terminal candidates match', async () => {
    const runningOnly = {
      runs: [
        { id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeffffffff', created_at: '2026-08-01T00:00:00Z', status: 'running', pipeline_id: 'pipeline-1', thread_id: 'thread-1', estimated_bytes: 26214400 },
      ],
      total_count: 1,
      total_estimated_bytes: 26214400,
      terminal_total: 0,
      terminal_estimated_bytes: 0,
    }
    ;(api.GET as Mock).mockImplementation(async (url: string) => {
      if (url === '/api/v1/pipelines') return { data: mockPipelines, error: undefined }
      if (url === '/api/v1/admin/run-retention/candidates') return { data: runningOnly, error: undefined }
      return { data: null, error: undefined }
    })

    const wrapper = await mountView()
    expect(wrapper.find('[data-testid="admin-run-retention-purge"]').attributes('disabled')).toBeDefined()
  })

  it('renders the shared empty state with guidance when no candidates match', async () => {
    const noRuns = {
      runs: [],
      total_count: 0,
      total_estimated_bytes: 0,
      terminal_total: 0,
      terminal_estimated_bytes: 0,
    }
    ;(api.GET as Mock).mockImplementation(async (url: string) => {
      if (url === '/api/v1/pipelines') return { data: mockPipelines, error: undefined }
      if (url === '/api/v1/admin/run-retention/candidates') return { data: noRuns, error: undefined }
      return { data: null, error: undefined }
    })

    const wrapper = await mountView()
    const empty = wrapper.find('[data-testid="admin-run-retention-empty"]')
    expect(empty.exists()).toBe(true)
    expect(wrapper.find('[data-testid="admin-run-retention-candidates-table"]').exists()).toBe(false)
    // The empty state must explain what to do next, not just state the absence.
    expect(empty.text()).toContain('No runs match the current filters.')
    expect(empty.text()).toContain('Adjust the date, pipeline, or status filters to find purgable runs.')
  })

  it('localises the status filter options instead of showing raw status values', async () => {
    const wrapper = await mountView()
    const statusSelect = wrapper
      .findAllComponents(Select)
      .find(s => s.find('[data-testid="admin-run-retention-status"]').exists())
    expect(statusSelect).toBeTruthy()

    const options = statusSelect!.props('options') as Array<{ value: string; label: string }>
    // Every RUN_STATUS value is offered, each with a human-readable label.
    expect(options).toHaveLength(Object.values(RUN_STATUS).length)
    expect(options).toEqual(
      expect.arrayContaining([
        { value: 'budget_exceeded', label: 'Budget Exceeded' },
        { value: 'awaiting_human', label: 'Awaiting Human' },
        { value: 'complete', label: 'Complete' },
      ]),
    )
    // No label may leak an unresolved i18n key or a raw snake_case value.
    for (const option of options) {
      expect(option.label).not.toContain('views.')
      expect(option.label).not.toContain('_')
    }
  })

  it('exports the filtered set via a streaming download', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      statusText: 'OK',
      blob: async () => new Blob(['{}']),
      json: async () => ({}),
    })
    vi.stubGlobal('fetch', fetchMock)
    if (typeof URL.createObjectURL !== 'function') {
      // jsdom does not implement object URLs; provide a spyable stub.
      ;(URL as unknown as { createObjectURL: unknown }).createObjectURL = vi.fn()
    }
    if (typeof URL.revokeObjectURL !== 'function') {
      ;(URL as unknown as { revokeObjectURL: unknown }).revokeObjectURL = vi.fn()
    }
    const createObjectURLSpy = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:mock')
    const revokeObjectURLSpy = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click')

    const wrapper = await mountView()
    await wrapper.find('[data-testid="admin-run-retention-export"]').trigger('click')
    await flushPromises()
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, opts] = fetchMock.mock.calls[0] as [string, { method: string; headers: Record<string, string>; body: string }]
    expect(url).toBe('/api/v1/admin/run-retention/export')
    expect(opts.method).toBe('POST')
    expect(opts.headers).toMatchObject({ 'Content-Type': 'application/json' })
    expect(JSON.parse(opts.body)).toEqual({
      date_from: null,
      date_to: null,
      pipeline_id: null,
      status: null,
    })
    expect(createObjectURLSpy).toHaveBeenCalled()
    expect(clickSpy).toHaveBeenCalled()
    expect(wrapper.find('[data-testid="admin-run-retention-export-result"]').exists()).toBe(true)

    vi.unstubAllGlobals()
    createObjectURLSpy.mockRestore()
    revokeObjectURLSpy.mockRestore()
    clickSpy.mockRestore()
  })

  it('purges the applied snapshot, not unapplied filter edits', async () => {
    const wrapper = await mountView()
    // Edit a filter WITHOUT clicking Apply, so the displayed/confirmed set
    // still reflects the last load (empty applied filters).
    await wrapper.find('[data-testid="admin-run-retention-date-from"]').setValue('2026-07-01T00:00')
    await flushPromises()
    await flushPromises()

    await wrapper.find('[data-testid="admin-run-retention-purge"]').trigger('click')
    await flushPromises()
    await flushPromises()

    const confirmBtn = document.body.querySelector('[data-testid="admin-run-retention-purge-confirm"]') as HTMLElement
    expect(confirmBtn).not.toBeNull()
    confirmBtn.click()
    for (let i = 0; i < 5; i++) await flushPromises()

    const postMock = api.POST as Mock
    const purgeCall = postMock.mock.calls.find((c: unknown[]) => c[0] === '/api/v1/admin/run-retention/purge')
    expect(purgeCall).toBeDefined()
    const body = (purgeCall![1] as { body: Record<string, unknown> }).body
    // The purge must target the applied snapshot (empty filters), never the
    // unapplied date_from edit — otherwise it could purge a different set than confirmed.
    expect(body.date_from).toBeNull()
  })

  it('surfaces an export error when the export endpoint fails', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      statusText: 'Internal Server Error',
      blob: async () => new Blob([]),
      json: async () => ({ detail: 'export boom' }),
    })
    vi.stubGlobal('fetch', fetchMock)
    if (typeof URL.createObjectURL !== 'function') {
      ;(URL as unknown as { createObjectURL: unknown }).createObjectURL = vi.fn()
    }
    if (typeof URL.revokeObjectURL !== 'function') {
      ;(URL as unknown as { revokeObjectURL: unknown }).revokeObjectURL = vi.fn()
    }
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:mock')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})

    const wrapper = await mountView()
    await wrapper.find('[data-testid="admin-run-retention-export"]').trigger('click')
    await flushPromises()
    await flushPromises()

    const error = wrapper.find('[data-testid="admin-run-retention-error"]')
    expect(error.exists()).toBe(true)
    expect(error.text()).toContain('export boom')

    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('uses the server-side terminal total (not the page-capped count) in the confirm dialog', async () => {
    // 500 candidates are returned (the client page cap), but 640 terminal runs
    // actually match server-side — the purge is unbounded, so the confirm must
    // reflect 640, never the truncated 500 shown in the table.
    const manyRuns = Array.from({ length: 500 }, (_, i) => ({
      id: `run-${i.toString().padStart(4, '0')}-aaaaaaaaaaaaaaaa`,
      created_at: '2026-08-01T00:00:00Z',
      status: 'complete',
      pipeline_id: 'pipeline-1',
      thread_id: `thread-${i}`,
      estimated_bytes: 26214400,
    }))
    const many = {
      runs: manyRuns,
      total_count: 640,
      total_estimated_bytes: 640 * 26214400,
      terminal_total: 640,
      terminal_estimated_bytes: 640 * 26214400,
    }
    ;(api.GET as Mock).mockImplementation(async (url: string) => {
      if (url === '/api/v1/pipelines') return { data: mockPipelines, error: undefined }
      if (url === '/api/v1/admin/run-retention/candidates') return { data: many, error: undefined }
      return { data: null, error: undefined }
    })

    const wrapper = await mountView()
    // The "Terminal (purge-able)" card shows the uncapped server total (640),
    // not the 500-length page the table is rendering.
    expect(wrapper.find('[data-testid="admin-run-retention-terminal-runs"]').text()).toBe('640')

    await wrapper.find('[data-testid="admin-run-retention-purge"]').trigger('click')
    await flushPromises()
    await flushPromises()

    const dialog = document.body.querySelector('[data-testid="admin-run-retention-confirm-dialog"]') as HTMLElement
    expect(dialog).not.toBeNull()
    // Confirm dialog must state 640 (all matching terminal runs), not 500.
    expect(dialog.textContent).toContain('640')
    expect(dialog.textContent).not.toContain('500')
  })
})
