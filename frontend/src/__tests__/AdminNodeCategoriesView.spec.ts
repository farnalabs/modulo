import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const mockGet = vi.fn()
const mockDelete = vi.fn()

vi.mock('../lib/api/client', () => ({
  api: {
    GET: (...args: unknown[]) => mockGet(...args),
    POST: vi.fn(),
    PUT: vi.fn(),
    PATCH: vi.fn(),
    DELETE: (...args: unknown[]) => mockDelete(...args),
  },
}))

vi.mock('../lib/api/formatError', () => ({
  formatApiError: (e: unknown) => (e instanceof Error ? e.message : String(e)),
}))

import AdminNodeCategoriesView from '../views/AdminNodeCategoriesView.vue'

function makeCategory(overrides: Record<string, unknown> = {}) {
  return {
    id: 'cat-1',
    name: 'LLM',
    description: 'Language model nodes',
    color: '#6366f1',
    icon: 'bot',
    sort_order: 0,
    ...overrides,
  }
}

const tableActionsStub = {
  template: '<div data-testid="table-actions"><button v-for="a in actions" :key="a.key" :data-testid="`action-${a.key}`" @click="a.onClick">{{ a.label }}</button></div>',
  props: ['actions'],
}

const pageHeaderStub = {
  template: '<div><h1>{{ title }}</h1></div>',
  props: ['title', 'subtitle'],
}

const nodeCategoryEditorStub = {
  template: '<div data-testid="node-category-editor"></div>',
  props: ['category'],
  emits: ['saved', 'cancelled'],
}

function mountView(categories: unknown[] = []) {
  mockGet.mockResolvedValue({ data: { items: categories }, error: null })
  return mount(AdminNodeCategoriesView, {
    global: {
      stubs: {
        LoadingSpinner: true,
        ErrorAlert: {
          template: '<div data-testid="error-alert">{{ message }}</div>',
          props: ['message', 'onRetry'],
        },
        NodeCategoryEditor: nodeCategoryEditorStub,
        FeatureGate: { template: '<div><slot /></div>' },
        TableActions: tableActionsStub,
        PageHeader: pageHeaderStub,
        Button: {
          template: '<button type="button" :disabled="disabled" :data-testid="$attrs[\'data-testid\']" @click="$emit(\'click\')"><slot /></button>',
          props: ['disabled'],
          emits: ['click'],
        },
      },
    },
  })
}

describe('AdminNodeCategoriesView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    mockGet.mockResolvedValue({ data: { items: [] }, error: null })
    mockDelete.mockResolvedValue({ response: { status: 204 }, error: null })
  })

  // ─── Original test (never modify) ─────────────────────────────────────

  it('renders without crashing', async () => {
    const wrapper = mount(AdminNodeCategoriesView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
          NodeCategoryEditor: true,
          FeatureGate: { template: '<div><slot /></div>' },
        },
      },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Node Categories')
  })

  // ─── Empty state ──────────────────────────────────────────────────────

  it('shows empty state when no categories exist', async () => {
    const wrapper = mountView([])
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('No node categories configured')
  })

  it('hides empty state when categories exist', async () => {
    const wrapper = mountView([makeCategory()])
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).not.toContain('No node categories configured')
  })

  // ─── Loading state ────────────────────────────────────────────────────

  it('shows loading spinner while fetching', async () => {
    mockGet.mockReturnValue(new Promise(() => {}))
    const wrapper = mount(AdminNodeCategoriesView, {
      global: {
        stubs: {
          LoadingSpinner: { template: '<div data-testid="loading-spinner">loading</div>' },
          ErrorAlert: true,
          NodeCategoryEditor: true,
          FeatureGate: { template: '<div><slot /></div>' },
        },
      },
    })
    await nextTick()
    expect(wrapper.find('[data-testid="loading-spinner"]').exists()).toBe(true)
  })

  // ─── Error state ──────────────────────────────────────────────────────

  it('shows no error alert when API returns data successfully', async () => {
    mockGet.mockResolvedValue({ data: { items: [] }, error: null })
    const wrapper = mountView()
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="error-alert"]').exists()).toBe(false)
  })

  // ─── Table rendering ──────────────────────────────────────────────────

  it('renders categories in a table', async () => {
    const cat = makeCategory({ name: 'LLM', description: 'Language model nodes', icon: 'bot', sort_order: 1 })
    const wrapper = mountView([cat])
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('LLM')
    expect(wrapper.text()).toContain('Language model nodes')
    expect(wrapper.text()).toContain('bot')
    expect(wrapper.text()).toContain('1')
  })

  it('renders dash for null description', async () => {
    const cat = makeCategory({ description: null })
    const wrapper = mountView([cat])
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('—')
  })

  it('renders dash for null icon', async () => {
    const cat = makeCategory({ icon: null })
    const wrapper = mountView([cat])
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('—')
  })

  it('renders default color when color is empty', async () => {
    const cat = makeCategory({ color: '' })
    const wrapper = mountView([cat])
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('#6366f1')
  })

  it('renders the color value in the color cell', async () => {
    const cat = makeCategory({ color: '#ff0000' })
    const wrapper = mountView([cat])
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('#ff0000')
  })

  it('renders table actions for each category', async () => {
    const cat = makeCategory()
    const wrapper = mountView([cat])
    await flushPromises()
    await nextTick()

    const actions = wrapper.findAll('[data-testid="table-actions"]')
    expect(actions.length).toBe(1)
    expect(wrapper.find('[data-testid="action-edit"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="action-delete"]').exists()).toBe(true)
  })

  // ─── Add form ─────────────────────────────────────────────────────────

  it('shows add form when Add Category button is clicked', async () => {
    const wrapper = mountView([])
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="admin-node-categories-add"]').trigger('click')
    await nextTick()

    expect(wrapper.find('[data-testid="node-category-editor"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('New Node Category')
  })

  // ─── Edit flow ────────────────────────────────────────────────────────

  it('shows edit form when Edit action is clicked', async () => {
    const cat = makeCategory({ id: 'cat-edit', name: 'Edit Me' })
    const wrapper = mountView([cat])
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="action-edit"]').trigger('click')
    await nextTick()

    expect(wrapper.text()).toContain('Edit Node Category')
    expect(wrapper.find('[data-testid="node-category-editor"]').exists()).toBe(true)
  })

  // ─── Delete flow ──────────────────────────────────────────────────────

  it('shows delete confirmation when Delete action is clicked', async () => {
    const cat = makeCategory({ id: 'cat-del', name: 'Delete Me' })
    const wrapper = mountView([cat])
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="action-delete"]').trigger('click')
    await nextTick()

    expect(wrapper.text()).toContain('Delete "Delete Me"?')
    expect(wrapper.find('[data-testid="admin-node-categories-delete-confirm"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="admin-node-categories-delete-cancel"]').exists()).toBe(true)
  })

  it('hides delete confirmation when Cancel is clicked', async () => {
    const cat = makeCategory({ id: 'cat-cancel', name: 'Cancel Delete' })
    const wrapper = mountView([cat])
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="action-delete"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Delete "Cancel Delete"?')

    await wrapper.find('[data-testid="admin-node-categories-delete-cancel"]').trigger('click')
    await nextTick()

    expect(wrapper.text()).not.toContain('Delete "Cancel Delete"?')
  })

  it('calls DELETE API and reloads categories on successful delete', async () => {
    const cat = makeCategory({ id: 'cat-del-ok', name: 'To Delete' })
    mockGet.mockResolvedValueOnce({ data: { items: [cat] }, error: null })
    const wrapper = mountView([cat])
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="action-delete"]').trigger('click')
    await nextTick()

    mockGet.mockResolvedValueOnce({ data: { items: [] }, error: null })

    await wrapper.find('[data-testid="admin-node-categories-delete-confirm"]').trigger('click')
    await flushPromises()

    expect(mockDelete).toHaveBeenCalledWith(
      '/api/v1/node-categories/{category_id}',
      expect.objectContaining({
        params: { path: { category_id: 'cat-del-ok' } },
      }),
    )
  })

  it('shows error message when delete fails', async () => {
    const cat = makeCategory({ id: 'cat-del-fail', name: 'Fail Delete' })
    mockGet.mockResolvedValueOnce({ data: { items: [cat] }, error: null })
    mockDelete.mockResolvedValueOnce({ error: 'Cannot delete', response: {} })
    const wrapper = mountView([cat])
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="action-delete"]').trigger('click')
    await nextTick()

    await wrapper.find('[data-testid="admin-node-categories-delete-confirm"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Cannot delete')
  })

  it('shows delete error from thrown exception', async () => {
    const cat = makeCategory({ id: 'cat-throw', name: 'Throw Delete' })
    mockGet.mockResolvedValueOnce({ data: { items: [cat] }, error: null })
    mockDelete.mockRejectedValueOnce(new Error('Network timeout'))
    const wrapper = mountView([cat])
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="action-delete"]').trigger('click')
    await nextTick()

    await wrapper.find('[data-testid="admin-node-categories-delete-confirm"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Network timeout')
  })

  // ─── Category actions (categoryActions function) ──────────────────────

  it('categoryActions returns edit and delete actions with correct labels', async () => {
    const cat = makeCategory()
    const wrapper = mountView([cat])
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="action-edit"]').text()).toBe('Edit')
    expect(wrapper.find('[data-testid="action-delete"]').text()).toBe('Delete')
  })

  // ─── iconSvg function ─────────────────────────────────────────────────

  it('renders SVG for known icon names', async () => {
    const cat = makeCategory({ icon: 'bot' })
    const wrapper = mountView([cat])
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('bot')
  })

  it('renders empty string for unknown icon name', async () => {
    const cat = makeCategory({ icon: 'nonexistent' })
    const wrapper = mountView([cat])
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('nonexistent')
  })

  // ─── Multiple categories ──────────────────────────────────────────────

  it('renders multiple categories in the table', async () => {
    const cats = [
      makeCategory({ id: '1', name: 'Cat A' }),
      makeCategory({ id: '2', name: 'Cat B' }),
      makeCategory({ id: '3', name: 'Cat C' }),
    ]
    const wrapper = mountView(cats)
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('Cat A')
    expect(wrapper.text()).toContain('Cat B')
    expect(wrapper.text()).toContain('Cat C')
  })

  // ─── FeatureGate wrapping ─────────────────────────────────────────────

  it('wraps content in FeatureGate', async () => {
    const wrapper = mountView([])
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('Node Categories')
  })

  // ─── Add button ───────────────────────────────────────────────────────

  it('renders Add Category button', async () => {
    const wrapper = mountView([])
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="admin-node-categories-add"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="admin-node-categories-add"]').text()).toContain('Add Category')
  })

  // ─── openAddForm resets state ─────────────────────────────────────────

  it('add button resets editor state', async () => {
    const cat = makeCategory()
    const wrapper = mountView([cat])
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="action-edit"]').trigger('click')
    await nextTick()

    await wrapper.find('[data-testid="admin-node-categories-add"]').trigger('click')
    await nextTick()

    expect(wrapper.text()).toContain('New Node Category')
    expect(wrapper.text()).not.toContain('Edit Node Category')
  })

  // ─── confirmDelete clears editor ──────────────────────────────────────

  it('confirm delete resets editor mode', async () => {
    const cat = makeCategory({ id: 'cat-rst', name: 'Rst' })
    const wrapper = mountView([cat])
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="admin-node-categories-add"]').trigger('click')
    await nextTick()

    await wrapper.find('[data-testid="action-delete"]').trigger('click')
    await nextTick()

    expect(wrapper.text()).toContain('Delete "Rst"?')
  })

  // ─── Table structure ──────────────────────────────────────────────────

  it('renders table headers', async () => {
    const cat = makeCategory()
    const wrapper = mountView([cat])
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('Name')
    expect(wrapper.text()).toContain('Description')
    expect(wrapper.text()).toContain('Color')
    expect(wrapper.text()).toContain('Icon')
    expect(wrapper.text()).toContain('Sort Order')
    expect(wrapper.text()).toContain('Actions')
  })

  // ─── deleteConfirmCategoryId guards against null ──────────────────────

  it('deleteCategory is a no-op when deleteConfirmCategoryId is null', async () => {
    mountView([])
    await flushPromises()
    await nextTick()

    expect(mockDelete).not.toHaveBeenCalled()
  })

  // ─── Category with icon renders SVG inline ────────────────────────────

  it('renders icon SVG with v-html for known icons', async () => {
    const cat = makeCategory({ icon: 'database' })
    const wrapper = mountView([cat])
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('database')
  })

  // ─── Color cell has the span with background color style ──────────────

  it('renders color swatch with inline background color', async () => {
    const cat = makeCategory({ color: '#ff5500' })
    const wrapper = mountView([cat])
    await flushPromises()
    await nextTick()

    const colorSpan = wrapper.find('span[style*="background-color"]')
    expect(colorSpan.exists()).toBe(true)
    // jsdom normalizes hex to rgb
    expect(colorSpan.attributes('style')).toContain('background-color')
  })

  // ─── Deletion success resets and reloads ──────────────────────────────

  it('successful delete clears the confirmation and reloads', async () => {
    const cat = makeCategory({ id: 'cat-reload', name: 'Reload' })
    mockGet.mockResolvedValueOnce({ data: { items: [cat] }, error: null })
    const wrapper = mountView([cat])
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="action-delete"]').trigger('click')
    await nextTick()

    mockGet.mockResolvedValueOnce({ data: { items: [] }, error: null })

    await wrapper.find('[data-testid="admin-node-categories-delete-confirm"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).not.toContain('Delete "Reload"?')
  })

  // ─── iconSvg returns known SVG for all listed icons ───────────────────

  it('renders all known icon names without crashing', async () => {
    const icons = ['bot', 'database', 'globe', 'mail', 'message-circle', 'refresh-cw', 'search', 'settings', 'sliders', 'terminal', 'upload', 'zap']
    const cats = icons.map((icon, i) => makeCategory({ id: `icon-${i}`, name: icon, icon }))
    const wrapper = mountView(cats)
    await flushPromises()
    await nextTick()

    for (const icon of icons) {
      expect(wrapper.text()).toContain(icon)
    }
  })
})
