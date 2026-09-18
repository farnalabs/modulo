import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick as vueNextTick } from 'vue'

async function nextTick() { await vueNextTick(); await flushPromises() }

const { mockGet, mockPost } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPost: vi.fn(),
}))

vi.mock('../composables/useApi', () => ({
  useApi: () => ({
    get: mockGet,
    post: mockPost,
    put: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  }),
}))

import AdminHousekeepingView from '../views/AdminHousekeepingView.vue'

const candidate = (id: string, name: string, over: Record<string, unknown> = {}) => ({
  id,
  name,
  detail: `detail-for-${id}`,
  created_at: '2026-08-01T00:00:00Z',
  entity_type: 'pipeline',
  ...over,
})

const category = (category: string, items: unknown[], over: Record<string, unknown> = {}) => ({
  category,
  label: `${category} label`,
  description: `${category} description`,
  candidates: items,
  count: items.length,
  ...over,
})

const scanPayload = (cats: Array<{ count: number }>) => ({
  categories: cats,
  total_count: cats.reduce((acc, c) => acc + c.count, 0),
})

function mountView() {
  return mount(AdminHousekeepingView, {
    global: {
      stubs: {
        FeatureGate: { template: '<div><slot /></div>' },
        Dialog: {
          props: ['visible', 'modal'],
          template: '<div v-if="visible" data-testid="hk-confirm-dialog"><slot name="header" /><slot /><slot name="footer" /></div>',
        },
      },
    },
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  mockGet.mockResolvedValue(scanPayload([]))
  mockPost.mockResolvedValue({ deleted_count: 0, errors: [] })
})

describe('AdminHousekeepingView — scan', () => {
  it('shows the scanning state before the scan resolves', async () => {
    mockGet.mockReturnValue(new Promise(() => {}))
    const wrapper = mountView()
    await vueNextTick()
    expect(wrapper.text()).toContain('Scanning')
    expect(wrapper.find('[data-testid="hk-error"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="hk-category"]').exists()).toBe(false)
  })

  it('renders categories and candidates after a successful scan', async () => {
    mockGet.mockResolvedValue(scanPayload([
      category('stale_pipelines', [candidate('p-1', 'Old Pipeline'), candidate('p-2', 'Older Pipeline')]),
      category('orphan_connectors', [candidate('c-1', 'Dead Connector', { entity_type: 'connector' })]),
    ]))
    const wrapper = mountView()
    await nextTick()

    expect(wrapper.find('[data-testid="hk-category"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('stale_pipelines label')
    expect(wrapper.text()).toContain('Old Pipeline')
    expect(wrapper.text()).toContain('detail-for-p-1')
    expect(wrapper.text()).toContain('Dead Connector')
    expect(wrapper.text()).toContain('orphan_connectors')
    expect(wrapper.text()).toContain('2 items')
    expect(wrapper.text()).toContain('1 item')
    expect(wrapper.text()).toContain('3 candidates')
    expect(wrapper.text()).toContain('0 of 3 selected')
  })

  it('shows the All Clean empty state when there are no candidates (fe-003)', async () => {
    const wrapper = mountView()
    await nextTick()
    expect(wrapper.find('[data-testid="hk-empty"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('All Clean!')
  })

  it('surfaces a scan failure with retry and recovers on retry (fe-002)', async () => {
    mockGet.mockRejectedValue(new Error('scan blew up'))
    const wrapper = mountView()
    await nextTick()
    expect(wrapper.find('[data-testid="hk-error"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('scan blew up')

    mockGet.mockResolvedValue(scanPayload([category('stale_pipelines', [candidate('p-1', 'Recovered')])]))
    await wrapper.find('[data-testid="hk-retry"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="hk-error"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('Recovered')
  })

  it('renders the refresh button and re-scans on refresh click', async () => {
    const wrapper = mountView()
    await nextTick()
    expect(wrapper.find('[data-testid="hk-refresh"]').exists()).toBe(true)
    const callsBefore = mockGet.mock.calls.length
    await wrapper.find('[data-testid="hk-refresh"]').trigger('click')
    await nextTick()
    expect(mockGet.mock.calls.length).toBeGreaterThan(callsBefore)
  })

  it('echoes the raw created_at string when it cannot be parsed as a date', async () => {
    mockGet.mockResolvedValue(scanPayload([
      category('stale_pipelines', [candidate('p-1', 'Weird Date', { created_at: 'not-a-date' })]),
    ]))
    const wrapper = mountView()
    await nextTick()
    expect(wrapper.text()).toContain('not-a-date')
  })
})

describe('AdminHousekeepingView — selection', () => {
  const twoCategories = () => ([
    category('stale_pipelines', [candidate('p-1', 'Pipe A'), candidate('p-2', 'Pipe B')]),
    category('orphan_connectors', [candidate('c-1', 'Conn C', { entity_type: 'connector' })]),
  ])

  it('excludes checkpoint_retention from the generic selection flow and shows its reclaimable count', async () => {
    // total_count mirrors the backend contract: it covers the selectable
    // (non-checkpoint) candidates; the checkpoint cat only carries a count.
    mockGet.mockResolvedValue({
      categories: [
        category('stale_pipelines', [candidate('p-1', 'Pipe A')]),
        category('checkpoint_retention', [], { count: 5 }),
      ],
      total_count: 1,
    })
    const wrapper = mountView()
    await nextTick()

    expect(wrapper.find('[data-testid="hk-checkpoint-retention"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('5')
    expect(wrapper.text()).toContain('reclaimable')
    // Only the non-checkpoint candidate counts towards generic selection.
    expect(wrapper.text()).toContain('1 candidates')
    expect(wrapper.text()).toContain('0 of 1 selected')
  })

  it('shows 0 reclaimable when there is no checkpoint category', async () => {
    mockGet.mockResolvedValue(scanPayload([category('stale_pipelines', [candidate('p-1', 'Pipe A')])]))
    const wrapper = mountView()
    await nextTick()
    expect(wrapper.text()).toContain('0')
  })

  it('select all selects every non-checkpoint candidate and reveals Delete Selected', async () => {
    mockGet.mockResolvedValue(scanPayload(twoCategories()))
    const wrapper = mountView()
    await nextTick()

    expect(wrapper.find('[data-testid="hk-delete-selected"]').exists()).toBe(false)
    await wrapper.find('[data-testid="hk-select-all"]').trigger('change')
    await nextTick()

    expect(wrapper.text()).toContain('3 of 3 selected')
    const deleteBtn = wrapper.find('[data-testid="hk-delete-selected"]')
    expect(deleteBtn.exists()).toBe(true)
    expect(deleteBtn.text()).toContain('Delete 3 Selected')
    for (const cb of wrapper.find('[data-testid="hk-candidate-checkbox"]').element.parentElement!.parentElement!.querySelectorAll('input')) {
      expect((cb as HTMLInputElement).checked).toBe(true)
    }
  })

  it('select all deselects everything when everything is already selected', async () => {
    mockGet.mockResolvedValue(scanPayload(twoCategories()))
    const wrapper = mountView()
    await nextTick()

    await wrapper.find('[data-testid="hk-select-all"]').trigger('change')
    await nextTick()
    await wrapper.find('[data-testid="hk-select-all"]').trigger('change')
    await nextTick()
    expect(wrapper.text()).toContain('0 of 3 selected')
    expect(wrapper.find('[data-testid="hk-delete-selected"]').exists()).toBe(false)
  })

  it('category checkbox selects only that category and leaves select-all indeterminate', async () => {
    mockGet.mockResolvedValue(scanPayload(twoCategories()))
    const wrapper = mountView()
    await nextTick()

    const categoryBoxes = wrapper.findAll('[data-testid="hk-category-checkbox"]')
    await categoryBoxes[0].trigger('change')
    await nextTick()

    expect(wrapper.text()).toContain('2 of 3 selected')
    const selectAll = wrapper.find('[data-testid="hk-select-all"]').element as HTMLInputElement
    expect(selectAll.indeterminate).toBe(true)
    expect(selectAll.checked).toBe(false)
  })

  it('individual candidate toggles update the count both ways', async () => {
    mockGet.mockResolvedValue(scanPayload(twoCategories()))
    const wrapper = mountView()
    await nextTick()

    const boxes = wrapper.findAll('[data-testid="hk-candidate-checkbox"]')
    expect(boxes).toHaveLength(3)
    await boxes[0].setValue(true)
    await nextTick()
    expect(wrapper.text()).toContain('1 of 3 selected')

    await boxes[0].setValue(false)
    await nextTick()
    expect(wrapper.text()).toContain('0 of 3 selected')
  })
})

describe('AdminHousekeepingView — cleanup', () => {
  async function openConfirm(wrapper: ReturnType<typeof mount>) {
    await wrapper.find('[data-testid="hk-select-all"]').trigger('change')
    await nextTick()
    await wrapper.find('[data-testid="hk-delete-selected"]').trigger('click')
    await nextTick()
  }

  it('opens a confirm dialog grouped by entity type and posts the selected items on confirm', async () => {
    mockGet.mockResolvedValue(scanPayload([
      category('stale_pipelines', [candidate('p-1', 'Pipe A'), candidate('p-2', 'Pipe B')]),
      category('orphan_connectors', [candidate('c-1', 'Conn C', { entity_type: 'connector' })]),
    ]))
    const wrapper = mountView()
    await nextTick()
    await openConfirm(wrapper)

    const dialog = wrapper.find('[data-testid="hk-confirm-dialog"]')
    expect(dialog.exists()).toBe(true)
    expect(dialog.text()).toContain('Confirm Cleanup')
    expect(dialog.text()).toContain('cannot be undone')
    expect(dialog.text()).toContain('pipeline (2)')
    expect(dialog.text()).toContain('connector (1)')

    await wrapper.find('[data-testid="hk-confirm-cleanup"]').trigger('click')
    await nextTick()

    expect(mockPost).toHaveBeenCalledTimes(1)
    const [path, body] = mockPost.mock.calls[0]
    expect(path).toBe('/api/v1/admin/housekeeping/cleanup')
    expect(body.items).toEqual([
      { id: 'p-1', entity_type: 'pipeline' },
      { id: 'p-2', entity_type: 'pipeline' },
      { id: 'c-1', entity_type: 'connector' },
    ])
    expect(wrapper.find('[data-testid="hk-confirm-dialog"]').exists()).toBe(false)
  })

  it('falls back to the category as entity_type when the candidate has none', async () => {
    mockGet.mockResolvedValue(scanPayload([
      category('stale_pipelines', [candidate('p-1', 'Pipe A', { entity_type: '' })]),
    ]))
    const wrapper = mountView()
    await nextTick()
    await openConfirm(wrapper)

    expect(wrapper.find('[data-testid="hk-confirm-dialog"]').text()).toContain('stale_pipelines (1)')
    await wrapper.find('[data-testid="hk-confirm-cleanup"]').trigger('click')
    await nextTick()
    expect(mockPost.mock.calls[0][1].items).toEqual([{ id: 'p-1', entity_type: 'stale_pipelines' }])
  })

  it('cancel closes the confirm dialog without posting', async () => {
    mockGet.mockResolvedValue(scanPayload([category('stale_pipelines', [candidate('p-1', 'Pipe A')])]))
    const wrapper = mountView()
    await nextTick()
    await openConfirm(wrapper)

    await wrapper.find('[data-testid="hk-cancel-cleanup"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="hk-confirm-dialog"]').exists()).toBe(false)
    expect(mockPost).not.toHaveBeenCalled()
  })

  it('cleanup failure surfaces the error message', async () => {
    mockGet.mockResolvedValue(scanPayload([category('stale_pipelines', [candidate('p-1', 'Pipe A')])]))
    mockPost.mockRejectedValue(new Error('cleanup boom'))
    const wrapper = mountView()
    await nextTick()
    await openConfirm(wrapper)

    await wrapper.find('[data-testid="hk-confirm-cleanup"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="hk-error"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('cleanup boom')
  })
})

describe('AdminHousekeepingView — checkpoint retention', () => {
  const checkpointPayload = () => ({ checkpoints_purged: 2, threads_purged: 1, bytes_freed: 1024 })

  it('renders the checkpoint retention section with the default max age', async () => {
    mockGet.mockResolvedValue(scanPayload([category('checkpoint_retention', [], { count: 7 })]))
    const wrapper = mountView()
    await nextTick()

    expect(wrapper.text()).toContain('Checkpoint Retention')
    expect(wrapper.text()).toContain('Purge terminal runs older than')
    const ageInput = wrapper.find('[data-testid="hk-ckpt-max-age"]').element as HTMLInputElement
    expect(ageInput.value).toBe('3')
    expect(wrapper.find('[data-testid="hk-ckpt-purge"]').exists()).toBe(true)
  })

  it('purge requires a two-step confirm and posts max_age_days with confirm flag', async () => {
    mockGet.mockResolvedValue(scanPayload([category('checkpoint_retention', [], { count: 7 })]))
    mockPost.mockResolvedValue(checkpointPayload())
    const wrapper = mountView()
    await nextTick()

    // No prompt before the first click.
    expect(wrapper.find('[data-testid="hk-ckpt-purge-confirm"]').exists()).toBe(false)
    await wrapper.find('[data-testid="hk-ckpt-purge"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Confirm purge of checkpoints older than')

    await wrapper.find('[data-testid="hk-ckpt-purge-confirm"]').trigger('click')
    await nextTick()

    expect(mockPost).toHaveBeenCalledTimes(1)
    const [path, body] = mockPost.mock.calls[0]
    expect(path).toBe('/api/v1/admin/housekeeping/checkpoints/purge')
    expect(body).toEqual({ max_age_days: 3, confirm: true })

    // Result banner + prompt dismissed.
    const result = wrapper.find('[data-testid="hk-ckpt-result"]')
    expect(result.exists()).toBe(true)
    expect(result.text()).toContain('Purged')
    expect(result.text()).toContain('2 checkpoint row')
    expect(result.text()).toContain('from 1 run')
    expect(result.text()).toContain('freed 1.0 KB')
    expect(wrapper.find('[data-testid="hk-ckpt-purge-confirm"]').exists()).toBe(false)
  })

  it('a custom max age is sent in the purge body', async () => {
    mockGet.mockResolvedValue(scanPayload([category('checkpoint_retention', [], { count: 7 })]))
    mockPost.mockResolvedValue(checkpointPayload())
    const wrapper = mountView()
    await nextTick()

    await wrapper.find('[data-testid="hk-ckpt-max-age"]').setValue('14')
    await wrapper.find('[data-testid="hk-ckpt-purge"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="hk-ckpt-purge-confirm"]').trigger('click')
    await nextTick()
    expect(mockPost.mock.calls[0][1]).toEqual({ max_age_days: 14, confirm: true })
  })

  it('cancel hides the confirm prompt without posting', async () => {
    mockGet.mockResolvedValue(scanPayload([category('checkpoint_retention', [], { count: 7 })]))
    const wrapper = mountView()
    await nextTick()

    await wrapper.find('[data-testid="hk-ckpt-purge"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="hk-ckpt-purge-cancel"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="hk-ckpt-purge-confirm"]').exists()).toBe(false)
    expect(mockPost).not.toHaveBeenCalled()
  })

  it('purge failure shows the inline error', async () => {
    mockGet.mockResolvedValue(scanPayload([category('checkpoint_retention', [], { count: 7 })]))
    mockPost.mockRejectedValue(new Error('purge boom'))
    const wrapper = mountView()
    await nextTick()

    await wrapper.find('[data-testid="hk-ckpt-purge"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="hk-ckpt-purge-confirm"]').trigger('click')
    await nextTick()

    expect(wrapper.find('[data-testid="hk-ckpt-error"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('purge boom')
    expect(wrapper.find('[data-testid="hk-ckpt-result"]').exists()).toBe(false)
  })

  it('disables the purge button when the max age is below 1', async () => {
    mockGet.mockResolvedValue(scanPayload([category('checkpoint_retention', [], { count: 7 })]))
    const wrapper = mountView()
    await nextTick()

    await wrapper.find('[data-testid="hk-ckpt-max-age"]').setValue('0')
    await nextTick()
    const purge = wrapper.find('[data-testid="hk-ckpt-purge"]')
    expect(purge.attributes('disabled')).toBeDefined()
  })

  it('formats larger byte counts with units', async () => {
    mockGet.mockResolvedValue(scanPayload([category('checkpoint_retention', [], { count: 7 })]))
    mockPost.mockResolvedValue({ checkpoints_purged: 1, threads_purged: 1, bytes_freed: 5 * 1024 * 1024 })
    const wrapper = mountView()
    await nextTick()

    await wrapper.find('[data-testid="hk-ckpt-purge"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="hk-ckpt-purge-confirm"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="hk-ckpt-result"]').text()).toContain('5.0 MB')
  })
})
