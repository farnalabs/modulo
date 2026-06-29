import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

vi.mock('../../lib/api/client', () => ({
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
  clearAccessToken: vi.fn(),
  api: {
    GET: vi.fn(),
    POST: vi.fn(),
    PUT: vi.fn(),
    PATCH: vi.fn(),
    DELETE: vi.fn(),
  },
}))

import NodeCategoryEditor from '../../components/NodeCategoryEditor.vue'

const editCategory = {
  id: '550e8400-e29b-41d4-a716-446655440000',
  name: 'LLM Call',
  description: 'Nodes that invoke an LLM',
  color: '#6366f1',
  icon: 'bot',
  sort_order: 1,
}

describe('NodeCategoryEditor', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders form fields', async () => {
    const wrapper = mount(NodeCategoryEditor, { props: { category: null } })
    await nextTick()

    const inputs = wrapper.findAll('input')
    const textareas = wrapper.findAll('textarea')
    const selects = wrapper.findAll('select')

    expect(inputs.length).toBeGreaterThanOrEqual(2)
    expect(textareas.length).toBeGreaterThanOrEqual(1)
    expect(selects.length).toBeGreaterThanOrEqual(1)

    const nameInput = wrapper.find('input[type="text"]')
    expect(nameInput.exists()).toBe(true)
  })

  it('shows create mode by default', async () => {
    const wrapper = mount(NodeCategoryEditor, { props: { category: null } })
    await nextTick()

    expect(wrapper.text()).toContain('Create Category')
    expect(wrapper.text()).not.toContain('Update Category')
  })

  it('pre-fills form in edit mode', async () => {
    const wrapper = mount(NodeCategoryEditor, { props: { category: editCategory } })
    await nextTick()

    const nameInput = wrapper.find('input[type="text"]')
    expect((nameInput.element as HTMLInputElement).value).toBe('LLM Call')
  })

  it('shows update mode for existing category', async () => {
    const wrapper = mount(NodeCategoryEditor, { props: { category: editCategory } })
    await nextTick()

    expect(wrapper.text()).toContain('Update Category')
    expect(wrapper.text()).not.toContain('Create Category')
  })

  it('renders color picker', async () => {
    const wrapper = mount(NodeCategoryEditor, { props: { category: null } })
    await nextTick()

    const colorInput = wrapper.find('input[type="color"]')
    expect(colorInput.exists()).toBe(true)
    expect((colorInput.element as HTMLInputElement).value).toBe('#6366f1')
  })

  it('renders icon dropdown', async () => {
    const wrapper = mount(NodeCategoryEditor, { props: { category: null } })
    await nextTick()

    const select = wrapper.find('select')
    expect(select.exists()).toBe(true)
    const options = select.findAll('option')
    expect(options.length).toBeGreaterThan(5)
  })

  it('emits cancelled when cancel is clicked', async () => {
    const wrapper = mount(NodeCategoryEditor, { props: { category: null } })
    await nextTick()

    const cancelBtn = wrapper.findAll('button').filter(b => b.text() === 'Cancel')
    expect(cancelBtn.length).toBe(1)
    await cancelBtn[0].trigger('click')

    expect(wrapper.emitted('cancelled')).toBeTruthy()
    expect(wrapper.emitted('cancelled')!.length).toBe(1)
  })

  it('disables save button when name is empty', async () => {
    const wrapper = mount(NodeCategoryEditor, { props: { category: null } })
    await nextTick()

    const saveBtn = wrapper.findAll('button').filter(b => b.text() === 'Create Category')
    expect(saveBtn.length).toBe(1)
    expect((saveBtn[0].element as HTMLButtonElement).disabled).toBe(true)
  })

  it('pre-fills color in edit mode', async () => {
    const wrapper = mount(NodeCategoryEditor, { props: { category: editCategory } })
    await nextTick()

    const colorInput = wrapper.find('input[type="color"]')
    expect((colorInput.element as HTMLInputElement).value).toBe('#6366f1')
  })
})
