import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick as vueNextTick } from 'vue'

async function nextTick() { await vueNextTick(); await flushPromises() }

const { mockGet, mockPost, mockPatch, mockDelete } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPost: vi.fn(),
  mockPatch: vi.fn(),
  mockDelete: vi.fn(),
}))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: mockGet,
    POST: mockPost,
    PUT: vi.fn(),
    PATCH: mockPatch,
    DELETE: mockDelete,
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import AdminModelBackendsView from '../views/AdminModelBackendsView.vue'

const backend = (id: string, over: Record<string, unknown> = {}) => ({
  id,
  name: id,
  display_name: `Display ${id}`,
  provider: 'anthropic',
  model_id: 'claude-sonnet',
  has_credentials: true,
  default_params: {},
  visibility: 'org',
  tier: 'native',
  ...over,
})

function mockBackendsGet(items: unknown[]) {
  mockGet.mockImplementation(async (url: string) => {
    if (url === '/api/v1/model-backends') return { data: { items }, error: undefined }
    return { data: undefined, error: { detail: `unrouted: ${url}` } }
  })
}

function mountView() {
  return mount(AdminModelBackendsView, {
    global: {
      stubs: {
        LoadingSpinner: true,
        FeatureGate: { template: '<div><slot /></div>' },
      },
    },
  })
}

async function openEdit(wrapper: ReturnType<typeof mountView>, backendId: string) {
  const row = wrapper.find(`[data-testid="model-backend-row-${backendId}"]`)
  // TableActions renders Edit first, then Delete.
  await row.findAll('button')[0].trigger('click')
  await nextTick()
}

beforeEach(() => {
  vi.clearAllMocks()
  mockBackendsGet([backend('mb-1')])
  mockPost.mockResolvedValue({ data: backend('mb-new'), error: undefined })
  mockPatch.mockResolvedValue({ data: backend('mb-1'), error: undefined })
  mockDelete.mockResolvedValue({ response: { status: 204, ok: true }, error: undefined })
})

describe('AdminModelBackendsView — list', () => {
  it('renders backends with provider, model id, credentials state and visibility', async () => {
    mockBackendsGet([
      backend('mb-1', { has_credentials: true }),
      backend('mb-2', { has_credentials: false, visibility: 'private' }),
    ])
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('Display mb-1')
    expect(wrapper.text()).toContain('anthropic')
    expect(wrapper.text()).toContain('claude-sonnet')
    expect(wrapper.text()).toContain('Configured')
    expect(wrapper.text()).toContain('Missing')
    expect(wrapper.text()).toContain('private')
  })

  it('shows the empty state when no backends are configured (fe-003)', async () => {
    mockBackendsGet([])
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('No model backends configured')
    expect(wrapper.find('[data-testid="model-backend-row-mb-1"]').exists()).toBe(false)
  })

  it('surfaces a load failure inline (fe-002 message path)', async () => {
    mockGet.mockResolvedValue({ data: undefined, error: { detail: 'backends down' } })
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('backends down')
  })

  it('segregates preview model backends into a disclosure section and hides in-dev backends', async () => {
    mockBackendsGet([
      backend('native-1', { name: 'native-1', display_name: 'Native Backend' }),
      backend('preview-1', { name: 'preview-1', display_name: 'Preview Backend', tier: 'preview' }),
      backend('indev-1', { name: 'indev-1', display_name: 'InDev Backend', tier: 'in_dev' }),
    ])
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('Native Backend')
    expect(wrapper.text()).not.toContain('InDev Backend')
    expect(wrapper.text()).not.toContain('indev-1')

    const previewSection = wrapper.find('[data-testid="model-backends-preview-section"]')
    expect(previewSection.exists()).toBe(true)
    expect(previewSection.text()).toContain('preview-1')
  })
})

describe('AdminModelBackendsView — create', () => {
  async function openAddForm(wrapper: ReturnType<typeof mountView>) {
    await wrapper.find('[data-testid="admin-model-backends-add"]').trigger('click')
    await nextTick()
  }

  it('opens the add form and disables submit until the required fields are filled', async () => {
    const wrapper = mountView()
    await nextTick()
    await openAddForm(wrapper)

    expect(wrapper.text()).toContain('New Model Backend')
    const submit = wrapper.find('[data-testid="admin-model-backends-submit"]')
    expect(submit.attributes('disabled')).toBeDefined()

    await wrapper.find('[data-testid="admin-model-backends-name-input"]').setValue('my-backend')
    await wrapper.find('[data-testid="admin-model-backends-display-name-input"]').setValue('My Backend')
    await wrapper.find('[data-testid="admin-model-backends-model-id-input"]').setValue('claude')
    await wrapper.find('[data-testid="admin-model-backends-api-key-input"]').setValue('sk-1')
    await nextTick()
    expect(wrapper.find('[data-testid="admin-model-backends-submit"]').attributes('disabled')).toBeUndefined()
  })

  it('creates via POST with the trimmed form values and a native tier', async () => {
    const wrapper = mountView()
    await nextTick()
    await openAddForm(wrapper)

    await wrapper.find('[data-testid="admin-model-backends-name-input"]').setValue('  my-backend  ')
    await wrapper.find('[data-testid="admin-model-backends-display-name-input"]').setValue('My Backend')
    await wrapper.find('[data-testid="admin-model-backends-model-id-input"]').setValue(' claude ')
    await wrapper.find('[data-testid="admin-model-backends-api-key-input"]').setValue('sk-1')
    await wrapper.find('[data-testid="admin-model-backends-params-input"]').setValue('{"temperature": 0.5}')
    await nextTick()

    await wrapper.find('form').trigger('submit')
    await nextTick()
    await nextTick()

    expect(mockPost).toHaveBeenCalledTimes(1)
    const [url, opts] = mockPost.mock.calls[0]
    expect(url).toBe('/api/v1/model-backends')
    expect(opts.body).toMatchObject({
      name: 'my-backend',
      display_name: 'My Backend',
      provider: 'anthropic',
      model_id: 'claude',
      api_key: 'sk-1',
      default_params: { temperature: 0.5 },
      visibility: 'org',
      tier: 'native',
    })
    // Form closes and the list reloads.
    expect(wrapper.find('[data-testid="admin-model-backends-name-input"]').exists()).toBe(false)
    const listCalls = mockGet.mock.calls.filter((c: unknown[]) => c[0] === '/api/v1/model-backends')
    expect(listCalls.length).toBeGreaterThanOrEqual(2)
  })

  it('reveals the base url field for variable-base providers and merges it into default_params', async () => {
    const wrapper = mountView()
    await nextTick()
    await openAddForm(wrapper)

    // anthropic does not show a base url field.
    expect(wrapper.find('[data-testid="admin-model-backends-base-url-input"]').exists()).toBe(false)

    const vm = wrapper.vm as unknown as { formData: { provider: string } }
    vm.formData.provider = 'ollama'
    await nextTick()
    expect(wrapper.find('[data-testid="admin-model-backends-base-url-input"]').exists()).toBe(true)

    await wrapper.find('[data-testid="admin-model-backends-name-input"]').setValue('ollama-1')
    await wrapper.find('[data-testid="admin-model-backends-display-name-input"]').setValue('Ollama')
    await wrapper.find('[data-testid="admin-model-backends-model-id-input"]').setValue('llama3')
    await wrapper.find('[data-testid="admin-model-backends-api-key-input"]').setValue('x')
    await wrapper.find('[data-testid="admin-model-backends-base-url-input"]').setValue('http://localhost:11434')
    await nextTick()
    await wrapper.find('form').trigger('submit')
    await nextTick()
    await nextTick()

    expect(mockPost.mock.calls[0][1].body.default_params).toEqual({ base_url: 'http://localhost:11434' })
  })

  it('ignores unparseable default params JSON with a console warning', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const wrapper = mountView()
    await nextTick()
    await openAddForm(wrapper)

    await wrapper.find('[data-testid="admin-model-backends-name-input"]').setValue('bad-json')
    await wrapper.find('[data-testid="admin-model-backends-display-name-input"]').setValue('Bad JSON')
    await wrapper.find('[data-testid="admin-model-backends-model-id-input"]').setValue('m')
    await wrapper.find('[data-testid="admin-model-backends-api-key-input"]').setValue('k')
    await wrapper.find('[data-testid="admin-model-backends-params-input"]').setValue('not-json{')
    await nextTick()
    await wrapper.find('form').trigger('submit')
    await nextTick()
    await nextTick()

    expect(warnSpy).toHaveBeenCalled()
    expect(mockPost.mock.calls[0][1].body.default_params).toEqual({})
    warnSpy.mockRestore()
  })

  it('shows a create failure in the form and keeps the form open', async () => {
    mockPost.mockResolvedValue({ data: undefined, error: { detail: 'duplicate name' } })
    const wrapper = mountView()
    await nextTick()
    await openAddForm(wrapper)

    await wrapper.find('[data-testid="admin-model-backends-name-input"]').setValue('dup')
    await wrapper.find('[data-testid="admin-model-backends-display-name-input"]').setValue('Dup')
    await wrapper.find('[data-testid="admin-model-backends-model-id-input"]').setValue('m')
    await wrapper.find('[data-testid="admin-model-backends-api-key-input"]').setValue('k')
    await nextTick()
    await wrapper.find('form').trigger('submit')
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('duplicate name')
    expect(wrapper.find('[data-testid="admin-model-backends-name-input"]').exists()).toBe(true)
  })

  it('cancel closes the add form', async () => {
    const wrapper = mountView()
    await nextTick()
    await openAddForm(wrapper)
    await wrapper.find('[data-testid="admin-model-backends-cancel"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="admin-model-backends-name-input"]').exists()).toBe(false)
  })
})

describe('AdminModelBackendsView — edit', () => {
  it('pre-fills the edit form and saves via PATCH without re-sending a blank api key', async () => {
    mockBackendsGet([backend('mb-1', { default_params: { temperature: 0.7, base_url: 'https://x' } })])
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await openEdit(wrapper, 'mb-1')

    expect((wrapper.find('[data-testid="admin-model-backends-edit-name"]').element as HTMLInputElement).value).toBe('mb-1')
    expect((wrapper.find('[data-testid="admin-model-backends-edit-display-name"]').element as HTMLInputElement).value).toBe('Display mb-1')
    expect((wrapper.find('[data-testid="admin-model-backends-edit-model-id"]').element as HTMLInputElement).value).toBe('claude-sonnet')
    expect((wrapper.find('[data-testid="admin-model-backends-edit-params"]').element as HTMLTextAreaElement).value).toContain('"temperature": 0.7')
    expect(wrapper.text()).toContain('Edit Model Backend')

    await wrapper.find('[data-testid="admin-model-backends-edit-name"]').setValue('mb-1-renamed')
    await wrapper.find('form').trigger('submit')
    await nextTick()
    await nextTick()

    expect(mockPatch).toHaveBeenCalledTimes(1)
    const [url, opts] = mockPatch.mock.calls[0]
    expect(url).toBe('/api/v1/model-backends/{backend_id}')
    expect(opts.params.path.backend_id).toBe('mb-1')
    expect(opts.body.name).toBe('mb-1-renamed')
    expect(opts.body.default_params).toEqual({ temperature: 0.7, base_url: 'https://x' })
    expect(opts.body.api_key).toBeUndefined()
    // Edit form closes.
    expect(wrapper.find('[data-testid="admin-model-backends-edit-name"]').exists()).toBe(false)
  })

  it('includes the api key in the PATCH body only when re-entered', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await openEdit(wrapper, 'mb-1')

    await wrapper.find('[data-testid="admin-model-backends-edit-api-key"]').setValue('sk-rotated')
    await wrapper.find('form').trigger('submit')
    await nextTick()
    await nextTick()
    expect(mockPatch.mock.calls[0][1].body.api_key).toBe('sk-rotated')
  })

  it('shows an update failure in the edit form', async () => {
    mockPatch.mockResolvedValue({ data: undefined, error: { detail: 'update rejected' } })
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await openEdit(wrapper, 'mb-1')
    await wrapper.find('form').trigger('submit')
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('update rejected')
  })

  it('cancel closes the edit form', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await openEdit(wrapper, 'mb-1')
    await wrapper.find('[data-testid="admin-model-backends-edit-cancel"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="admin-model-backends-edit-name"]').exists()).toBe(false)
  })
})

describe('AdminModelBackendsView — delete', () => {
  async function openDeleteConfirm(wrapper: ReturnType<typeof mountView>) {
    const row = wrapper.find('[data-testid="model-backend-row-mb-1"]')
    // TableActions: Edit first, Delete second.
    await row.findAll('button')[1].trigger('click')
    await nextTick()
  }

  it('asks for confirmation, DELETEs and reloads the list', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await openDeleteConfirm(wrapper)

    expect(wrapper.text()).toContain('Delete "Display mb-1"?')
    expect(wrapper.text()).toContain('This action cannot be undone.')

    await wrapper.find('[data-testid="admin-model-backends-delete-confirm"]').trigger('click')
    await nextTick()
    await nextTick()

    expect(mockDelete).toHaveBeenCalledTimes(1)
    const [url, opts] = mockDelete.mock.calls[0]
    expect(url).toBe('/api/v1/model-backends/{backend_id}')
    expect(opts.params.path.backend_id).toBe('mb-1')
    const listCalls = mockGet.mock.calls.filter((c: unknown[]) => c[0] === '/api/v1/model-backends')
    expect(listCalls.length).toBeGreaterThanOrEqual(2)
  })

  it('surfaces a DELETE error envelope', async () => {
    mockDelete.mockResolvedValue({ response: { status: 409 }, error: { detail: 'backend in use' } })
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await openDeleteConfirm(wrapper)
    await wrapper.find('[data-testid="admin-model-backends-delete-confirm"]').trigger('click')
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('backend in use')
  })

  it('BUG CHARACTERISATION: cancel does not close the delete confirmation (see report)', async () => {
    // The delete-confirm Cancel button calls closeForm(), which resets
    // formMode but NOT deleteConfirmBackendId — so the confirmation block
    // stays visible and Delete remains clickable. Reported; a fix that also
    // clears deleteConfirmBackendId flips this test.
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await openDeleteConfirm(wrapper)
    // Only the delete block's cancel button exists (the add form is closed).
    await wrapper.find('[data-testid="admin-model-backends-cancel"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Delete "Display mb-1"?')
    expect(mockDelete).not.toHaveBeenCalled()
  })
})

describe('AdminModelBackendsView — pipeline references', () => {
  const refItem = (pipelineId: string, over: Record<string, unknown> = {}) => ({
    pipeline_id: pipelineId,
    pipeline_name: `Pipeline ${pipelineId}`,
    agent_id: null,
    agent_name: null,
    reference_type: 'direct_node',
    ...over,
  })

  async function expandRefs(wrapper: ReturnType<typeof mountView>, backendId = 'mb-1') {
    await wrapper.find(`[data-testid="admin-model-backends-refs-expand-${backendId}"]`).trigger('toggle')
    await nextTick()
    await nextTick()
  }

  it('loads and renders pipeline references when the details row is expanded', async () => {
    mockGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/model-backends') return { data: { items: [backend('mb-1')] }, error: undefined }
      if (url === '/api/v1/model-backends/{backend_id}/pipeline-references') {
        return { data: { items: [refItem('p-1'), refItem('p-2', { agent_id: 'a-1', agent_name: 'Agent A', reference_type: 'via_agent' })], total: 2, page: 1, page_size: 20 }, error: undefined }
      }
      return { data: undefined, error: { detail: 'unrouted' } }
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await expandRefs(wrapper)

    expect(mockGet).toHaveBeenCalledWith(
      '/api/v1/model-backends/{backend_id}/pipeline-references',
      expect.objectContaining({ params: { path: { backend_id: 'mb-1' }, query: { page: 1, page_size: 20 } } }),
    )
    expect(wrapper.text()).toContain('Pipeline p-1')
    expect(wrapper.text()).toContain('Direct')
    expect(wrapper.text()).toContain('Agent A')
    expect(wrapper.text()).toContain('Via Agent')
  })

  it('shows the empty message when the backend has no references', async () => {
    mockGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/model-backends') return { data: { items: [backend('mb-1')] }, error: undefined }
      if (url === '/api/v1/model-backends/{backend_id}/pipeline-references') {
        return { data: { items: [], total: 0, page: 1, page_size: 20 }, error: undefined }
      }
      return { data: undefined, error: { detail: 'unrouted' } }
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await expandRefs(wrapper)
    expect(wrapper.text()).toContain('No pipeline graph references found')
  })

  it('shows a refs load failure with a working retry button', async () => {
    let refsCalls = 0
    mockGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/model-backends') return { data: { items: [backend('mb-1')] }, error: undefined }
      if (url === '/api/v1/model-backends/{backend_id}/pipeline-references') {
        refsCalls++
        if (refsCalls === 1) return { data: undefined, error: { detail: 'refs down' } }
        return { data: { items: [refItem('p-1')], total: 1, page: 1, page_size: 20 }, error: undefined }
      }
      return { data: undefined, error: { detail: 'unrouted' } }
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await expandRefs(wrapper)
    expect(wrapper.text()).toContain('refs down')

    // Retry fetches page 1 again and recovers.
    await wrapper.find('[data-testid="admin-model-backends-refs-retry-mb-1"]').trigger('click')
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('Pipeline p-1')
  })

  it('paginates references when there is more than one page', async () => {
    mockGet.mockImplementation(async (url: string, opts?: { params?: { query?: { page?: number } } }) => {
      if (url === '/api/v1/model-backends') return { data: { items: [backend('mb-1')] }, error: undefined }
      if (url === '/api/v1/model-backends/{backend_id}/pipeline-references') {
        const page = opts?.params?.query?.page ?? 1
        return { data: { items: [refItem(`p-${page}`)], total: 45, page, page_size: 20 }, error: undefined }
      }
      return { data: undefined, error: { detail: 'unrouted' } }
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await expandRefs(wrapper)

    expect(wrapper.text()).toContain('Page 1 of 3')
    expect(wrapper.find('[data-testid="admin-model-backends-refs-prev-mb-1"]').exists()).toBe(false)

    await wrapper.find('[data-testid="admin-model-backends-refs-next-mb-1"]').trigger('click')
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('Pipeline p-2')
    expect(wrapper.text()).toContain('Page 2 of 3')

    await wrapper.find('[data-testid="admin-model-backends-refs-prev-mb-1"]').trigger('click')
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('Page 1 of 3')
  })

  it('collapses the refs panel on the second toggle and clears the loaded refs', async () => {
    mockGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/model-backends') return { data: { items: [backend('mb-1')] }, error: undefined }
      if (url === '/api/v1/model-backends/{backend_id}/pipeline-references') {
        return { data: { items: [refItem('p-1')], total: 1, page: 1, page_size: 20 }, error: undefined }
      }
      return { data: undefined, error: { detail: 'unrouted' } }
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()
    await expandRefs(wrapper)
    expect(wrapper.text()).toContain('Pipeline p-1')

    await wrapper.find('[data-testid="admin-model-backends-refs-expand-mb-1"]').trigger('toggle')
    await nextTick()
    expect(wrapper.text()).not.toContain('Pipeline p-1')
  })
})
