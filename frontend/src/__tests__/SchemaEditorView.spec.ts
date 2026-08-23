import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'

vi.mock('vue-router', () => ({
  useRoute: vi.fn(() => ({ params: {}, path: '/schemas/editor' })),
  useRouter: vi.fn(() => ({ push: vi.fn() })),
}))

const mockSchemas = [
  {
    id: 'schema-1',
    organisation_id: 'org-1',
    name: 'User Profile',
    description: 'User profile data schema',
    abstract_name: null,
    created_by: 'user-1',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-15T00:00:00Z',
    deprecated: false,
    deprecated_at: null,
  },
  {
    id: 'schema-2',
    organisation_id: 'org-1',
    name: 'Product Catalog',
    description: 'Product catalog schema',
    abstract_name: null,
    created_by: 'user-1',
    created_at: '2026-02-01T00:00:00Z',
    updated_at: '2026-02-10T00:00:00Z',
    deprecated: true,
    deprecated_at: '2026-06-01T00:00:00Z',
  },
]

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((_url: string) => {
      if (String(_url).includes('/versions')) {
        return Promise.resolve({
          data: { items: [], total: 0, page: 1, page_size: 1 },
          error: undefined,
        })
      }
      return Promise.resolve({
        data: { items: mockSchemas, total: 2, page: 1, page_size: 100 },
        error: undefined,
      })
    }),
    POST: vi.fn().mockImplementation((_url: string) => {
      return Promise.resolve({
        data: { id: 'schema-new', name: 'New Schema' },
        error: undefined,
      })
    }),
    PATCH: vi.fn().mockImplementation((_url: string) => {
      return Promise.resolve({
        data: { ...mockSchemas[0], name: 'User Profile' },
        error: undefined,
      })
    }),
    PUT: vi.fn(),
    DELETE: vi.fn(),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

vi.mock('../stores/planStore', () => ({
  usePlanStore: vi.fn(() => ({
    featureEnabled: vi.fn().mockReturnValue(true),
    currentTier: 'team',
    isTeam: true,
    fetchPlan: vi.fn(),
  })),
}))

import SchemaEditorView from '../views/SchemaEditorView.vue'

describe('SchemaEditorView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const wrapper = mount(SchemaEditorView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          FeatureGate: {
            template: '<div><slot /></div>',
          },
        },
      },
    })
    await flushPromises()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Schemas')
  })

  it('loads and displays schema list', async () => {
    const wrapper = mount(SchemaEditorView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          FeatureGate: {
            template: '<div><slot /></div>',
          },
        },
      },
    })
    await flushPromises()
    expect(wrapper.text()).toContain('User Profile')
    expect(wrapper.text()).toContain('Product Catalog')
  })

  it('shows deprecated badge for deprecated schemas', async () => {
    const wrapper = mount(SchemaEditorView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          FeatureGate: {
            template: '<div><slot /></div>',
          },
        },
      },
    })
    await flushPromises()
    const items = wrapper.findAll('[data-testid="schema-editor-list-item"]')
    expect(items).toHaveLength(2)
    expect(items[1].text()).toContain('Deprecated')
  })

  it('filters schemas by search query', async () => {
    const wrapper = mount(SchemaEditorView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          FeatureGate: {
            template: '<div><slot /></div>',
          },
        },
      },
    })
    await flushPromises()

    const searchInput = wrapper.find('[data-testid="filter-bar-search"]')
    await searchInput.setValue('User')
    await nextTick()

    const items = wrapper.findAll('[data-testid="schema-editor-list-item"]')
    expect(items).toHaveLength(1)
    expect(items[0].text()).toContain('User Profile')
  })

  it('opens editor on new schema button', async () => {
    const wrapper = mount(SchemaEditorView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          FeatureGate: {
            template: '<div><slot /></div>',
          },
        },
      },
    })
    await flushPromises()

    const newBtn = wrapper.find('[data-testid="schema-editor-new"]')
    await newBtn.trigger('click')
    await nextTick()

    expect(wrapper.text()).toContain('New Schema')
    expect(wrapper.text()).toContain('Schema Details')
    expect(wrapper.text()).toContain('Fields')
  })

  it('can add and remove fields', async () => {
    const wrapper = mount(SchemaEditorView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          FeatureGate: {
            template: '<div><slot /></div>',
          },
        },
      },
    })
    await flushPromises()

    const newBtn = wrapper.find('[data-testid="schema-editor-new"]')
    await newBtn.trigger('click')
    await nextTick()

    const addBtn = wrapper.find('[data-testid="schema-editor-add-field"]')
    await addBtn.trigger('click')
    await nextTick()

    let fields = wrapper.findAll('[data-testid="schema-editor-field"]')
    expect(fields).toHaveLength(1)

    await addBtn.trigger('click')
    await nextTick()

    fields = wrapper.findAll('[data-testid="schema-editor-field"]')
    expect(fields).toHaveLength(2)

    const removeBtns = wrapper.findAll('[data-testid="schema-editor-field-remove"]')
    await removeBtns[0].trigger('click')
    await nextTick()

    fields = wrapper.findAll('[data-testid="schema-editor-field"]')
    expect(fields).toHaveLength(1)
  })

  it('renders JSON Schema preview', async () => {
    const wrapper = mount(SchemaEditorView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          FeatureGate: {
            template: '<div><slot /></div>',
          },
        },
      },
    })
    await flushPromises()

    const newBtn = wrapper.find('[data-testid="schema-editor-new"]')
    await newBtn.trigger('click')
    await nextTick()

    const addBtn = wrapper.find('[data-testid="schema-editor-add-field"]')
    await addBtn.trigger('click')
    await nextTick()

    const nameInput = wrapper.find('[data-testid="schema-editor-field-name"]')
    await nameInput.setValue('email')

    ;(wrapper.vm as any).fields[0].type = 'string'
    await nextTick()

    const preview = wrapper.find('[data-testid="schema-editor-json-preview"]')
    expect(preview.text()).toContain('email')
    expect(preview.text()).toContain('string')
  })

  it('shows validation errors on save with duplicate field names', async () => {
    const wrapper = mount(SchemaEditorView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          FeatureGate: {
            template: '<div><slot /></div>',
          },
        },
      },
    })
    await flushPromises()

    const newBtn = wrapper.find('[data-testid="schema-editor-new"]')
    await newBtn.trigger('click')
    await nextTick()

    const nameInput = wrapper.find('[data-testid="schema-editor-name"]')
    await nameInput.setValue('Test Schema')

    const addBtn = wrapper.find('[data-testid="schema-editor-add-field"]')
    await addBtn.trigger('click')
    await nextTick()
    await addBtn.trigger('click')
    await nextTick()

    const fieldNameInputs = wrapper.findAll('[data-testid="schema-editor-field-name"]')
    await fieldNameInputs[0].setValue('duplicate_field')
    await fieldNameInputs[1].setValue('duplicate_field')

    const saveBtn = wrapper.find('[data-testid="schema-editor-save"]')
    await saveBtn.trigger('click')
    await nextTick()

    expect(wrapper.text()).toContain('Duplicate field name')
  })

  it('can move fields up and down', async () => {
    const wrapper = mount(SchemaEditorView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          FeatureGate: {
            template: '<div><slot /></div>',
          },
        },
      },
    })
    await flushPromises()

    const newBtn = wrapper.find('[data-testid="schema-editor-new"]')
    await newBtn.trigger('click')
    await nextTick()

    const addBtn = wrapper.find('[data-testid="schema-editor-add-field"]')
    await addBtn.trigger('click')
    await nextTick()
    await addBtn.trigger('click')
    await nextTick()

    const nameInputs = wrapper.findAll('[data-testid="schema-editor-field-name"]')
    await nameInputs[0].setValue('field_a')
    await nameInputs[1].setValue('field_b')

    const moveUpBtns = wrapper.findAll('[data-testid="schema-editor-field-move-up"]')
    await moveUpBtns[1].trigger('click')
    await nextTick()

    const nameInputsAfter = wrapper.findAll('[data-testid="schema-editor-field-name"]')
    const value0 = (nameInputsAfter[0].element as HTMLInputElement).value
    const value1 = (nameInputsAfter[1].element as HTMLInputElement).value
    expect([value0, value1]).toContain('field_a')
    expect([value0, value1]).toContain('field_b')
  })

  it('renders version history when FeatureGate is enabled', async () => {
    const wrapper = mount(SchemaEditorView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          FeatureGate: {
            template: '<div><slot /></div>',
          },
        },
      },
    })
    await flushPromises()

    const items = wrapper.findAll('[data-testid="schema-editor-list-item"]')
    await items[0].trigger('click')
    await nextTick()

    expect(wrapper.text()).toContain('Version History')
  })

  it('shows empty state when no schema is selected', async () => {
    const wrapper = mount(SchemaEditorView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          FeatureGate: {
            template: '<div><slot /></div>',
          },
        },
      },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('Select a schema or create a new one')
  })
})
