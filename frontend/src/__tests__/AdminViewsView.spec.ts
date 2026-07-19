import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

const mockFetch = vi.hoisted(() => vi.fn())

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn(),
    POST: vi.fn(),
    DELETE: vi.fn().mockResolvedValue({ data: null, error: null }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import AdminViewsView from '../views/AdminViewsView.vue'

const sampleViews = {
  items: [
    {
      id: 'v1',
      name: 'Active Runs',
      view_type: 'table',
      filters: { status: 'active' },
      columns: ['name', 'status', 'created_at'],
      sort_by: 'created_at',
      sort_order: 'desc',
      created_by: 'alice@test.com',
      created_at: '2025-01-15T10:00:00Z',
    },
    {
      id: 'v2',
      name: 'Kanban Board',
      view_type: 'kanban',
      filters: null,
      columns: ['title', 'assignee'],
      sort_by: 'priority',
      sort_order: 'asc',
      created_by: 'bob@test.com',
      created_at: '2025-02-20T14:30:00Z',
    },
  ],
}

async function flush() {
  await new Promise(resolve => setTimeout(resolve))
  await nextTick()
}

function mockFetchOk(data: unknown) {
  mockFetch.mockResolvedValue({
    ok: true,
    json: () => Promise.resolve(data),
  })
}

describe('AdminViewsView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.stubGlobal('fetch', mockFetch)
    mockFetchOk(sampleViews)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders without crashing', async () => {
    const wrapper = mount(AdminViewsView, {
      global: {
        stubs: { LoadingSpinner: true, ErrorAlert: true, Tooltip: { template: '<div><slot /></div>' }, TooltipTrigger: { template: '<div><slot /></div>' }, TooltipContent: { template: '<div><slot /></div>' } },
      },
    })
    await flush()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Saved Views')
  })

  it('displays views in a table after loading', async () => {
    const wrapper = mount(AdminViewsView, {
      global: {
        stubs: { LoadingSpinner: true, ErrorAlert: true, Tooltip: { template: '<div><slot /></div>' }, TooltipTrigger: { template: '<div><slot /></div>' }, TooltipContent: { template: '<div><slot /></div>' } },
      },
    })
    await flush()
    expect(wrapper.text()).toContain('Active Runs')
    expect(wrapper.text()).toContain('Kanban Board')
    expect(wrapper.text()).toContain('alice@test.com')
    expect(wrapper.text()).toContain('bob@test.com')
  })

  it('shows empty state when no views exist', async () => {
    mockFetchOk({ items: [] })
    const wrapper = mount(AdminViewsView, {
      global: {
        stubs: { LoadingSpinner: true, ErrorAlert: true, Tooltip: { template: '<div><slot /></div>' }, TooltipTrigger: { template: '<div><slot /></div>' }, TooltipContent: { template: '<div><slot /></div>' } },
      },
    })
    await flush()
    expect(wrapper.text()).toContain('Saved Views')
  })

  it('opens create form on button click', async () => {
    const wrapper = mount(AdminViewsView, {
      global: {
        stubs: { LoadingSpinner: true, ErrorAlert: true, Tooltip: { template: '<div><slot /></div>' }, TooltipTrigger: { template: '<div><slot /></div>' }, TooltipContent: { template: '<div><slot /></div>' } },
      },
    })
    await flush()
    await wrapper.find('[data-testid="admin-views-add"]').trigger('click')
    expect(wrapper.text()).toContain('New View')
    expect(wrapper.find('[data-testid="admin-views-name-input"]').exists()).toBe(true)
  })

  it('shows delete confirmation on delete button click', async () => {
    const wrapper = mount(AdminViewsView, {
      global: {
        stubs: { LoadingSpinner: true, ErrorAlert: true, Tooltip: { template: '<div><slot /></div>' }, TooltipTrigger: { template: '<div><slot /></div>' }, TooltipContent: { template: '<div><slot /></div>' } },
      },
    })
    await flush()
    const deleteBtns = wrapper.findAll('button').filter(button => button.text() === 'Delete')
    expect(deleteBtns.length).toBe(2)
    await deleteBtns[0].trigger('click')
    expect(wrapper.text()).toContain('Delete "Active Runs"?')
  })

  it('sends POST request on duplicate button click', async () => {
    const wrapper = mount(AdminViewsView, {
      global: {
        stubs: { LoadingSpinner: true, ErrorAlert: true, Tooltip: { template: '<div><slot /></div>' }, TooltipTrigger: { template: '<div><slot /></div>' }, TooltipContent: { template: '<div><slot /></div>' } },
      },
    })
    await flush()

    const dupBtns = wrapper.findAll('button').filter(button => button.text() === 'Duplicate')
    expect(dupBtns.length).toBe(2)
    await dupBtns[0].trigger('click')
    await flush()

    const fetchCalls = mockFetch.mock.calls
    const postCall = fetchCalls.find((c: any[]) => c[1]?.method === 'POST' || !c[1]?.method)
    expect(postCall).toBeDefined()
  })
})
