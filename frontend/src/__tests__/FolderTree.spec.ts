import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'

const { getMock } = vi.hoisted(() => ({ getMock: vi.fn() }))

vi.mock('../composables/useApi', () => ({
  useApi: () => ({
    get: getMock,
    post: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  }),
}))

import FolderTree from '../components/pipelines/FolderTree.vue'

function mountTree() {
  return mount(FolderTree, {
    props: { selectedFolderId: null },
    global: {
      stubs: {
        Dialog: true,
        InputText: true,
        Select: true,
        Button: true,
        draggable: true,
      },
    },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  getMock.mockResolvedValue([])
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
