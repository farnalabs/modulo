import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import enUS from '../locales/en-US.js'

const mockGet = vi.hoisted(() => vi.fn())

vi.mock('../lib/api/client', () => ({
  api: { GET: mockGet },
  getAccessToken: vi.fn(),
}))

import SystemMapAttributionBindings from '../components/system-maps/SystemMapAttributionBindings.vue'

const i18n = createI18n({ legacy: false, locale: 'en-US', messages: { 'en-US': enUS } })

const nodes = [
  { node_key: 'pipeline-1', node_type: 'pipeline', display_name: 'Pipeline 1' },
  { node_key: 'loop-1', node_type: 'improvement_loop', display_name: 'Loop 1' },
  { node_key: 'agent-1', node_type: 'agent', display_name: 'Agent 1' },
]

const bindings = [
  { id: 'binding-1', binding_kind: 'trigger', node_key: 'pipeline-1', pipeline_id: 'pipe-1', trigger_id: 'trig-1' },
  { id: 'binding-2', binding_kind: 'action_pipeline', node_key: 'loop-1', pipeline_id: 'pipe-2', trigger_id: null },
]

function mountComponent(options: { bindings?: unknown[]; readOnly?: boolean; attrs?: Record<string, unknown> } = {}) {
  return mount(SystemMapAttributionBindings, {
    props: {
      nodes: nodes as never,
      bindings: (options.bindings ?? []) as never,
      readOnly: options.readOnly,
    },
    attrs: options.attrs,
    global: { plugins: [i18n] },
  })
}

function pipelinesResponse() {
  return {
    data: {
      items: [
        { id: 'pipe-1', name: 'Pipeline one' },
        { id: 'pipe-2', name: 'Pipeline two' },
      ],
    },
  }
}

describe('SystemMapAttributionBindings', () => {
  beforeEach(() => {
    mockGet.mockReset()
  })

  it('loads pipelines, shows the spinner, and filters eligible nodes by binding kind', async () => {
    let resolvePipelines: (value: unknown) => void = () => {}
    mockGet.mockReturnValue(new Promise((resolve) => { resolvePipelines = resolve }))

    const wrapper = mountComponent()
    await wrapper.vm.$nextTick()
    expect(wrapper.find('.animate-spin').exists()).toBe(true)

    resolvePipelines(pipelinesResponse())
    await flushPromises()

    expect(mockGet).toHaveBeenCalledWith('/api/v1/pipelines', { params: { query: { page_size: 100 } } })
    expect(wrapper.find('.animate-spin').exists()).toBe(false)
    expect(wrapper.find('[data-testid="system-map-binding-empty"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="system-map-binding-trigger"]').exists()).toBe(true)

    const nodeSelect = wrapper.get('[data-testid="system-map-binding-node"]')
    expect(nodeSelect.findAll('option').map((option) => option.text())).toContain('Pipeline 1')
    expect(nodeSelect.findAll('option').map((option) => option.text())).not.toContain('Loop 1')

    await wrapper.get('[data-testid="system-map-binding-kind"]').setValue('action_pipeline')

    const loopNodeSelect = wrapper.get('[data-testid="system-map-binding-node"]')
    expect(loopNodeSelect.findAll('option').map((option) => option.text())).toContain('Loop 1')
    expect(loopNodeSelect.findAll('option').map((option) => option.text())).not.toContain('Pipeline 1')
    expect(wrapper.find('[data-testid="system-map-binding-trigger"]').exists()).toBe(false)
  })

  it('loads triggers for the selected pipeline and clears them when the selection changes', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/pipelines') return Promise.resolve(pipelinesResponse())
      if (path === '/api/v1/triggers') return Promise.resolve({ data: { items: [{ id: 'trig-1', name: 'Nightly' }, { id: 'trig-2', name: null, trigger_type: 'schedule' }, { id: 'trig-3', name: '', trigger_type: null }] } })
      return Promise.resolve({ data: {} })
    })

    const wrapper = mountComponent()
    await flushPromises()

    await wrapper.get('[data-testid="system-map-binding-pipeline"]').setValue('pipe-1')
    await flushPromises()

    expect(mockGet).toHaveBeenCalledWith('/api/v1/triggers', { params: { query: { pipeline_id: 'pipe-1', page_size: 100 } } })
    const triggerOptions = wrapper.get('[data-testid="system-map-binding-trigger"]').findAll('option').map((option) => option.text())
    expect(triggerOptions).toContain('Nightly')
    expect(triggerOptions).toContain('schedule')
    expect(triggerOptions).toContain('trig-3')

    // Changing the binding kind resets the pipeline selection to an empty value.
    await wrapper.get('[data-testid="system-map-binding-kind"]').setValue('action_pipeline')
    await flushPromises()
    expect(wrapper.find('[data-testid="system-map-binding-trigger"]').exists()).toBe(false)

    // A pipeline selected while the kind is not "trigger" skips the trigger lookup.
    const callsBefore = mockGet.mock.calls.length
    await wrapper.get('[data-testid="system-map-binding-pipeline"]').setValue('pipe-1')
    await flushPromises()
    expect(mockGet.mock.calls.length).toBe(callsBefore)
  })

  it('treats a pipelines lookup without an items array as empty', async () => {
    mockGet.mockResolvedValue({ data: {} })

    const wrapper = mountComponent()
    await flushPromises()

    expect(wrapper.get('[data-testid="system-map-binding-pipeline"]').findAll('option')).toHaveLength(1)
  })

  it('treats a trigger lookup without an items array as empty', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/pipelines') return Promise.resolve(pipelinesResponse())
      if (path === '/api/v1/triggers') return Promise.resolve({ data: {} })
      return Promise.resolve({ data: {} })
    })

    const wrapper = mountComponent()
    await flushPromises()

    await wrapper.get('[data-testid="system-map-binding-pipeline"]').setValue('pipe-1')
    await flushPromises()

    expect(wrapper.get('[data-testid="system-map-binding-trigger"]').findAll('option')).toHaveLength(1)
  })

  it('submits a trigger binding, including the optional trigger id', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/pipelines') return Promise.resolve(pipelinesResponse())
      if (path === '/api/v1/triggers') return Promise.resolve({ data: { items: [{ id: 'trig-1', name: 'Nightly' }, { id: 'trig-2', name: null, trigger_type: 'schedule' }, { id: 'trig-3', name: '', trigger_type: null }] } })
      return Promise.resolve({ data: {} })
    })
    const onCreate = vi.fn()

    const wrapper = mountComponent({ attrs: { onCreate } })
    await flushPromises()

    await wrapper.get('[data-testid="system-map-binding-node"]').setValue('pipeline-1')
    await wrapper.get('[data-testid="system-map-binding-pipeline"]').setValue('pipe-1')
    await flushPromises()
    await wrapper.get('[data-testid="system-map-binding-trigger"]').setValue('trig-1')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(onCreate).toHaveBeenCalledWith({
      node_key: 'pipeline-1',
      pipeline_id: 'pipe-1',
      trigger_id: 'trig-1',
      binding_kind: 'trigger',
    })

    await wrapper.get('[data-testid="system-map-binding-trigger"]').setValue('')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(onCreate).toHaveBeenLastCalledWith({
      node_key: 'pipeline-1',
      pipeline_id: 'pipe-1',
      trigger_id: null,
      binding_kind: 'trigger',
    })
  })

  it('submits an action-pipeline binding without loading triggers', async () => {
    mockGet.mockResolvedValue(pipelinesResponse())
    const onCreate = vi.fn()

    const wrapper = mountComponent({ attrs: { onCreate } })
    await flushPromises()

    await wrapper.get('[data-testid="system-map-binding-kind"]').setValue('action_pipeline')
    await wrapper.get('[data-testid="system-map-binding-node"]').setValue('loop-1')
    await wrapper.get('[data-testid="system-map-binding-pipeline"]').setValue('pipe-2')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(mockGet).not.toHaveBeenCalledWith('/api/v1/triggers', expect.anything())
    expect(onCreate).toHaveBeenCalledWith({
      node_key: 'loop-1',
      pipeline_id: 'pipe-2',
      trigger_id: null,
      binding_kind: 'action_pipeline',
    })
  })

  it('renders existing bindings and emits a delete', async () => {
    mockGet.mockResolvedValue(pipelinesResponse())
    const onDelete = vi.fn()

    const wrapper = mountComponent({ bindings, attrs: { onDelete } })
    await flushPromises()

    expect(wrapper.get('[data-testid="system-map-binding-list"]').text()).toContain('trigger · pipeline-1 · pipe-1 · trig-1')
    expect(wrapper.get('[data-testid="system-map-binding-list"]').text()).toContain('action_pipeline · loop-1 · pipe-2')

    await wrapper.get('[data-testid="system-map-binding-delete-binding-1"]').trigger('click')
    await flushPromises()

    expect(onDelete).toHaveBeenCalledWith('binding-1')
  })

  it('hides the authoring controls when read-only', async () => {
    mockGet.mockResolvedValue(pipelinesResponse())

    const wrapper = mountComponent({ bindings, readOnly: true })
    await flushPromises()

    expect(wrapper.find('form').exists()).toBe(false)
    expect(wrapper.find('[data-testid="system-map-binding-delete-binding-1"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="system-map-binding-list"]').text()).toContain('trigger · pipeline-1 · pipe-1 · trig-1')
  })

  it('surfaces pipeline load failures and falls back for non-Error rejections', async () => {
    mockGet.mockRejectedValueOnce(new Error('pipelines unavailable'))
    const first = mountComponent()
    await flushPromises()
    expect(first.text()).toContain('pipelines unavailable')

    mockGet.mockRejectedValueOnce('offline')
    const second = mountComponent()
    await flushPromises()
    expect(second.text()).toContain('Could not load pipelines')
  })

  it('surfaces create and delete emit failures, falling back for non-Error rejections', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/pipelines') return Promise.resolve(pipelinesResponse())
      return Promise.resolve({ data: {} })
    })
    const onCreate = vi.fn(() => {
      throw new Error('create exploded')
    })
    const onDelete = vi.fn(() => {
      throw new Error('delete exploded')
    })

    const wrapper = mountComponent({ bindings, attrs: { onCreate, onDelete } })
    await flushPromises()

    await wrapper.get('[data-testid="system-map-binding-node"]').setValue('pipeline-1')
    await wrapper.get('[data-testid="system-map-binding-pipeline"]').setValue('pipe-1')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('create exploded')

    await wrapper.get('[data-testid="system-map-binding-delete-binding-1"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('delete exploded')
  })

  it('falls back to generic emit-failure messages for non-Error rejections', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/pipelines') return Promise.resolve(pipelinesResponse())
      return Promise.resolve({ data: {} })
    })
    const onCreate = vi.fn(() => {
      throw 'offline'
    })
    const onDelete = vi.fn(() => {
      throw 'offline'
    })

    const wrapper = mountComponent({ bindings, attrs: { onCreate, onDelete } })
    await flushPromises()

    await wrapper.get('[data-testid="system-map-binding-node"]').setValue('pipeline-1')
    await wrapper.get('[data-testid="system-map-binding-pipeline"]').setValue('pipe-1')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('Could not create attribution binding')

    await wrapper.get('[data-testid="system-map-binding-delete-binding-1"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Could not delete attribution binding')
  })
})
