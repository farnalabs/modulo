import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'

const { getMock, postMock, patchMock, deleteMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  postMock: vi.fn(),
  patchMock: vi.fn(),
  deleteMock: vi.fn(),
}))

vi.mock('../composables/useApi', () => ({
  useApi: () => ({
    get: getMock,
    post: postMock,
    patch: patchMock,
    delete: deleteMock,
  }),
}))

import FolderTree from '../components/pipelines/FolderTree.vue'

interface FolderItem {
  id: string
  organisation_id: string
  name: string
  parent_id: string | null
  sort_order: number
}

function makeFolder(overrides: Partial<FolderItem> = {}): FolderItem {
  return {
    id: 'f1',
    organisation_id: 'org-1',
    name: 'Root Folder',
    parent_id: null,
    sort_order: 0,
    ...overrides,
  }
}

const DialogStub = {
  name: 'DialogStub',
  props: { visible: { type: Boolean, default: false } },
  emits: ['update:visible'],
  template: '<div v-if="visible" data-testid="dialog-stub"><slot name="header" /><slot /><slot name="footer" /></div>',
}

const InputTextStub = {
  name: 'InputTextStub',
  props: ['modelValue'],
  emits: ['update:modelValue'],
  template: '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
}

const SelectStub = {
  name: 'SelectStub',
  props: ['modelValue', 'options', 'optionLabel', 'optionValue', 'placeholder', 'ariaLabel', 'id'],
  emits: ['update:modelValue'],
  template: `
    <select :id="id" :aria-label="ariaLabel || 'select'" @change="$emit('update:modelValue', $event.target.value)">
      <option v-for="o in options" :key="String(o.value ?? o)" :value="o.value ?? o">{{ o.label ?? o }}</option>
    </select>`,
}

const ButtonStub = {
  name: 'ButtonStub',
  props: ['disabled', 'loading', 'severity', 'outlined', 'size'],
  emits: ['click'],
  template: '<button type="button" :disabled="disabled || loading" @click="$emit(\'click\')"><slot /></button>',
}

const DraggableStub = {
  name: 'DraggableStub',
  props: ['modelValue', 'itemKey'],
  emits: ['end', 'change'],
  template: `
    <div data-testid="draggable-stub">
      <template v-for="element in modelValue" :key="element.folder.id">
        <slot name="item" :element="element" />
      </template>
    </div>`,
}

function mountTree(props: Record<string, unknown> = {}) {
  return mount(FolderTree, {
    props: { selectedFolderId: null, ...props },
    global: {
      stubs: {
        Dialog: DialogStub,
        InputText: InputTextStub,
        Select: SelectStub,
        Button: ButtonStub,
        draggable: DraggableStub,
      },
    },
  })
}

async function mountTreeWithFolders(folders: FolderItem[], props: Record<string, unknown> = {}) {
  getMock.mockResolvedValue(folders)
  const wrapper = mountTree(props)
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  getMock.mockResolvedValue([])
  postMock.mockResolvedValue(makeFolder())
  patchMock.mockResolvedValue(makeFolder())
  deleteMock.mockResolvedValue(undefined)
})

describe('FolderTree height chain', () => {
  it('fills the full height of its sidebar column (h-full, not the old h-screen sticky)', () => {
    const wrapper = mountTree()
    const root = wrapper.find('[data-testid="folder-tree"]')
    expect(root.exists()).toBe(true)
    const classes = root.classes()
    expect(classes).toContain('h-full')
    expect(classes).toContain('min-h-0')
    expect(classes).toContain('flex-col')
    // The old h-screen + sticky approach left the tree shorter than the
    // column next to a long table — it must not come back.
    expect(classes).not.toContain('h-screen')
    expect(classes).not.toContain('sticky')
  })

  it('scrolls internally when its content exceeds the column (flex-1 min-h-0 overflow-y-auto body)', () => {
    const wrapper = mountTree()
    const root = wrapper.find('[data-testid="folder-tree"]')
    const body = root.element.children[1] as HTMLElement
    expect(body.className).toContain('flex-1')
    expect(body.className).toContain('min-h-0')
    expect(body.className).toContain('overflow-y-auto')
  })

  it('stays hidden below md where the view offers a mobile folder select instead', () => {
    const wrapper = mountTree()
    const classes = wrapper.find('[data-testid="folder-tree"]').classes()
    expect(classes).toContain('hidden')
    expect(classes).toContain('md:flex')
  })
})

describe('FolderTree loading, error and empty states', () => {
  it('shows skeletons while folders are loading', async () => {
    getMock.mockReturnValue(new Promise(() => undefined))
    const wrapper = mountTree()
    await nextTick()
    expect(wrapper.findAll('.animate-pulse').length).toBe(3)
    expect(wrapper.text()).not.toContain('No folders yet')
  })

  it('shows an error message when loading fails', async () => {
    getMock.mockRejectedValue(new Error('folders unavailable'))
    const wrapper = mountTree()
    await flushPromises()
    expect(wrapper.text()).toContain('folders unavailable')
  })

  it('shows the empty state when there are no folders', async () => {
    const wrapper = await mountTreeWithFolders([])
    expect(wrapper.text()).toContain('No folders yet')
  })
})

describe('FolderTree rendering and selection', () => {
  it('renders nested folders in depth order with indentation and counts', async () => {
    const wrapper = await mountTreeWithFolders(
      [makeFolder(), makeFolder({ id: 'f2', name: 'Child Folder', parent_id: 'f1', sort_order: 0 })],
      { itemCounts: { f1: 3, f2: 1 } },
    )
    const items = wrapper.findAll('[data-testid^="folder-tree-item-"]')
    expect(items.map(i => i.text())).toEqual(['Root Folder3', 'Child Folder1'])
    expect(items[0].text()).toContain('Root Folder')
    expect(items[0].text()).toContain('3')
    expect(items[1].text()).toContain('Child Folder')
    expect(items[1].text()).toContain('1')
    const style = items[1].attributes('style')
    expect(style).toContain('padding-left: 28px')
  })

  it('emits select-folder with the folder id, and null for All Pipelines', async () => {
    const wrapper = await mountTreeWithFolders([makeFolder()])
    await wrapper.find('[data-testid="folder-tree-item-f1"]').trigger('click')
    expect(wrapper.emitted('select-folder')![0]).toEqual(['f1'])
    await wrapper.find('[data-testid="folder-tree-all-pipelines"]').trigger('click')
    expect(wrapper.emitted('select-folder')![1]).toEqual([null])
  })

  it('selects a folder with keyboard enter and space', async () => {
    const wrapper = await mountTreeWithFolders([makeFolder()])
    await wrapper.find('[data-testid="folder-tree-item-f1"]').trigger('keydown.enter')
    await wrapper.find('[data-testid="folder-tree-item-f1"]').trigger('keydown.space')
    expect(wrapper.emitted('select-folder')).toHaveLength(2)
  })

  it('highlights the selected folder', async () => {
    const wrapper = await mountTreeWithFolders([makeFolder()], { selectedFolderId: 'f1' })
    expect(wrapper.find('[data-testid="folder-tree-item-f1"]').classes()).toContain('bg-accent')
  })
})

describe('FolderTree create flow', () => {
  it('opens the create dialog and posts a root-level folder', async () => {
    const wrapper = await mountTreeWithFolders([])
    await wrapper.find('[data-testid="folder-tree-new"]').trigger('click')
    const dialog = wrapper.find('[data-testid="dialog-stub"]')
    expect(dialog.exists()).toBe(true)
    expect(dialog.text()).toContain('New Folder')
    await dialog.find('input').setValue('My New Folder')
    const saveBtn = dialog.findAll('button').find(b => b.text() === 'Save')
    await saveBtn!.trigger('click')
    await flushPromises()
    expect(postMock).toHaveBeenCalledWith('/api/v1/pipeline-folders', { name: 'My New Folder' })
    expect(wrapper.emitted('folders-changed')).toHaveLength(1)
    expect(wrapper.find('[data-testid="dialog-stub"]').exists()).toBe(false)
  })

  it('posts a parent_id when a parent folder is selected', async () => {
    const wrapper = await mountTreeWithFolders([makeFolder()])
    await wrapper.find('[data-testid="folder-tree-new"]').trigger('click')
    const dialog = wrapper.find('[data-testid="dialog-stub"]')
    await dialog.find('input').setValue('Nested')
    await dialog.find('select').setValue('f1')
    const saveBtn = dialog.findAll('button').find(b => b.text() === 'Save')
    await saveBtn!.trigger('click')
    await flushPromises()
    expect(postMock).toHaveBeenCalledWith('/api/v1/pipeline-folders', { name: 'Nested', parent_id: 'f1' })
  })

  it('shows a create error inline and keeps the dialog open', async () => {
    postMock.mockRejectedValue(new Error('name taken'))
    const wrapper = await mountTreeWithFolders([])
    await wrapper.find('[data-testid="folder-tree-new"]').trigger('click')
    const dialog = wrapper.find('[data-testid="dialog-stub"]')
    await dialog.find('input').setValue('Dup')
    const saveBtn = dialog.findAll('button').find(b => b.text() === 'Save')
    await saveBtn!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('name taken')
    expect(wrapper.find('[data-testid="dialog-stub"]').exists()).toBe(true)
  })

  it('disables save while the folder name is empty', async () => {
    const wrapper = await mountTreeWithFolders([])
    await wrapper.find('[data-testid="folder-tree-new"]').trigger('click')
    const dialog = wrapper.find('[data-testid="dialog-stub"]')
    const saveBtn = dialog.findAll('button').find(b => b.text() === 'Save')
    expect(saveBtn!.attributes('disabled')).toBeDefined()
  })

  it('closes the create dialog on cancel without posting', async () => {
    const wrapper = await mountTreeWithFolders([])
    await wrapper.find('[data-testid="folder-tree-new"]').trigger('click')
    const dialog = wrapper.find('[data-testid="dialog-stub"]')
    const cancelBtn = dialog.findAll('button').find(b => b.text() === 'Cancel')
    await cancelBtn!.trigger('click')
    expect(wrapper.find('[data-testid="dialog-stub"]').exists()).toBe(false)
    expect(postMock).not.toHaveBeenCalled()
  })
})

describe('FolderTree rename flow', () => {
  it('opens pre-filled and patches the folder name', async () => {
    const wrapper = await mountTreeWithFolders([makeFolder()])
    const renameBtn = wrapper.findAll('button').find(b => b.attributes('aria-label') === 'Rename Folder')
    await renameBtn!.trigger('click')
    const dialog = wrapper.find('[data-testid="dialog-stub"]')
    expect(dialog.exists()).toBe(true)
    const input = dialog.find('input')
    expect((input.element as HTMLInputElement).value).toBe('Root Folder')
    await input.setValue('Renamed Folder')
    const saveBtn = dialog.findAll('button').find(b => b.text() === 'Save')
    await saveBtn!.trigger('click')
    await flushPromises()
    expect(patchMock).toHaveBeenCalledWith('/api/v1/pipeline-folders/f1', { name: 'Renamed Folder' })
    expect(wrapper.emitted('folders-changed')).toHaveLength(1)
    expect(wrapper.find('[data-testid="dialog-stub"]').exists()).toBe(false)
  })

  it('shows a rename error inline', async () => {
    patchMock.mockRejectedValue(new Error('rename conflict'))
    const wrapper = await mountTreeWithFolders([makeFolder()])
    const renameBtn = wrapper.findAll('button').find(b => b.attributes('aria-label') === 'Rename Folder')
    await renameBtn!.trigger('click')
    const dialog = wrapper.find('[data-testid="dialog-stub"]')
    const saveBtn = dialog.findAll('button').find(b => b.text() === 'Save')
    await saveBtn!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('rename conflict')
    expect(wrapper.find('[data-testid="dialog-stub"]').exists()).toBe(true)
  })

  it('uses a custom apiBase when provided', async () => {
    const wrapper = await mountTreeWithFolders([makeFolder()], { apiBase: '/api/v1/job-folders' })
    const renameBtn = wrapper.findAll('button').find(b => b.attributes('aria-label') === 'Rename Folder')
    await renameBtn!.trigger('click')
    const dialog = wrapper.find('[data-testid="dialog-stub"]')
    const saveBtn = dialog.findAll('button').find(b => b.text() === 'Save')
    await saveBtn!.trigger('click')
    await flushPromises()
    expect(patchMock).toHaveBeenCalledWith('/api/v1/job-folders/f1', { name: 'Root Folder' })
    expect(getMock).toHaveBeenCalledWith('/api/v1/job-folders')
  })
})

describe('FolderTree delete flow', () => {
  it('asks for confirmation and deletes, resetting the selection when the selected folder is deleted', async () => {
    const wrapper = await mountTreeWithFolders([makeFolder()], { selectedFolderId: 'f1' })
    const deleteBtn = wrapper.findAll('button').find(b => b.attributes('aria-label') === 'Delete Folder')
    await deleteBtn!.trigger('click')
    const dialog = wrapper.find('[data-testid="dialog-stub"]')
    expect(dialog.exists()).toBe(true)
    expect(dialog.text()).toContain('Are you sure you want to delete this folder?')
    const confirmBtn = dialog.findAll('button').find(b => b.text() === 'Delete')
    await confirmBtn!.trigger('click')
    await flushPromises()
    expect(deleteMock).toHaveBeenCalledWith('/api/v1/pipeline-folders/f1')
    expect(wrapper.emitted('select-folder')![0]).toEqual([null])
    expect(wrapper.emitted('folders-changed')).toHaveLength(1)
  })

  it('mentions where items will be moved when the folder still has items', async () => {
    const wrapper = await mountTreeWithFolders([makeFolder()], { itemCounts: { f1: 4 } })
    const deleteBtn = wrapper.findAll('button').find(b => b.attributes('aria-label') === 'Delete Folder')
    await deleteBtn!.trigger('click')
    const dialog = wrapper.find('[data-testid="dialog-stub"]')
    expect(dialog.text()).toContain('Pipelines will be moved to Uncategorised')
  })

  it('shows a delete error inline and keeps the dialog open', async () => {
    deleteMock.mockRejectedValue(new Error('folder not empty'))
    const wrapper = await mountTreeWithFolders([makeFolder()])
    const deleteBtn = wrapper.findAll('button').find(b => b.attributes('aria-label') === 'Delete Folder')
    await deleteBtn!.trigger('click')
    const dialog = wrapper.find('[data-testid="dialog-stub"]')
    const confirmBtn = dialog.findAll('button').find(b => b.text() === 'Delete')
    await confirmBtn!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('folder not empty')
    expect(wrapper.find('[data-testid="dialog-stub"]').exists()).toBe(true)
  })
})

describe('FolderTree drag and drop', () => {
  it('emits move-pipeline when a pipeline is dropped on a folder', async () => {
    const wrapper = await mountTreeWithFolders([makeFolder()])
    await wrapper.find('[data-testid="folder-tree-item-f1"]').trigger('drop', {
      dataTransfer: { getData: () => 'pipe-9' },
    })
    expect(wrapper.emitted('move-pipeline')![0]).toEqual([{ pipelineId: 'pipe-9', folderId: 'f1' }])
    expect(wrapper.emitted('folders-changed')).toHaveLength(1)
  })

  it('emits move-pipeline with null folderId when dropped on the root', async () => {
    const wrapper = await mountTreeWithFolders([makeFolder()])
    await wrapper.find('[data-testid="folder-tree-all-pipelines"]').trigger('drop', {
      dataTransfer: { getData: () => 'pipe-9' },
    })
    expect(wrapper.emitted('move-pipeline')![0]).toEqual([{ pipelineId: 'pipe-9', folderId: null }])
    expect(wrapper.find('[data-testid="folder-tree-all-pipelines"]').classes()).not.toContain('ring-primary')
  })

  it('does not emit move-pipeline when the drop carries no payload', async () => {
    const wrapper = await mountTreeWithFolders([makeFolder()])
    await wrapper.find('[data-testid="folder-tree-item-f1"]').trigger('drop', {
      dataTransfer: { getData: () => '' },
    })
    expect(wrapper.emitted('move-pipeline')).toBeUndefined()
  })

  it('persists sort_order changes on drag end', async () => {
    const wrapper = await mountTreeWithFolders([
      makeFolder(),
      makeFolder({ id: 'f2', name: 'Second', sort_order: 5 }),
    ])
    const draggable = wrapper.findComponent({ name: 'DraggableStub' })
    draggable.vm.$emit('end')
    await flushPromises()
    expect(patchMock).toHaveBeenCalledWith('/api/v1/pipeline-folders/f2/move', { sort_order: 1 })
    expect(patchMock).not.toHaveBeenCalledWith('/api/v1/pipeline-folders/f1/move', expect.anything())
  })

  it('warns and continues when a sort_order patch fails', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    patchMock.mockRejectedValue(new Error('move failed'))
    const wrapper = await mountTreeWithFolders([
      makeFolder(),
      makeFolder({ id: 'f2', name: 'Second', sort_order: 5 }),
    ])
    const draggable = wrapper.findComponent({ name: 'DraggableStub' })
    draggable.vm.$emit('end')
    await flushPromises()
    expect(warnSpy).toHaveBeenCalledWith(expect.stringContaining('f2'))
    warnSpy.mockRestore()
  })
})
