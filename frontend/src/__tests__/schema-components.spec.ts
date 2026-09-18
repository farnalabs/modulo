import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import SchemaFieldEditor from '../components/schema/SchemaFieldEditor.vue'
import SchemaEditorForm from '../components/schema/SchemaEditorForm.vue'
import SchemaJsonPreview from '../components/schema/SchemaJsonPreview.vue'
import SchemaVersionHistory from '../components/schema/SchemaVersionHistory.vue'
import SchemaEditorSidebar from '../components/schema/SchemaEditorSidebar.vue'
import type { SchemaField } from '../utils/schema-definition'

const baseField: SchemaField = {
  _key: 1,
  name: 'email',
  type: 'string',
  required: true,
  description: 'User email',
  defaultValue: 'a@b.c',
}

const version = {
  id: 'v-1',
  schema_id: 'schema-1',
  version: '1.0.0',
  version_number: 1,
  definition_json: { type: 'object', properties: {} },
  published: true,
  created_at: '2026-01-01T00:00:00Z',
}

vi.mock('../lib/formatDate', () => ({
  formatDateShort: vi.fn(() => 'Jan 1, 2026'),
}))

describe('SchemaFieldEditor', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders field values', () => {
    const wrapper = mount(SchemaFieldEditor, {
      props: { field: baseField, index: 0, isFirst: true, isLast: false },
      global: {
        stubs: {
          Select: { template: '<div><slot /></div>' },
          SelectTrigger: { template: '<button type="button"><slot /></button>' },
          SelectContent: { template: '<div><slot /></div>' },
          SelectItem: { template: '<div><slot /></div>' },
          SelectValue: { template: '<span><slot /></span>' },
        },
      },
    })

    const nameInput = wrapper.find('[data-testid="schema-editor-field-name"]').element as HTMLInputElement
    expect(nameInput.value).toBe('email')
    const descInput = wrapper.find('[data-testid="schema-editor-field-description"]').element as HTMLInputElement
    expect(descInput.value).toBe('User email')
    const required = wrapper.find('[data-testid="schema-editor-field-required"]').element as HTMLInputElement
    expect(required.checked).toBe(true)
  })

  it('emits move-up, move-down and remove', async () => {
    const wrapper = mount(SchemaFieldEditor, {
      props: { field: baseField, index: 1, isFirst: false, isLast: false },
      global: {
        stubs: {
          Select: { template: '<div><slot /></div>' },
          SelectTrigger: { template: '<button type="button"><slot /></button>' },
          SelectContent: { template: '<div><slot /></div>' },
          SelectItem: { template: '<div><slot /></div>' },
          SelectValue: { template: '<span><slot /></span>' },
        },
      },
    })

    await wrapper.find('[data-testid="schema-editor-field-move-up"]').trigger('click')
    await wrapper.find('[data-testid="schema-editor-field-move-down"]').trigger('click')
    await wrapper.find('[data-testid="schema-editor-field-remove"]').trigger('click')

    expect(wrapper.emitted('move-up')).toBeTruthy()
    expect(wrapper.emitted('move-down')).toBeTruthy()
    expect(wrapper.emitted('remove')).toBeTruthy()
  })

  it('emits update:field with patched values', async () => {
    const wrapper = mount(SchemaFieldEditor, {
      props: { field: baseField, index: 0, isFirst: true, isLast: false },
      global: {
        stubs: {
          Select: { template: '<div><slot /></div>' },
          SelectTrigger: { template: '<button type="button"><slot /></button>' },
          SelectContent: { template: '<div><slot /></div>' },
          SelectItem: { template: '<div><slot /></div>' },
          SelectValue: { template: '<span><slot /></span>' },
        },
      },
    })

    const nameInput = wrapper.find('[data-testid="schema-editor-field-name"]')
    await nameInput.setValue('full_name')
    const emitted = wrapper.emitted('update:field')
    expect(emitted).toBeTruthy()
    expect((emitted![0] as unknown[])[0]).toMatchObject({ _key: 1, name: 'full_name' })
  })

  it('uses unique input ids per field instance', () => {
    const wrapper = mount(SchemaFieldEditor, {
      props: { field: baseField, index: 0, isFirst: true, isLast: false },
      global: {
        stubs: {
          Select: { template: '<div><slot /></div>' },
          SelectTrigger: { template: '<button type="button"><slot /></button>' },
          SelectContent: { template: '<div><slot /></div>' },
          SelectItem: { template: '<div><slot /></div>' },
          SelectValue: { template: '<span><slot /></span>' },
        },
      },
    })
    const second = mount(SchemaFieldEditor, {
      props: { field: { ...baseField, _key: 2 }, index: 1, isFirst: false, isLast: true },
      global: {
        stubs: {
          Select: { template: '<div><slot /></div>' },
          SelectTrigger: { template: '<button type="button"><slot /></button>' },
          SelectContent: { template: '<div><slot /></div>' },
          SelectItem: { template: '<div><slot /></div>' },
          SelectValue: { template: '<span><slot /></span>' },
        },
      },
    })

    const ids = [
      ...wrapper.findAll('input[id]').map(i => i.attributes('id')),
      ...second.findAll('input[id]').map(i => i.attributes('id')),
    ]
    expect(new Set(ids).size).toBe(ids.length)
  })
})

describe('SchemaEditorForm', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  const fields = () => [{ ...baseField }]

  it('adds a field with a unique key', async () => {
    const wrapper = mount(SchemaEditorForm, {
      props: {
        name: 'My Schema',
        description: '',
        version: '1.0.0',
        fields: fields(),
      },
      global: {
        stubs: {
          SchemaFieldEditor: { template: '<div data-testid="schema-editor-field" />' },
          Button: { template: '<button type="button"><slot /></button>' },
        },
      },
    })

    await wrapper.find('[data-testid="schema-editor-add-field"]').trigger('click')

    const emitted = wrapper.emitted('update:fields')
    expect(emitted).toBeTruthy()
    const nextFields = (emitted![0] as unknown[])[0] as SchemaField[]
    expect(nextFields).toHaveLength(2)
    expect(nextFields[1]._key).toBe(2)
  })

  it('removes a field by index', async () => {
    const wrapper = mount(SchemaEditorForm, {
      props: {
        name: 'My Schema',
        description: '',
        version: '1.0.0',
        fields: fields(),
      },
      global: {
        stubs: {
          SchemaFieldEditor: { template: '<div data-testid="schema-editor-field" />' },
          Button: { template: '<button type="button"><slot /></button>' },
        },
      },
    })
    const vm = wrapper.vm as any
    vm.removeField(0)
    await nextTick()
    expect(wrapper.emitted('update:fields')![0][0]).toEqual([])
  })

  it('moves a field within bounds', async () => {
    const two = [
      { ...baseField, _key: 1, name: 'a' },
      { ...baseField, _key: 2, name: 'b' },
    ]
    const wrapper = mount(SchemaEditorForm, {
      props: {
        name: 'My Schema',
        description: '',
        version: '1.0.0',
        fields: two,
      },
      global: {
        stubs: {
          SchemaFieldEditor: { template: '<div data-testid="schema-editor-field" />' },
          Button: { template: '<button type="button"><slot /></button>' },
        },
      },
    })
    const vm = wrapper.vm as any
    vm.moveField(1, -1)
    await nextTick()
    const moved = wrapper.emitted('update:fields')![0][0] as SchemaField[]
    expect(moved.map(f => f.name)).toEqual(['b', 'a'])
  })
})

describe('SchemaJsonPreview', () => {
  it('renders the json and emits copy', async () => {
    const wrapper = mount(SchemaJsonPreview, {
      props: { json: '{\n  "type": "object"\n}' },
    })
    expect(wrapper.find('[data-testid="schema-editor-json-preview"]').text()).toContain('"type"')
    await wrapper.find('[data-testid="schema-editor-copy-json"]').trigger('click')
    expect(wrapper.emitted('copy')).toBeTruthy()
  })
})

describe('SchemaVersionHistory', () => {
  it('shows loading spinner while loading', () => {
    const wrapper = mount(SchemaVersionHistory, {
      props: { versions: [], loading: true },
      global: {
        stubs: {
          LoadingSpinner: { template: '<div data-testid="loading-spinner" />' },
        },
      },
    })
    expect(wrapper.find('[data-testid="loading-spinner"]').exists()).toBe(true)
  })

  it('shows empty state when no versions', () => {
    const wrapper = mount(SchemaVersionHistory, {
      props: { versions: [], loading: false },
      global: {
        stubs: {
          LoadingSpinner: { template: '<div data-testid="loading-spinner" />' },
        },
      },
    })
    expect(wrapper.text()).toContain('No version history')
  })

  it('emits restore with the version', async () => {
    const wrapper = mount(SchemaVersionHistory, {
      props: { versions: [version], loading: false },
      global: {
        stubs: {
          LoadingSpinner: { template: '<div data-testid="loading-spinner" />' },
        },
      },
    })
    expect(wrapper.text()).toContain('v1.0.0')
    expect(wrapper.text()).toContain('Published')
    await wrapper.find('[data-testid="schema-editor-restore-version"]').trigger('click')
    expect(wrapper.emitted('restore')![0]).toEqual([version])
  })
})

describe('SchemaEditorSidebar', () => {
  const schema = {
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
  }

  it('emits select and create', async () => {
    const wrapper = mount(SchemaEditorSidebar, {
      props: { schemas: [schema], loading: false, selectedId: null, searchQuery: '' },
      global: {
        stubs: {
          LoadingSpinner: { template: '<div data-testid="loading-spinner" />' },
          FilterBar: { template: '<div data-testid="filter-bar" />' },
          Button: { template: '<button type="button"><slot /></button>' },
        },
      },
    })

    await wrapper.find('[data-testid="schema-editor-list-item"]').trigger('click')
    await wrapper.find('[data-testid="schema-editor-new"]').trigger('click')

    expect(wrapper.emitted('select')![0]).toEqual(['schema-1'])
    expect(wrapper.emitted('create')).toBeTruthy()
  })

  it('emits update:searchQuery from the filter bar', async () => {
    const wrapper = mount(SchemaEditorSidebar, {
      props: { schemas: [schema], loading: false, selectedId: null, searchQuery: '' },
      global: {
        stubs: {
          LoadingSpinner: { template: '<div data-testid="loading-spinner" />' },
          FilterBar: { template: '<input data-testid="filter-bar" :value="searchValue" @input="$emit(\'update:search\', $event.target.value)" />' },
          Button: { template: '<button type="button"><slot /></button>' },
        },
      },
    })
    await wrapper.find('[data-testid="filter-bar"]').setValue('user')
    expect(wrapper.emitted('update:searchQuery')![0]).toEqual(['user'])
  })
})
