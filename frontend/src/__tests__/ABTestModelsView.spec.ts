import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

vi.mock('vue-i18n', () => ({
  useI18n: () => ({ t: (key: string) => key }),
}))

const routeQuery: Record<string, string> = {}
const pushMock = vi.hoisted(() => vi.fn())

vi.mock('vue-router', () => ({
  useRoute: () => ({ query: routeQuery, path: '/variants/ab-test', name: 'ab-test-models', params: {}, fullPath: '/variants/ab-test' }),
  useRouter: () => ({ push: pushMock }),
}))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    PUT: vi.fn().mockResolvedValue({ data: null, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import { api } from '../lib/api/client'
import ABTestModelsView from '../views/ABTestModelsView.vue'

const pipelines = [
  { id: 'p1', name: 'Pipe One' },
  { id: 'p2', name: 'Pipe Two' },
]
const backends = [
  { id: 'mb1', display_name: 'MB1', provider: 'openai' },
  { id: 'mb2', display_name: 'MB2', provider: 'anthropic' },
]
const snapshots = [
  { id: 's1', snapshot_version: 1, tag: null },
  { id: 's2', snapshot_version: 2, tag: 'prod' },
]

function mockGet(url: string) {
  if (url === '/api/v1/pipelines') {
    return Promise.resolve({ data: { items: pipelines, total: pipelines.length, page: 1, page_size: 50 }, error: undefined })
  }
  if (url === '/api/v1/model-backends') {
    return Promise.resolve({ data: { items: backends, total: backends.length, page: 1, page_size: 50 }, error: undefined })
  }
  if (url === '/api/v1/pipelines/{pipeline_id}/snapshots') {
    return Promise.resolve({ data: { items: snapshots, total: snapshots.length }, error: undefined })
  }
  if (url === '/api/v1/pipelines/{pipeline_id}/graph') {
    return Promise.resolve({ data: { nodes: [{ id: 'n1', agent_id: 'a1' }], edges: [] }, error: undefined })
  }
  if (url === '/api/v1/agents/{agent_id}/prompts') {
    return Promise.resolve({ data: [{ version: 'v3' }, { version: 'v4' }], error: undefined })
  }
  return Promise.resolve({ data: null, error: undefined })
}

async function mountView() {
  vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation(mockGet)
  const wrapper = mount(ABTestModelsView)
  await nextTick()
  await new Promise(r => setTimeout(r, 0))
  return wrapper
}

describe('ABTestModelsView variant comparison creator', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    pushMock.mockClear()
    delete routeQuery.pipeline_id
  })

  it('renders without crashing and shows the pipeline picker', async () => {
    const wrapper = await mountView()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.find('[data-testid="variant-builder-pipeline-select"]').exists()).toBe(true)
  })

  it('adds rows with stable ids and defaults the snapshot to the pipeline snapshot', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="variant-builder-add"]').trigger('click')
    await wrapper.find('[data-testid="variant-builder-add"]').trigger('click')
    await nextTick()

    expect(wrapper.findAll('[data-testid^="variant-builder-label-"]')).toHaveLength(2)
    const firstRowIds = (wrapper.vm as unknown as { variants: Array<{ id: string }> }).variants.map(v => v.id)
    expect(new Set(firstRowIds).size).toBe(2)
    const variants = (wrapper.vm as unknown as { variants: Array<{ snapshotId: string | null }> }).variants
    expect(variants.every(v => v.snapshotId === 's1')).toBe(true)
  })

  it('duplicate mints a fresh variant id and auto-suffixes the label with "(copy)"', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="variant-builder-add"]').trigger('click')
    await nextTick()
    const vm = wrapper.vm as unknown as { variants: Array<{ id: string; label: string; snapshotId: string | null }> }
    const originalId = vm.variants[0].id
    const originalLabel = vm.variants[0].label

    await wrapper.find('[data-testid="variant-builder-duplicate-0"]').trigger('click')
    await nextTick()

    expect(vm.variants).toHaveLength(2)
    expect(vm.variants[1].id).not.toBe(originalId)
    expect(vm.variants[1].label).toBe(`${originalLabel} (copy)`)
    expect(vm.variants[1].snapshotId).toBe(vm.variants[0].snapshotId)
  })

  it('remove row drops it and min-2 gate re-checks after a drop', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="variant-builder-add"]').trigger('click')
    await wrapper.find('[data-testid="variant-builder-add"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="variant-builder-remove-1"]').trigger('click')
    await nextTick()

    const vm = wrapper.vm as unknown as { variants: unknown[]; canFire: boolean }
    expect(vm.variants).toHaveLength(1)
    expect(vm.canFire).toBe(false)
    expect(wrapper.find('[data-testid="variant-builder-min-two"]').exists()).toBe(true)
  })

  it('fire button is disabled below 2 variants', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="variant-builder-add"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="variant-builder-fire"]').attributes('disabled')).toBeDefined()
  })

  it('row cap of 10 disables the add button at the headroom limit', async () => {
    const wrapper = await mountView()
    const vm = wrapper.vm as unknown as { variants: unknown[] }
    for (let i = 0; i < 10; i += 1) {
      await wrapper.find('[data-testid="variant-builder-add"]').trigger('click')
    }
    await nextTick()
    expect(vm.variants).toHaveLength(10)
    expect(wrapper.find('[data-testid="variant-builder-add"]').attributes('disabled')).toBeDefined()
  })

  it('duplicate respects the row cap of 10 and disables at the limit', async () => {
    const wrapper = await mountView()
    const vm = wrapper.vm as unknown as { variants: unknown[] }
    for (let i = 0; i < 10; i += 1) {
      await wrapper.find('[data-testid="variant-builder-add"]').trigger('click')
    }
    await nextTick()

    await wrapper.find('[data-testid="variant-builder-duplicate-0"]').trigger('click')
    await nextTick()

    expect(vm.variants).toHaveLength(10)
    expect(wrapper.find('[data-testid="variant-builder-duplicate-0"]').attributes('disabled')).toBeDefined()
  })

  it('first-class pickers translate into run_context_overrides keys on fire', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="variant-builder-add"]').trigger('click')
    await wrapper.find('[data-testid="variant-builder-add"]').trigger('click')
    await nextTick()

    const vm = wrapper.vm as unknown as { variants: Array<{ modelBackendId: string | null; promptVersion: string | null }> }
    vm.variants[0].modelBackendId = 'mb1'
    vm.variants[0].promptVersion = 'v3'
    vm.variants[1].modelBackendId = 'mb2'
    await nextTick()

    vi.mocked(api.POST as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) => {
      if (url === '/api/v1/variant-groups') {
        return Promise.resolve({ data: { id: 'g1' }, error: undefined })
      }
      if (url === '/api/v1/variant-groups/{group_id}/batch-run') {
        return Promise.resolve({ data: { runs: [], count: 2 }, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    await wrapper.find('[data-testid="variant-builder-fire"]').trigger('click')
    await nextTick()
    const vmDialog = wrapper.vm as unknown as { showFireDialog: boolean }
    expect(vmDialog.showFireDialog).toBe(true)
    const confirmBtn = document.body.querySelector('[data-testid="variant-builder-confirm-fire"]') as HTMLElement
    confirmBtn.click()
    await nextTick()
    await new Promise(r => setTimeout(r, 0))

    const calls = (api.POST as unknown as ReturnType<typeof vi.fn>).mock.calls as Array<[string, { body: { variants: Array<{ run_context_overrides: Record<string, unknown> }> } }]>
    const createCall = calls.find(c => c[0] === '/api/v1/variant-groups')
    expect(createCall).toBeDefined()
    const variants = createCall![1].body.variants
    expect(variants[0].run_context_overrides).toEqual({ model_backend_id: 'mb1', prompt_version: 'v3' })
    expect(variants[1].run_context_overrides).toEqual({ model_backend_id: 'mb2' })
    expect(Object.keys(variants[0].run_context_overrides)).not.toContain('unknown_key')
  })

  it('fires the batch and navigates to the compare detail route with the group id', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="variant-builder-add"]').trigger('click')
    await wrapper.find('[data-testid="variant-builder-add"]').trigger('click')
    await nextTick()

    vi.mocked(api.POST as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) => {
      if (url === '/api/v1/variant-groups') {
        return Promise.resolve({ data: { id: 'g-batch' }, error: undefined })
      }
      if (url === '/api/v1/variant-groups/{group_id}/batch-run') {
        return Promise.resolve({ data: { runs: [{ run_id: 'r1' }, { run_id: 'r2' }], count: 2 }, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })

    await wrapper.find('[data-testid="variant-builder-fire"]').trigger('click')
    await nextTick()
    const confirmBtn = document.body.querySelector('[data-testid="variant-builder-confirm-fire"]') as HTMLElement
    confirmBtn.click()
    await nextTick()
    await new Promise(r => setTimeout(r, 0))

    expect(pushMock).toHaveBeenCalledWith({
      name: 'variant-compare-detail',
      params: { batchId: 'g-batch' },
      state: { firedRuns: [{ run_id: 'r1' }, { run_id: 'r2' }] },
    })
  })

  it('pre-selects the pipeline from the pipeline_id deep-link query', async () => {
    routeQuery.pipeline_id = 'p2'
    const wrapper = await mountView()
    await new Promise(r => setTimeout(r, 0))
    const vm = wrapper.vm as unknown as { selectedPipelineId: string }
    expect(vm.selectedPipelineId).toBe('p2')
  })
})
