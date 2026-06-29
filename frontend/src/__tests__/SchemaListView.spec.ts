import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

function flushPromises(): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, 0))
}

vi.mock('../lib/api/client', () => {
  const schemas = [
    {
      id: '1',
      organisation_id: 'org-1',
      name: 'Active Schema',
      description: 'An active schema',
      abstract_name: null,
      created_by: 'user-1',
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
      deprecated: false,
      deprecated_at: null,
    },
    {
      id: '2',
      organisation_id: 'org-1',
      name: 'Deprecated Schema',
      description: 'A deprecated schema',
      abstract_name: null,
      created_by: 'user-1',
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
      deprecated: true,
      deprecated_at: '2026-06-01T00:00:00Z',
    },
  ]
  return {
    api: {
      GET: vi.fn().mockResolvedValue({
        data: { items: schemas, total: 2, page: 1, page_size: 100 },
        error: undefined,
      }),
      PATCH: vi.fn().mockResolvedValue({
        data: { ...schemas[0], deprecated: true, deprecated_at: '2026-06-29T00:00:00Z' },
        error: undefined,
      }),
    },
    getAccessToken: vi.fn().mockReturnValue('mock-token'),
  }
})

import SchemaListView from '../views/SchemaListView.vue'

describe('SchemaListView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const wrapper = mount(SchemaListView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
        },
      },
    })
    await flushPromises()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Schemas')
  })

  it('displays active and deprecated badges', async () => {
    const wrapper = mount(SchemaListView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
        },
      },
    })
    await flushPromises()
    expect(wrapper.text()).toContain('Active Schema')
    expect(wrapper.text()).toContain('Deprecated Schema')
    expect(wrapper.text()).toContain('Active')
    expect(wrapper.text()).toContain('Deprecated')
  })

  it('shows deprecate button only for active schemas', async () => {
    const wrapper = mount(SchemaListView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
        },
      },
    })
    await flushPromises()
    const deprecateButtons = wrapper.findAll('[data-testid="schema-deprecate"]')
    expect(deprecateButtons.length).toBe(1)
  })

  it('opens, confirms, and cancels deprecation', async () => {
    const wrapper = mount(SchemaListView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
        },
      },
    })
    await flushPromises()

    const deprecateBtn = wrapper.find('[data-testid="schema-deprecate"]')
    await deprecateBtn.trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Deprecate "Active Schema"?')

    // Cancel deprecation
    let cancelBtn = wrapper.find('[data-testid="schema-deprecate-cancel"]')
    await cancelBtn.trigger('click')
    await nextTick()
    expect(wrapper.text()).not.toContain('Deprecate "Active Schema"?')
    expect(wrapper.text()).toContain('Active')

    // Re-open and confirm deprecation
    const deprecateBtn2 = wrapper.find('[data-testid="schema-deprecate"]')
    await deprecateBtn2.trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Deprecate "Active Schema"?')

    const confirmBtn = wrapper.find('[data-testid="schema-deprecate-confirm"]')
    await confirmBtn.trigger('click')
    await nextTick()
    expect(wrapper.text()).not.toContain('Deprecate "Active Schema"?')
    expect(wrapper.text()).toContain('Deprecated')
  })
})
