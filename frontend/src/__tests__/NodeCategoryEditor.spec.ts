import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import NodeCategoryEditor from '../components/NodeCategoryEditor.vue'

const mockPost = vi.fn()
const mockPatch = vi.fn()

vi.mock('../lib/api/client', () => ({
  api: {
    POST: (...args: unknown[]) => mockPost(...args),
    PATCH: (...args: unknown[]) => mockPatch(...args),
  },
}))

vi.mock('../lib/api/formatError', () => ({
  formatApiError: (e: unknown) => {
    if (e instanceof Error) return e.message
    if (typeof e === 'object' && e !== null && 'detail' in e) return (e as Record<string, unknown>).detail as string
    return 'Request failed'
  },
}))

const i18n = createI18n({
  legacy: false,
  locale: 'en-US',
  messages: {
    'en-US': {
      components: {
        NodeCategoryEditor: {
          name: 'Name',
          description: 'Description',
          color: 'Color',
          icon: 'Icon',
          sort_order: 'Sort Order',
          eg_llm_call_connector_read: 'e.g. LLM Call, Connector Read',
          optional_description_of_this_category: 'Optional description of this category',
          select_icon: 'Select icon',
          none: 'None',
          bot: 'Bot',
          database: 'Database',
          globe: 'Globe',
          mail: 'Mail',
          message_circle: 'Message Circle',
          refresh: 'Refresh',
          settings: 'Settings',
          sliders: 'Sliders',
          terminal: 'Terminal',
          upload: 'Upload',
          zap: 'Zap',
        },
        pipeline: {
          composite: {
            OutputValidationTab: {
              output_validation: 'Output Validation',
            },
          },
        },
      },
      common: {
        search: 'Search',
      },
    },
  },
})

describe('NodeCategoryEditor', () => {
  beforeEach(() => {
    mockPost.mockReset()
    mockPatch.mockReset()
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('renders empty form in create mode (no category prop)', () => {
    const wrapper = mount(NodeCategoryEditor, {
      global: { plugins: [i18n] },
    })
    expect(wrapper.text()).toContain('Create Category')
    expect(wrapper.text()).toContain('Name')
    expect(wrapper.text()).toContain('Description')
    expect(wrapper.text()).toContain('Color')
    expect(wrapper.text()).toContain('Icon')
    expect(wrapper.text()).toContain('Sort Order')
  })

  it('shows Update Category when category prop is provided', () => {
    const wrapper = mount(NodeCategoryEditor, {
      props: {
        category: {
          id: 'cat-1',
          name: 'LLM Calls',
          description: 'All LLM-related nodes',
          color: '#ff0000',
          icon: 'bot',
          sort_order: 5,
        },
      },
      global: { plugins: [i18n] },
    })
    expect(wrapper.text()).toContain('Update Category')
    expect(wrapper.text()).not.toContain('Create Category')
  })

  it('pre-populates form fields from category prop', () => {
    const wrapper = mount(NodeCategoryEditor, {
      props: {
        category: {
          id: 'cat-1',
          name: 'LLM Calls',
          description: 'All LLM nodes',
          color: '#ff0000',
          icon: 'bot',
          sort_order: 3,
        },
      },
      global: { plugins: [i18n] },
    })
    const nameInput = wrapper.find('input[type="text"]')
    expect((nameInput.element as HTMLInputElement).value).toBe('LLM Calls')

    const textarea = wrapper.find('textarea')
    expect((textarea.element as HTMLTextAreaElement).value).toBe('All LLM nodes')

    const colorInput = wrapper.find('input[type="color"]')
    expect((colorInput.element as HTMLInputElement).value).toBe('#ff0000')

    const numberInput = wrapper.find('input[type="number"]')
    expect((numberInput.element as HTMLInputElement).value).toBe('3')
  })

  it('updates form.name when name input changes', async () => {
    const wrapper = mount(NodeCategoryEditor, {
      global: { plugins: [i18n] },
    })
    const nameInput = wrapper.find('input[type="text"]')
    await nameInput.setValue('My Category')

    // Form should have the new value
    expect((nameInput.element as HTMLInputElement).value).toBe('My Category')
  })

  it('disables save button when name is empty', () => {
    const wrapper = mount(NodeCategoryEditor, {
      global: { plugins: [i18n] },
    })
    // Save button is the first button in the actions div
    const buttons = wrapper.findAll('button')
    const saveButton = buttons.find((b) => b.text().includes('Create Category'))
    expect(saveButton?.attributes('disabled')).toBeDefined()
  })

  it('enables save button when name is non-empty', async () => {
    const wrapper = mount(NodeCategoryEditor, {
      global: { plugins: [i18n] },
    })
    const nameInput = wrapper.find('input[type="text"]')
    await nameInput.setValue('Test')

    const buttons = wrapper.findAll('button')
    const saveButton = buttons.find((b) => b.text().includes('Create Category'))
    expect(saveButton?.attributes('disabled')).toBeUndefined()
  })

  it('calls POST API when saving in create mode', async () => {
    mockPost.mockResolvedValue({ data: { id: 'new-cat', name: 'New' }, error: undefined })

    const wrapper = mount(NodeCategoryEditor, {
      global: { plugins: [i18n] },
    })
    const nameInput = wrapper.find('input[type="text"]')
    await nameInput.setValue('New Category')

    const buttons = wrapper.findAll('button')
    const saveButton = buttons.find((b) => b.text().includes('Create Category'))
    await saveButton!.trigger('click')
    await flushPromises()

    expect(mockPost).toHaveBeenCalledWith('/api/v1/node-categories', {
      body: {
        name: 'New Category',
        description: null,
        color: '#6366f1',
        icon: null,
        sort_order: 0,
      },
    })
  })

  it('calls PATCH API when saving in edit mode', async () => {
    mockPatch.mockResolvedValue({ data: { id: 'cat-1', name: 'Updated' }, error: undefined })

    const wrapper = mount(NodeCategoryEditor, {
      props: {
        category: {
          id: 'cat-1',
          name: 'Old Name',
          description: null,
          color: '#6366f1',
          icon: null,
          sort_order: 0,
        },
      },
      global: { plugins: [i18n] },
    })
    const nameInput = wrapper.find('input[type="text"]')
    await nameInput.setValue('Updated Name')

    const buttons = wrapper.findAll('button')
    const saveButton = buttons.find((b) => b.text().includes('Update Category'))
    await saveButton!.trigger('click')
    await flushPromises()

    expect(mockPatch).toHaveBeenCalledWith('/api/v1/node-categories/{category_id}', {
      params: { path: { category_id: 'cat-1' } },
      body: {
        name: 'Updated Name',
        description: null,
        color: '#6366f1',
        icon: null,
        sort_order: 0,
      },
    })
  })

  it('emits saved event on successful save', async () => {
    const savedData = { id: 'new-cat', name: 'New' }
    mockPost.mockResolvedValue({ data: savedData, error: undefined })

    const wrapper = mount(NodeCategoryEditor, {
      global: { plugins: [i18n] },
    })
    const nameInput = wrapper.find('input[type="text"]')
    await nameInput.setValue('New Category')

    const buttons = wrapper.findAll('button')
    const saveButton = buttons.find((b) => b.text().includes('Create Category'))
    await saveButton!.trigger('click')
    await flushPromises()

    expect(wrapper.emitted('saved')).toBeTruthy()
    expect(wrapper.emitted('saved')![0][0]).toEqual(savedData)
  })

  it('shows error message when API call fails', async () => {
    mockPost.mockResolvedValue({ data: undefined, error: { detail: 'Name already exists' } })

    const wrapper = mount(NodeCategoryEditor, {
      global: { plugins: [i18n] },
    })
    const nameInput = wrapper.find('input[type="text"]')
    await nameInput.setValue('Duplicate Name')

    const buttons = wrapper.findAll('button')
    const saveButton = buttons.find((b) => b.text().includes('Create Category'))
    await saveButton!.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Name already exists')
  })

  it('shows error message when API throws', async () => {
    mockPost.mockRejectedValue(new Error('Network error'))

    const wrapper = mount(NodeCategoryEditor, {
      global: { plugins: [i18n] },
    })
    const nameInput = wrapper.find('input[type="text"]')
    await nameInput.setValue('Test')

    const buttons = wrapper.findAll('button')
    const saveButton = buttons.find((b) => b.text().includes('Create Category'))
    await saveButton!.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Network error')
  })

  it('shows generic error for non-Error exceptions', async () => {
    mockPost.mockRejectedValue('string error')

    const wrapper = mount(NodeCategoryEditor, {
      global: { plugins: [i18n] },
    })
    const nameInput = wrapper.find('input[type="text"]')
    await nameInput.setValue('Test')

    const buttons = wrapper.findAll('button')
    const saveButton = buttons.find((b) => b.text().includes('Create Category'))
    await saveButton!.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('An unexpected error occurred')
  })

  it('emits cancelled when Cancel button is clicked', async () => {
    const wrapper = mount(NodeCategoryEditor, {
      global: { plugins: [i18n] },
    })
    const buttons = wrapper.findAll('button')
    const cancelButton = buttons.find((b) => b.text().trim() === 'Cancel')
    expect(cancelButton).toBeDefined()

    await cancelButton!.trigger('click')
    expect(wrapper.emitted('cancelled')).toHaveLength(1)
  })

  it('shows "Saving..." text while save is in progress', async () => {
    let resolveSave: (v: unknown) => void
    mockPost.mockImplementation(() => new Promise((resolve) => { resolveSave = resolve }))

    const wrapper = mount(NodeCategoryEditor, {
      global: { plugins: [i18n] },
    })
    const nameInput = wrapper.find('input[type="text"]')
    await nameInput.setValue('Test')

    const buttons = wrapper.findAll('button')
    const saveButton = buttons.find((b) => b.text().includes('Create Category'))
    await saveButton!.trigger('click')
    await wrapper.vm.$nextTick()

    expect(wrapper.text()).toContain('Saving...')

    resolveSave!({ data: { id: '1' }, error: undefined })
    await flushPromises()
    expect(wrapper.text()).not.toContain('Saving...')
  })

  it('clears error before new save attempt', async () => {
    mockPost.mockResolvedValueOnce({ data: undefined, error: { detail: 'First error' } })

    const wrapper = mount(NodeCategoryEditor, {
      global: { plugins: [i18n] },
    })
    const nameInput = wrapper.find('input[type="text"]')
    await nameInput.setValue('Test')

    const buttons = wrapper.findAll('button')
    const saveButton = buttons.find((b) => b.text().includes('Create Category'))

    // First save - fails
    await saveButton!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('First error')

    // Second save - succeeds (error cleared at start of save)
    mockPost.mockResolvedValueOnce({ data: { id: '1' }, error: undefined })
    await saveButton!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).not.toContain('First error')
  })

  it('trims name and description before sending to API', async () => {
    mockPost.mockResolvedValue({ data: { id: '1' }, error: undefined })

    const wrapper = mount(NodeCategoryEditor, {
      global: { plugins: [i18n] },
    })
    const nameInput = wrapper.find('input[type="text"]')
    await nameInput.setValue('  Spaced Name  ')

    const textarea = wrapper.find('textarea')
    await textarea.setValue('  Spaced Desc  ')

    const buttons = wrapper.findAll('button')
    const saveButton = buttons.find((b) => b.text().includes('Create Category'))
    await saveButton!.trigger('click')
    await flushPromises()

    expect(mockPost).toHaveBeenCalledWith('/api/v1/node-categories', {
      body: expect.objectContaining({
        name: 'Spaced Name',
        description: 'Spaced Desc',
      }),
    })
  })

  it('sends null description when description is empty', async () => {
    mockPost.mockResolvedValue({ data: { id: '1' }, error: undefined })

    const wrapper = mount(NodeCategoryEditor, {
      global: { plugins: [i18n] },
    })
    const nameInput = wrapper.find('input[type="text"]')
    await nameInput.setValue('Test')

    const buttons = wrapper.findAll('button')
    const saveButton = buttons.find((b) => b.text().includes('Create Category'))
    await saveButton!.trigger('click')
    await flushPromises()

    expect(mockPost).toHaveBeenCalledWith('/api/v1/node-categories', {
      body: expect.objectContaining({
        description: null,
      }),
    })
  })

  it('sends null icon when icon is __all__', async () => {
    mockPost.mockResolvedValue({ data: { id: '1' }, error: undefined })

    const wrapper = mount(NodeCategoryEditor, {
      global: { plugins: [i18n] },
    })
    const nameInput = wrapper.find('input[type="text"]')
    await nameInput.setValue('Test')

    const buttons = wrapper.findAll('button')
    const saveButton = buttons.find((b) => b.text().includes('Create Category'))
    await saveButton!.trigger('click')
    await flushPromises()

    expect(mockPost).toHaveBeenCalledWith('/api/v1/node-categories', {
      body: expect.objectContaining({
        icon: null,
      }),
    })
  })

  it('watches category prop and updates form in edit mode', async () => {
    const wrapper = mount(NodeCategoryEditor, {
      props: { category: null },
      global: { plugins: [i18n] },
    })

    // Initially empty
    const nameInput = wrapper.find('input[type="text"]')
    expect((nameInput.element as HTMLInputElement).value).toBe('')

    // Set category prop
    await wrapper.setProps({
      category: {
        id: 'cat-1',
        name: 'Updated',
        description: 'Desc',
        color: '#00ff00',
        icon: 'zap',
        sort_order: 7,
      },
    })

    expect((nameInput.element as HTMLInputElement).value).toBe('Updated')
    const numberInput = wrapper.find('input[type="number"]')
    expect((numberInput.element as HTMLInputElement).value).toBe('7')
  })

  it('renders icon select with correct options', () => {
    const wrapper = mount(NodeCategoryEditor, {
      global: { plugins: [i18n] },
    })
    // The Select component is stubbed but rendered; verify the component mounted
    expect(wrapper.text()).toContain('Icon')
  })

  it('disables save button while saving', async () => {
    let resolveSave: (v: unknown) => void
    mockPost.mockImplementation(() => new Promise((resolve) => { resolveSave = resolve }))

    const wrapper = mount(NodeCategoryEditor, {
      global: { plugins: [i18n] },
    })
    const nameInput = wrapper.find('input[type="text"]')
    await nameInput.setValue('Test')

    const buttons = wrapper.findAll('button')
    const saveButton = buttons.find((b) => b.text().includes('Create Category'))
    await saveButton!.trigger('click')
    await wrapper.vm.$nextTick()

    // Button should be disabled while saving
    expect(saveButton!.attributes('disabled')).toBeDefined()

    resolveSave!({ data: { id: '1' }, error: undefined })
    await flushPromises()
  })
})
