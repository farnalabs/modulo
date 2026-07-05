import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('../../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockResolvedValue({ data: { items: [] }, error: undefined }),
    POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    PUT: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    PATCH: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    DELETE: vi.fn().mockResolvedValue({ response: { status: 204, ok: true }, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({
    get: vi.fn().mockResolvedValue({ items: [] }),
    post: vi.fn().mockResolvedValue({}),
    put: vi.fn().mockResolvedValue({}),
    patch: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue({}),
  }),
}))

vi.mock('@vue-flow/core', () => ({
  useNode: vi.fn(() => ({
    id: 'test-node',
    node: {
      data: {
        label: 'Test Composite',
        compositeRef: 'comp-1',
        compositeParameterValues: { model: 'gpt-4' },
        portCount: 5,
        totalPorts: 5,
      },
    },
  })),
}))

import CompositeNode from '../../components/pipeline/nodes/CompositeNode.vue'
import ParameterPortForm from '../../components/pipeline/composite/ParameterPortForm.vue'
import type { ParameterPort } from '../../types/pipeline'

describe('CompositeNode', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('renders without crashing', async () => {
    const wrapper = mount(CompositeNode, {
      global: {
        plugins: [createPinia()],
      },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Composite')
  })
})

describe('ParameterPortForm', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

function makePort(overrides: Partial<ParameterPort> = {}): ParameterPort {
  return {
    id: 'p-default',
    name: 'default',
    label: 'Default',
    description: null,
    type: 'string',
    required: false,
    default: null,
    options: null,
    multiline: false,
    target_injection: { mode: 'prompt_replace', node_id: '', injection_point: 'prompt_template' },
    ...overrides,
  }
}

  it('renders string port', async () => {
    const port = makePort({
      id: 'p1',
      name: 'prompt',
      label: 'Prompt',
      required: true,
      default: 'Hello',
    })
    const wrapper = mount(ParameterPortForm, {
      props: { port, modelValue: '' },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Prompt')
    expect(wrapper.text()).toContain('*')
  })

  it('renders number port', async () => {
    const port = makePort({
      id: 'p2',
      name: 'temperature',
      label: 'Temperature',
      type: 'number',
      default: 0.7,
    })
    const wrapper = mount(ParameterPortForm, {
      props: { port, modelValue: 0.7 },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Temperature')
  })

  it('renders boolean port', async () => {
    const port = makePort({
      id: 'p3',
      name: 'enabled',
      label: 'Enabled',
      type: 'boolean',
      required: true,
    })
    const wrapper = mount(ParameterPortForm, {
      props: { port, modelValue: false },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Enabled')
  })

  it('renders select port', async () => {
    const port = makePort({
      id: 'p4',
      name: 'model',
      label: 'Model',
      type: 'select',
      required: true,
      options: [
        { label: 'GPT-4', value: 'gpt-4' },
        { label: 'Claude 3', value: 'claude-3' },
      ],
    })
    const wrapper = mount(ParameterPortForm, {
      props: { port, modelValue: '' },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Model')
  })

  it('renders model_backend_ref port', async () => {
    const port = makePort({
      id: 'p5',
      name: 'backend',
      label: 'Backend',
      type: 'model_backend_ref',
    })
    const wrapper = mount(ParameterPortForm, {
      props: { port, modelValue: null },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Backend')
  })

  it('renders schema_ref port', async () => {
    const port = makePort({
      id: 'p6',
      name: 'input_schema',
      label: 'Input Schema',
      type: 'schema_ref',
    })
    const wrapper = mount(ParameterPortForm, {
      props: { port, modelValue: null },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Input Schema')
  })
})
