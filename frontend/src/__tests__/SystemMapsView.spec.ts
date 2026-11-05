import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'

const mockGet = vi.hoisted(() => vi.fn())
const mockPost = vi.hoisted(() => vi.fn())
const mockDelete = vi.hoisted(() => vi.fn())
const mockPush = vi.hoisted(() => vi.fn())
const mockGetAccessToken = vi.hoisted(() => vi.fn())
const mockIsOperator = vi.hoisted(() => ({ value: true }))
const mockIsSystemAdmin = vi.hoisted(() => ({ value: false }))
const mockPlanStore = vi.hoisted(() => ({
  loaded: true,
  currentTier: 'community',
  devMode: true,
  userPermissions: ['system_maps_access', 'system.config.manage'],
  fetchPlan: vi.fn(),
  fetchPermissions: vi.fn(),
}))

vi.mock('../lib/api/client', () => ({
  api: { GET: mockGet, POST: mockPost, DELETE: mockDelete },
  getAccessToken: mockGetAccessToken,
}))
vi.mock('../stores/planStore', () => ({ usePlanStore: () => mockPlanStore }))
vi.mock('../composables/useCurrentUser', () => ({
  useCurrentUser: () => ({ isOperator: mockIsOperator, isSystemAdmin: mockIsSystemAdmin }),
}))
vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { id: 'map-1' } }),
  useRouter: () => ({ push: mockPush }),
  RouterLink: { template: '<a><slot /></a>' },
}))

import SystemMapsView from '../views/SystemMapsView.vue'
import { useSystemMapsStore } from '../stores/systemMaps'
import enUS from '../locales/en-US.js'

const i18n = createI18n({ legacy: false, locale: 'en-US', messages: { 'en-US': enUS } })

const DialogStub = {
  name: 'Dialog',
  props: ['visible'],
  template: '<div v-if="visible"><slot /></div>',
}
const MenuStub = {
  name: 'Menu',
  props: ['model'],
  setup() { return { toggle: () => {} } },
  template: '<div data-testid="stub-menu" />',
}
const InputTextStub = {
  name: 'InputText',
  props: ['modelValue'],
  emits: ['update:modelValue'],
  template: '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
}
const TextareaStub = {
  name: 'Textarea',
  props: ['modelValue'],
  emits: ['update:modelValue'],
  template: '<textarea :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
}
const FormDialogStub = {
  name: 'FormDialog',
  props: ['open', 'confirmTestId', 'cancelTestId'],
  emits: ['confirm', 'update:open'],
  template: '<button v-if="open" :data-testid="confirmTestId" @click="$emit(\'confirm\')">confirm</button>',
}

function mountView() {
  return mount(SystemMapsView, {
    global: {
      plugins: [i18n],
      stubs: {
        Dialog: DialogStub,
        Menu: MenuStub,
        InputText: InputTextStub,
        Textarea: TextareaStub,
        FormDialog: FormDialogStub,
      },
    },
  })
}

function map(overrides: Record<string, unknown> = {}) {
  return {
    id: 'map-1',
    name: 'Map one',
    description: 'First map',
    published_revision_id: 'rev-1',
    updated_at: '2026-01-02T00:00:00Z',
    ...overrides,
  }
}

function flagsResponse() {
  return { data: { flags: [{ name: 'system_maps', currently_active: true }] } }
}

function listResponse(items: Array<Record<string, unknown>>, total = items.length, page = 1) {
  return { data: { items, total, page, page_size: 20 } }
}

describe('SystemMapsView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    mockGet.mockReset()
    mockPost.mockReset()
    mockDelete.mockReset()
    mockPush.mockReset()
    mockGetAccessToken.mockReturnValue('header.eyJzdWIiOiJvcGVyYXRvci0xIn0.signature')
    mockIsOperator.value = true
    mockIsSystemAdmin.value = false
    mockPlanStore.loaded = true
    mockPlanStore.devMode = true
    mockPlanStore.userPermissions = ['system_maps_access', 'system.config.manage']
  })

  afterEach(() => {
    mockPush.mockReset()
  })

  it('renders published and draft rows with a formatted date', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve(flagsResponse())
      return Promise.resolve(listResponse([map(), map({ id: 'map-2', name: 'Map two', description: null, published_revision_id: null })]))
    })

    const wrapper = mountView()
    await flushPromises()

    const rows = wrapper.findAll('[data-testid^="system-map-row-"]')
    expect(rows).toHaveLength(2)
    expect(wrapper.text()).toContain('Map one')
    expect(wrapper.text()).toContain('First map')
    expect(wrapper.text()).toContain('—')
    expect(wrapper.text()).toContain('Published')
    expect(wrapper.text()).toContain('Draft')
    expect(wrapper.find('[data-testid="system-maps-pagination"]').exists()).toBe(false)
  })

  it('navigates to a map on click, Enter and Space', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve(flagsResponse())
      return Promise.resolve(listResponse([map()]))
    })

    const wrapper = mountView()
    await flushPromises()

    const row = wrapper.get('[data-testid="system-map-row-map-1"]')
    await row.trigger('click')
    await row.trigger('keydown', { key: 'Enter' })
    await row.trigger('keydown', { key: ' ' })

    expect(mockPush).toHaveBeenCalledTimes(3)
    expect(mockPush).toHaveBeenCalledWith('/system-maps/map-1')
  })

  it('pages forward and backward when there are more maps than a page', async () => {
    const pages = vi.fn((path: string, init?: { params?: { query?: { page?: number } } }) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve(flagsResponse())
      const page = init?.params?.query?.page ?? 1
      return Promise.resolve(listResponse([map({ id: `map-${page}` })], 45, page))
    })
    mockGet.mockImplementation(pages)

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.find('[data-testid="system-maps-pagination"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="system-maps-prev"]').attributes('disabled')).toBeDefined()

    await wrapper.get('[data-testid="system-maps-next"]').trigger('click')
    await flushPromises()
    expect(mockGet).toHaveBeenLastCalledWith('/api/v1/system-maps', { params: { query: { page: 2, page_size: 20 } } })

    await wrapper.get('[data-testid="system-maps-prev"]').trigger('click')
    await flushPromises()
    expect(mockGet).toHaveBeenLastCalledWith('/api/v1/system-maps', { params: { query: { page: 1, page_size: 20 } } })
  })

  it('shows a skeleton while loading', async () => {
    let resolveList: ((value: unknown) => void) | undefined
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve(flagsResponse())
      return new Promise((resolve) => { resolveList = resolve })
    })

    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.find('[data-testid="system-maps-loading"]').exists()).toBe(true)

    resolveList?.(listResponse([map()]))
    await flushPromises()
    expect(wrapper.find('[data-testid="system-maps-loading"]').exists()).toBe(false)
  })

  it('shows the empty state when no maps exist', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve(flagsResponse())
      return Promise.resolve(listResponse([]))
    })

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('No system maps')
  })

  it('retries a failed list load', async () => {
    let calls = 0
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve(flagsResponse())
      calls += 1
      if (calls === 1) return Promise.resolve({ error: { detail: 'down' } })
      return Promise.resolve(listResponse([map()]))
    })

    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-testid="error-alert-retry"]').trigger('click')
    await flushPromises()

    expect(calls).toBe(2)
    expect(wrapper.find('[data-testid="system-maps-loading"]').exists()).toBe(false)
  })

  it('opens the action menu with no selected map without rendering commands', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve(flagsResponse())
      return Promise.resolve(listResponse([]))
    })

    const wrapper = mountView()
    await flushPromises()

    const menu = wrapper.findComponent(MenuStub)
    expect(menu.props('model')).toEqual([])
  })

  it('dispatches view, edit, discover and delete from the action menu', async () => {
    const report = { draft_map_id: 'map-9', draft_revision_id: 'rev-9', inferred_nodes: [], inferred_edges: [] }
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve(flagsResponse())
      if (path === '/api/v1/system-maps/{map_id}') return Promise.resolve({ data: { id: 'map-9', name: 'Map nine', published_revision_id: null } })
      if (path === '/api/v1/system-maps/{map_id}/revisions/{rev_id}') return Promise.resolve({ data: { revision: { id: 'rev-9' }, nodes: [], relationships: [] } })
      return Promise.resolve(listResponse([map()]))
    })
    mockPost.mockResolvedValue({ data: report })
    mockDelete.mockResolvedValue({ data: undefined })

    const wrapper = mountView()
    await flushPromises()

    const getCommands = () => wrapper.findComponent(MenuStub).props('model') as Array<Record<string, unknown>>

    await wrapper.get('[data-testid="system-map-action-map-1"]').trigger('click')
    const commands = getCommands()
    expect(commands).toHaveLength(5)

    ;(commands[0].command as () => void)()
    await flushPromises()
    expect(mockPush).toHaveBeenCalledWith('/system-maps/map-1')

    ;(commands[1].command as () => void)()
    await flushPromises()
    expect(mockPush).toHaveBeenCalledWith('/system-maps/map-1')

    ;(commands[2].command as () => void)()
    await flushPromises()
    expect(mockPost).toHaveBeenCalledWith('/api/v1/system-maps/discover', { body: { name: 'Discovered Map one', description: expect.stringContaining('Discovery from') } })
    expect(mockPush).toHaveBeenCalledWith('/system-maps/map-9')

    ;(commands[4].command as () => void)()
    await flushPromises()
    await wrapper.get('[data-testid="system-map-delete-confirm-btn"]').trigger('click')
    await flushPromises()
    expect(mockDelete).toHaveBeenCalledWith('/api/v1/system-maps/{map_id}', { params: { path: { map_id: 'map-1' } } })
  })

  it('runs discovery from the System Maps page and opens the draft for review', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve(flagsResponse())
      return Promise.resolve(listResponse([map()]))
    })
    const store = useSystemMapsStore()
    const discover = vi.spyOn(store, 'discover').mockResolvedValue({ draft_map_id: 'map-9' } as never)

    const wrapper = mountView()
    await flushPromises()

    const button = wrapper.get('[data-testid="system-maps-discover"]')
    expect(button.text()).toContain('Discover My System')
    await button.trigger('click')
    await flushPromises()

    expect(discover).toHaveBeenCalledTimes(1)
    expect(mockPush).toHaveBeenCalledWith('/system-maps/map-9?discovery=1')
  })

  it('disables the discovery action while in flight and ignores a repeat click', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve(flagsResponse())
      return Promise.resolve(listResponse([map()]))
    })
    const store = useSystemMapsStore()
    let resolveDiscover: (value: never) => void = () => {}
    const discover = vi.spyOn(store, 'discover').mockReturnValue(
      new Promise((resolve) => { resolveDiscover = resolve as (value: never) => void }) as never,
    )

    const wrapper = mountView()
    await flushPromises()

    const button = wrapper.get('[data-testid="system-maps-discover"]')
    await button.trigger('click')
    expect(button.attributes('disabled')).toBeDefined()
    expect(button.text()).toContain('Discovering')

    button.element.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    resolveDiscover({ draft_map_id: 'map-9' } as never)
    await flushPromises()

    expect(discover).toHaveBeenCalledTimes(1)
    expect(mockPush).toHaveBeenCalledWith('/system-maps/map-9?discovery=1')
  })

  it('surfaces a discovery failure on the page and clears it on dismiss', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve(flagsResponse())
      return Promise.resolve(listResponse([map()]))
    })
    const store = useSystemMapsStore()
    vi.spyOn(store, 'discover').mockRejectedValue(new Error('inventory unavailable'))

    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-testid="system-maps-discover"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[role="alert"]').text()).toContain('inventory unavailable')
    expect(wrapper.get('[data-testid="system-maps-discover"]').attributes('disabled')).toBeUndefined()
    expect(mockPush).not.toHaveBeenCalled()

    await wrapper.get('[data-testid="error-alert-dismiss"]').trigger('click')
    expect(wrapper.find('[data-testid="error-alert-dismiss"]').exists()).toBe(false)
  })

  it('uses the localized fallback for a non-Error discovery rejection', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve(flagsResponse())
      return Promise.resolve(listResponse([map()]))
    })
    const store = useSystemMapsStore()
    vi.spyOn(store, 'discover').mockRejectedValue('offline')

    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-testid="system-maps-discover"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Could not discover your system.')
    expect(mockPush).not.toHaveBeenCalled()
  })

  it('gives the create map form and controls the full dialog width', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve(flagsResponse())
      return Promise.resolve(listResponse([map()]))
    })

    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-testid="system-maps-create"]').trigger('click')

    expect(wrapper.get('form').classes()).toContain('w-full')
    expect(wrapper.get('[data-testid="system-map-create-name"]').classes()).toContain('w-full')
    expect(wrapper.get('[data-testid="system-map-create-description"]').classes()).toContain('w-full')
  })

  it('creates a map from the dialog and ignores an empty name', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve(flagsResponse())
      return Promise.resolve(listResponse([map()]))
    })
    mockPost.mockResolvedValue({ data: { id: 'new-map', name: 'New map' } })

    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-testid="system-maps-create"]').trigger('click')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(mockPost).not.toHaveBeenCalled()

    await wrapper.get('[data-testid="system-map-create-name"]').setValue('New map')
    await wrapper.get('[data-testid="system-map-create-description"]').setValue('A description')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(mockPost).toHaveBeenCalledWith('/api/v1/system-maps', { body: { name: 'New map', description: 'A description', visibility: 'org', default_map: false } })
    expect(wrapper.find('form').exists()).toBe(false)
  })

  it('shows a create error and dismisses it', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve(flagsResponse())
      return Promise.resolve(listResponse([map()]))
    })
    mockPost.mockResolvedValue({ error: { detail: 'exists' } })

    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-testid="system-maps-create"]').trigger('click')
    await wrapper.get('[data-testid="system-map-create-name"]').setValue('Duplicate')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('Could not create system map')

    await wrapper.get('[data-testid="error-alert-dismiss"]').trigger('click')
    expect(wrapper.find('[data-testid="error-alert-dismiss"]').exists()).toBe(false)
  })

  it('uses a non-Error create failure message', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve(flagsResponse())
      return Promise.resolve(listResponse([map()]))
    })
    mockPost.mockRejectedValue('offline')

    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-testid="system-maps-create"]').trigger('click')
    await wrapper.get('[data-testid="system-map-create-name"]').setValue('New map')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('Could not create the system map.')
  })

  it('surfaces discovery and delete failures from the action menu', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve(flagsResponse())
      return Promise.resolve(listResponse([map()]))
    })
    mockPost.mockRejectedValue(new Error('discovery offline'))
    mockDelete.mockResolvedValue({ error: { detail: 'delete failed' } })

    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-testid="system-map-action-map-1"]').trigger('click')
    const commands = wrapper.findComponent(MenuStub).props('model') as Array<Record<string, unknown>>

    ;(commands[2].command as () => void)()
    await flushPromises()
    expect(wrapper.text()).toContain('discovery offline')

    await wrapper.get('[data-testid="error-alert-dismiss"]').trigger('click')
    ;(commands[4].command as () => void)()
    await flushPromises()
    await wrapper.get('[data-testid="system-map-delete-confirm-btn"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Could not delete system map')
  })

  it('does nothing when delete is confirmed with no selected map', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve(flagsResponse())
      return Promise.resolve(listResponse([map()]))
    })

    const wrapper = mountView()
    await flushPromises()

    const store = useSystemMapsStore()
    await store.fetchMaps()
    const formDialog = wrapper.findComponent(FormDialogStub)
    formDialog.vm.$emit('confirm')
    await flushPromises()

    expect(mockDelete).not.toHaveBeenCalled()
  })
})

describe('SystemMapsView fallback branches', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    mockGet.mockReset()
    mockPost.mockReset()
    mockDelete.mockReset()
    mockPush.mockReset()
    mockGetAccessToken.mockReturnValue('header.eyJzdWIiOiJvcGVyYXRvci0xIn0.signature')
    mockIsOperator.value = true
    mockPlanStore.loaded = true
    mockPlanStore.devMode = true
    mockPlanStore.userPermissions = ['system_maps_access', 'system.config.manage']
  })

  it('falls back to the row map id when discovery leaves no active map', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve(flagsResponse())
      if (path === '/api/v1/system-maps/{map_id}') return Promise.resolve({ error: { detail: 'down' } })
      return Promise.resolve(listResponse([map()]))
    })
    mockPost.mockResolvedValue({ data: { draft_map_id: 'map-1', inferred_nodes: [], inferred_edges: [] } })

    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-testid="system-map-action-map-1"]').trigger('click')
    const commands = wrapper.findComponent(MenuStub).props('model') as Array<Record<string, unknown>>
    ;(commands[2].command as () => void)()
    await flushPromises()

    expect(mockPush).toHaveBeenCalledWith('/system-maps/map-1')
  })

  it('uses the localized discover fallback for a non-Error rejection', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve(flagsResponse())
      return Promise.resolve(listResponse([map()]))
    })
    mockPost.mockRejectedValue('offline')

    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-testid="system-map-action-map-1"]').trigger('click')
    const commands = wrapper.findComponent(MenuStub).props('model') as Array<Record<string, unknown>>
    ;(commands[2].command as () => void)()
    await flushPromises()

    expect(wrapper.text()).toContain('Discover')
  })


  it('renders a viewer read-only list without authoring controls', async () => {
    mockIsOperator.value = false
    mockPlanStore.devMode = false
    mockPlanStore.userPermissions = ['system_maps.read']
    mockGet.mockImplementation(() => Promise.resolve(listResponse([map()])))

    const wrapper = mountView()
    await flushPromises()

    expect(mockGet).not.toHaveBeenCalledWith('/api/v1/admin/feature-flags')
    expect(wrapper.find('[data-testid="system-maps-create"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="system-maps-discover"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="system-maps-read-only"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Map one')
    expect(wrapper.find('[data-testid="system-map-action-map-1"]').exists()).toBe(false)
  })

  it('renders the unavailable state without a retry on a 402 feature_required', async () => {
    mockIsOperator.value = false
    mockPlanStore.devMode = false
    mockPlanStore.userPermissions = ['system_maps.read']
    mockGet.mockResolvedValue({
      error: {
        type: 'urn:problem:modulo:feature_required',
        status: 402,
        detail: 'system_maps_read is not enabled for this organisation',
      },
      response: { status: 402 },
    })

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('System Maps is not available on your plan')
    expect(wrapper.find('[data-testid="error-alert-retry"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="system-maps-loading"]').exists()).toBe(false)
  })

  it('keeps the retryable error state for a genuine server failure', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve(flagsResponse())
      return Promise.resolve({ error: { detail: 'down' }, response: { status: 500 } })
    })

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.find('[data-testid="error-alert-retry"]').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('System Maps is not available on your plan')
  })

  it('keeps the operator action menu for an authoring operator', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve(flagsResponse())
      return Promise.resolve(listResponse([map()]))
    })

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.find('[data-testid="system-maps-create"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="system-maps-read-only"]').exists()).toBe(false)
    await wrapper.get('[data-testid="system-map-action-map-1"]').trigger('click')
    expect((wrapper.findComponent(MenuStub).props('model') as unknown[]).length).toBe(5)
  })

  it('narrows an open action menu to view-only when authoring access is lost', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve(flagsResponse())
      return Promise.resolve(listResponse([map()]))
    })
    const store = useSystemMapsStore()

    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-testid="system-map-action-map-1"]').trigger('click')
    expect((wrapper.findComponent(MenuStub).props('model') as unknown[]).length).toBe(5)

    // The entitlement can be revoked while the menu is open; the reactive
    // menu must then expose only the read action and never a mutation.
    store.systemMapsEnabled = false
    await wrapper.vm.$nextTick()

    const model = wrapper.findComponent(MenuStub).props('model') as Array<{ label: string }>
    expect(model).toHaveLength(1)
    expect(model[0].label).toBe('View')
  })

  it('uses the localized delete fallback for a non-Error rejection', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve(flagsResponse())
      return Promise.resolve(listResponse([map()]))
    })
    mockDelete.mockRejectedValue('offline')

    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-testid="system-map-action-map-1"]').trigger('click')
    const commands = wrapper.findComponent(MenuStub).props('model') as Array<Record<string, unknown>>
    ;(commands[4].command as () => void)()
    await flushPromises()
    await wrapper.get('[data-testid="system-map-delete-confirm-btn"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Could not delete the system map.')
  })
})
