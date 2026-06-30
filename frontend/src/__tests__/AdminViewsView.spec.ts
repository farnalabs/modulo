import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

const mockFetch = vi.fn()
global.fetch = mockFetch

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

describe('AdminViewsView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(sampleViews),
    })
  })

  it('renders without crashing', async () => {
    const wrapper = mount(AdminViewsView, {
      global: {
        stubs: { LoadingSpinner: true, ErrorAlert: true },
      },
    })
    await flush()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Saved Views')
  })

  it('displays views in a table after loading', async () => {
    const wrapper = mount(AdminViewsView, {
      global: {
        stubs: { LoadingSpinner: true, ErrorAlert: true },
      },
    })
    await flush()
    expect(wrapper.text()).toContain('Active Runs')
    expect(wrapper.text()).toContain('Kanban Board')
    expect(wrapper.text()).toContain('alice@test.com')
    expect(wrapper.text()).toContain('bob@test.com')
  })

  it('shows empty state when no views exist', async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ items: [] }),
    })
    const wrapper = mount(AdminViewsView, {
      global: {
        stubs: { LoadingSpinner: true, ErrorAlert: true },
      },
    })
    await flush()
    expect(wrapper.text()).toContain('No saved views yet')
  })

  it('opens create form on button click', async () => {
    const wrapper = mount(AdminViewsView, {
      global: {
        stubs: { LoadingSpinner: true, ErrorAlert: true },
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
        stubs: { LoadingSpinner: true, ErrorAlert: true },
      },
    })
    await flush()
    const deleteBtns = wrapper.findAll('[data-testid="admin-views-delete"]')
    expect(deleteBtns.length).toBe(2)
    await deleteBtns[0].trigger('click')
    expect(wrapper.text()).toContain('Delete "Active Runs"?')
  })

  it('sends POST request on duplicate button click', async () => {
    mockFetch
      .mockReset()
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve(sampleViews),
      })
    const wrapper = mount(AdminViewsView, {
      global: {
        stubs: { LoadingSpinner: true, ErrorAlert: true },
      },
    })
    await flush()

    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({}),
    }).mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve(sampleViews),
    })

    const dupBtns = wrapper.findAll('[data-testid="admin-views-duplicate"]')
    expect(dupBtns.length).toBe(2)
    await dupBtns[0].trigger('click')
    await flush()

    const postCall = mockFetch.mock.calls.find(
      c => Array.isArray(c) && c[0] === '/api/v1/views' && c[1]?.method === 'POST'
    )
    expect(postCall).toBeDefined()
    if (postCall) {
      const body = JSON.parse(postCall[1].body)
      expect(body.name).toBe('Active Runs (copy)')
    }
  })
})
