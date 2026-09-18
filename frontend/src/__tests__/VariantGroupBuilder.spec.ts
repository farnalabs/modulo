import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

vi.mock('vue-i18n', () => ({
  useI18n: () => ({ t: (key: string) => key }),
}))

const pushMock = vi.hoisted(() => vi.fn())

vi.mock('vue-router', () => ({
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
import VariantGroupBuilder from '../components/variants/VariantGroupBuilder.vue'

type BuilderVm = {
  variants: Array<{
    id: string
    label: string
    snapshotId: string | null
    modelBackendId: string | null
    promptVersion: string | null
  }>
  showFireDialog: boolean
  fireError: string | null
  selectedPipelineId: string
  fireBatch: () => Promise<void>
}

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

async function mountBuilder(initialPipelineId?: string) {
  vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation(mockGet)
  const wrapper = mount(VariantGroupBuilder, {
    props: initialPipelineId ? { initialPipelineId } : {},
  })
  await nextTick()
  await new Promise(r => setTimeout(r, 0))
  return wrapper
}

describe('VariantGroupBuilder', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    pushMock.mockClear()
  })

  it('renders without crashing and shows the pipeline picker', async () => {
    const wrapper = await mountBuilder()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.find('[data-testid="variant-builder-pipeline-select"]').exists()).toBe(true)
  })

  it('adds rows with stable ids and defaults the snapshot to the pipeline snapshot', async () => {
    const wrapper = await mountBuilder()
    await wrapper.find('[data-testid="variant-builder-add"]').trigger('click')
    await wrapper.find('[data-testid="variant-builder-add"]').trigger('click')
    await nextTick()

    expect(wrapper.findAll('[data-testid^="variant-builder-label-"]')).toHaveLength(2)
    const variants = (wrapper.vm as unknown as { variants: Array<{ id: string; snapshotId: string | null }> }).variants
    expect(new Set(variants.map(v => v.id)).size).toBe(2)
    expect(variants.every(v => v.snapshotId === 's1')).toBe(true)
  })

  it('duplicate mints a fresh variant id and auto-suffixes the label with "(copy)"', async () => {
    const wrapper = await mountBuilder()
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
    const wrapper = await mountBuilder()
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
    const wrapper = await mountBuilder()
    await wrapper.find('[data-testid="variant-builder-add"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="variant-builder-fire"]').attributes('disabled')).toBeDefined()
  })

  it('row cap of 10 disables the add button at the headroom limit', async () => {
    const wrapper = await mountBuilder()
    const vm = wrapper.vm as unknown as { variants: unknown[] }
    for (let i = 0; i < 10; i += 1) {
      await wrapper.find('[data-testid="variant-builder-add"]').trigger('click')
    }
    await nextTick()
    expect(vm.variants).toHaveLength(10)
    expect(wrapper.find('[data-testid="variant-builder-add"]').attributes('disabled')).toBeDefined()
  })

  it('duplicate respects the row cap of 10 and disables at the limit', async () => {
    const wrapper = await mountBuilder()
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
    const wrapper = await mountBuilder()
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
    const wrapper = await mountBuilder()
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

  it('pre-selects the pipeline passed as the initialPipelineId prop', async () => {
    const wrapper = await mountBuilder('p2')
    await new Promise(r => setTimeout(r, 0))
    const vm = wrapper.vm as unknown as { selectedPipelineId: string }
    expect(vm.selectedPipelineId).toBe('p2')
  })

  it('emits cancel when the builder cancel button is clicked', async () => {
    const wrapper = await mountBuilder()
    await wrapper.find('[data-testid="variant-builder-cancel-build"]').trigger('click')
    expect(wrapper.emitted('cancel')).toHaveLength(1)
  })

  it('flags a duplicate label with a row error', async () => {
    const wrapper = await mountBuilder()
    await wrapper.find('[data-testid="variant-builder-add"]').trigger('click')
    await wrapper.find('[data-testid="variant-builder-add"]').trigger('click')
    await nextTick()

    const vm = wrapper.vm as unknown as BuilderVm
    vm.variants[1].label = vm.variants[0].label
    await nextTick()

    const alerts = wrapper.findAll('[role="alert"]')
    expect(alerts).toHaveLength(2)
    expect(alerts[0].text()).toBe('views.variantCreator.error_duplicate_label')
  })

  it('auto-suffixes a duplicate label past an existing copy', async () => {
    const wrapper = await mountBuilder()
    await wrapper.find('[data-testid="variant-builder-add"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="variant-builder-duplicate-0"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="variant-builder-duplicate-0"]').trigger('click')
    await nextTick()

    const vm = wrapper.vm as unknown as BuilderVm
    expect(vm.variants.map(v => v.label)).toEqual([
      'views.variantCreator.variant_prefix 1',
      'views.variantCreator.variant_prefix 1 (copy)',
      'views.variantCreator.variant_prefix 1 (copy) (2)',
    ])
  })

  it('closes the fire dialog when a variant is invalid at fire time', async () => {
    const wrapper = await mountBuilder()
    await wrapper.find('[data-testid="variant-builder-add"]').trigger('click')
    await wrapper.find('[data-testid="variant-builder-add"]').trigger('click')
    await nextTick()

    const vm = wrapper.vm as unknown as BuilderVm
    vm.showFireDialog = true
    vm.variants[0].label = ''
    await nextTick()

    await vm.fireBatch()
    expect(vm.showFireDialog).toBe(false)
  })

  it('refuses to fire with fewer than two variants', async () => {
    const wrapper = await mountBuilder()
    await wrapper.find('[data-testid="variant-builder-add"]').trigger('click')
    await nextTick()

    const vm = wrapper.vm as unknown as BuilderVm
    vm.showFireDialog = true
    await nextTick()

    await vm.fireBatch()
    expect(vm.fireError).toBe('views.variantCreator.min_two_hint')
    expect(vm.showFireDialog).toBe(false)
  })

  async function mountTwoValidVariants() {
    const wrapper = await mountBuilder()
    await wrapper.find('[data-testid="variant-builder-add"]').trigger('click')
    await wrapper.find('[data-testid="variant-builder-add"]').trigger('click')
    await nextTick()
    return wrapper
  }

  it('surfaces a create-group error from the API', async () => {
    const wrapper = await mountTwoValidVariants()
    vi.mocked(api.POST as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) =>
      Promise.resolve(
        url === '/api/v1/variant-groups'
          ? { data: null, error: { detail: 'boom' } }
          : { data: null, error: undefined }
      )
    )

    const vm = wrapper.vm as unknown as BuilderVm
    await vm.fireBatch()
    expect(vm.fireError).toContain('views.variantCreator.failed_to_create_group')
  })

  it('surfaces an unexpected error when the create-group response is empty', async () => {
    const wrapper = await mountTwoValidVariants()
    vi.mocked(api.POST as unknown as (url: string) => Promise<unknown>).mockImplementation(() =>
      Promise.resolve({ data: null, error: undefined })
    )

    const vm = wrapper.vm as unknown as BuilderVm
    await vm.fireBatch()
    expect(vm.fireError).toBe('views.variantCreator.error_unexpected')
  })

  it('surfaces a batch-run error from the API', async () => {
    const wrapper = await mountTwoValidVariants()
    vi.mocked(api.POST as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) =>
      Promise.resolve(
        url === '/api/v1/variant-groups'
          ? { data: { id: 'g1' }, error: undefined }
          : { data: null, error: { detail: 'nope' } }
      )
    )

    const vm = wrapper.vm as unknown as BuilderVm
    await vm.fireBatch()
    expect(vm.fireError).toContain('views.variantCreator.failed_to_run')
    expect(pushMock).not.toHaveBeenCalled()
  })

  it('surfaces an unexpected error when the batch-run response is empty', async () => {
    const wrapper = await mountTwoValidVariants()
    vi.mocked(api.POST as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) =>
      Promise.resolve(
        url === '/api/v1/variant-groups'
          ? { data: { id: 'g2' }, error: undefined }
          : { data: null, error: undefined }
      )
    )

    const vm = wrapper.vm as unknown as BuilderVm
    await vm.fireBatch()
    expect(vm.fireError).toBe('views.variantCreator.error_unexpected')
    expect(pushMock).not.toHaveBeenCalled()
  })

  it('catches a thrown error while firing and reports it', async () => {
    const wrapper = await mountTwoValidVariants()
    vi.mocked(api.POST as unknown as (url: string) => Promise<unknown>).mockImplementation(() =>
      Promise.reject(new Error('network down'))
    )

    const vm = wrapper.vm as unknown as BuilderVm
    await vm.fireBatch()
    expect(vm.fireError).toContain('views.variantCreator.failed_to_run')
  })

  it('keeps going when the snapshot fetch fails', async () => {
    vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) =>
      url === '/api/v1/pipelines/{pipeline_id}/snapshots'
        ? Promise.reject(new Error('snapshots down'))
        : mockGet(url)
    )
    const wrapper = mount(VariantGroupBuilder, {})
    await nextTick()
    await new Promise(r => setTimeout(r, 0))

    const vm = wrapper.vm as unknown as BuilderVm
    expect(vm.selectedPipelineId).toBe('p1')
    expect(vm.variants).toEqual([])
  })

  it('keeps going when the prompt-version fetch fails', async () => {
    vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation((url: string) =>
      url === '/api/v1/pipelines/{pipeline_id}/graph'
        ? Promise.reject(new Error('graph down'))
        : mockGet(url)
    )
    const wrapper = mount(VariantGroupBuilder, {})
    await nextTick()
    await new Promise(r => setTimeout(r, 0))

    const vm = wrapper.vm as unknown as BuilderVm
    expect(vm.selectedPipelineId).toBe('p1')
  })
})
