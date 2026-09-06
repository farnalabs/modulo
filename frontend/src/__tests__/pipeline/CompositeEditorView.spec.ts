import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const { getMock, postMock, getAccessTokenMock, routerPush } = vi.hoisted(() => ({
  getMock: vi.fn(),
  postMock: vi.fn(),
  getAccessTokenMock: vi.fn(),
  routerPush: vi.fn(),
}))

vi.mock('@vue-flow/core', () => ({
  VueFlow: { name: 'VueFlow', props: ['nodes', 'edges'], template: '<div class="vue-flow-stub" />' },
}))
vi.mock('@vue-flow/background', () => ({ Background: { template: '<div />' } }))
vi.mock('@vue-flow/controls', () => ({ Controls: { template: '<div />' } }))

vi.mock('../../components/pipeline/composite/PortDefinitionPanel.vue', () => ({
  default: {
    name: 'PortDefinitionPanelStub',
    props: ['ports', 'nodeIds', 'nodes'],
    emits: ['update:ports'],
    template: '<div data-testid="port-panel" />',
  },
}))

vi.mock('../../components/pipeline/composite/PublishCompositeFlow.vue', () => ({
  default: {
    name: 'PublishCompositeFlowStub',
    props: ['compositeId', 'ports'],
    emits: ['close', 'published'],
    template: '<div data-testid="publish-flow" />',
  },
}))

vi.mock('../../lib/api/client', () => ({
  api: { GET: getMock, POST: postMock, PUT: vi.fn(), PATCH: vi.fn(), DELETE: vi.fn() },
  getAccessToken: getAccessTokenMock,
}))

vi.mock('vue-router', () => ({
  useRoute: vi.fn(() => ({
    path: '/library/tpl-1/edit',
    fullPath: '/library/tpl-1/edit',
    params: { id: 'tpl-1' },
    query: {},
    hash: '',
    matched: [],
    name: null,
    redirectedFrom: undefined,
    meta: {},
  })),
  useRouter: vi.fn(() => ({ push: routerPush, replace: vi.fn() })),
  createRouter: vi.fn(),
  createWebHistory: vi.fn(() => ({})),
}))

import CompositeEditorView from '../../views/pipeline/CompositeEditorView.vue'

function fakeJwt(orgRole: string): string {
  const header = btoa(JSON.stringify({ alg: 'HS256', typ: 'JWT' }))
  const payload = btoa(JSON.stringify({ sub: 'test@example.com', org_role: orgRole }))
  return `${header}.${payload}.signature`
}

function templatePayload() {
  return {
    id: 'tpl-1',
    name: 'My Composite',
    parameter_ports_json: [
      { id: 'port-1', name: 'topic', label: 'Topic', description: 'the topic', type: 'string', required: true, default_value: 'news' },
    ],
  }
}

function editorPayload() {
  return {
    nodes: [
      { id: 'n1', node_type: 'agent', label: 'Writer Agent', position: { x: 10, y: 20 } },
      { id: 'n2', node_type: 'manual', label: null, position: { x: 0, y: 0 } },
    ],
    edges: [{ id: 'e1', source_node_id: 'n1', target_node_id: 'n2', edge_type: 'normal' }],
  }
}

function mountView() {
  return mount(CompositeEditorView, {
    global: {
      stubs: {
        BackLink: { template: '<div />' },
        PageHeader: { template: '<div />' },
      },
    },
  })
}

async function flush() {
  await flushPromises()
  await nextTick()
}

describe('CompositeEditorView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    routerPush.mockClear()
    getAccessTokenMock.mockReturnValue(fakeJwt('admin'))
    getMock.mockImplementation((url: string) => {
      if (url === '/api/v1/composite-templates/{template_id}') {
        return Promise.resolve({ data: templatePayload(), error: undefined })
      }
      if (url === '/api/v1/composite-templates/{template_id}/editor') {
        return Promise.resolve({ data: editorPayload(), error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    })
    postMock.mockResolvedValue({ data: { id: 'new-tpl' }, error: undefined })
  })

  it('BUG: the fetcher resolves but useDataFetch rejects with "data is undefined" so the editor page never renders', async () => {
    // PRODUCTION BUG characterisation (FAR-617 delivery): the view's
    // useDataFetch fetcher returns a bare `{}` instead of `{ data: ... }`.
    // useDataFetch's queryFn returns `result.data` (undefined), and vue-query
    // unconditionally throws "<queryHash> data is undefined" for undefined
    // query results. The throw lands in the pageError branch, so the composite
    // editor renders a permanent error page instead of the canvas — for every
    // load, in every environment. The fetcher's own side effects still ran.
    const wrapper = mountView()
    await flush()

    // Both endpoints were fetched and the fetcher's mapping ran (vm state).
    expect(getMock).toHaveBeenCalledWith('/api/v1/composite-templates/{template_id}', {
      params: { path: { template_id: 'tpl-1' } },
    })
    expect(getMock).toHaveBeenCalledWith('/api/v1/composite-templates/{template_id}/editor', {
      params: { path: { template_id: 'tpl-1' } },
    })
    const vm = wrapper.vm as unknown as Record<string, unknown>
    expect(vm.compositeName).toBe('My Composite')
    expect(vm.flowNodes).toEqual([
      { id: 'n1', type: 'agent', position: { x: 10, y: 20 }, data: { label: 'Writer Agent' } },
      { id: 'n2', type: 'manual', position: { x: 0, y: 0 }, data: { label: 'Node #n2' } },
    ])
    expect(vm.flowEdges).toEqual([
      { id: 'e1', source: 'n1', target: 'n2', type: 'smoothstep', data: { edge_type: 'normal' } },
    ])
    expect(vm.ports).toEqual([
      { id: 'port-1', name: 'topic', label: 'Topic', description: 'the topic', type: 'string', required: true, default: 'news' },
    ])

    // ...but the template is stuck on the error branch: no canvas, no toolbar.
    expect(wrapper.text()).toContain('data is undefined')
    expect(wrapper.find('.vue-flow-stub').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('Save as composite')
    wrapper.unmount()
  })

  it('BUG: a total API failure lands on the same error page (both GETs are catch-guarded to data:null)', async () => {
    getMock.mockRejectedValue(new Error('network down'))
    const wrapper = mountView()
    await flush()

    expect(wrapper.text()).toContain('data is undefined')
    expect(wrapper.text()).not.toContain('network down')
    wrapper.unmount()
  })

  it('canManage: operator/admin JWTs grant manage rights; other roles do not', async () => {
    const wrapper = mountView()
    await flush()
    expect((wrapper.vm as unknown as { canManage: boolean }).canManage).toBe(true)
    wrapper.unmount()

    getAccessTokenMock.mockReturnValue(fakeJwt('viewer'))
    const viewer = mountView()
    await flush()
    expect((viewer.vm as unknown as { canManage: boolean }).canManage).toBe(false)
    viewer.unmount()
  })

  it('flowNodeIds maps the converted nodes (used by the port panel node-ids prop)', async () => {
    const wrapper = mountView()
    await flush()
    expect((wrapper.vm as unknown as { flowNodeIds: string[] }).flowNodeIds).toEqual(['n1', 'n2'])
    wrapper.unmount()
  })

  it('handleSaveAs POSTs the composite payload (graph + mapped ports with target_injection) and navigates to the library', async () => {
    const wrapper = mountView()
    await flush()
    const vm = wrapper.vm as unknown as {
      saveAsName: string
      saveAsDescription: string
      saveAsError: string | null
      handleSaveAs: () => Promise<void>
    }

    vm.saveAsName = 'Cloned Composite'
    vm.saveAsDescription = 'a copy'
    await vm.handleSaveAs()
    await flush()

    expect(postMock).toHaveBeenCalledTimes(1)
    const [url, options] = postMock.mock.calls[0]
    expect(url).toBe('/api/v1/composite-templates')
    expect(options.body).toMatchObject({
      name: 'Cloned Composite',
      description: 'a copy',
      version: '1.0.0',
      sub_pipeline_graph_json: { nodes: editorPayload().nodes, edges: editorPayload().edges },
    })
    expect(options.body.parameter_ports_json).toHaveLength(1)
    expect(options.body.parameter_ports_json[0]).toMatchObject({
      id: 'port-1',
      name: 'topic',
      type: 'string',
      required: true,
      // BUG (data-loss on Save-as): the load mapping renames the stored
      // default_value to `default` (view line: `default: p.default_value`),
      // but handleSaveAs reads back `p.default_value` — which no longer
      // exists — so the saved composite template always loses the port's
      // default value ('news' becomes null). Characterised here as shipped;
      // if the view is fixed to read p.default, update this assertion.
      default_value: null,
      target_injection: { mode: 'prompt_replace', injection_point: 'prompt_template' },
    })
    expect(vm.saveAsError).toBe(null)
    expect(routerPush).toHaveBeenCalledWith({ name: 'library' })
    wrapper.unmount()
  })

  it('handleSaveAs without a name is a no-op and a POST failure records the formatted error', async () => {
    const wrapper = mountView()
    await flush()
    const vm = wrapper.vm as unknown as {
      saveAsName: string
      saveAsError: string | null
      handleSaveAs: () => Promise<void>
    }

    vm.saveAsName = ''
    await vm.handleSaveAs()
    expect(postMock).not.toHaveBeenCalled()

    postMock.mockRejectedValue(new Error('name already taken'))
    vm.saveAsName = 'Dup'
    await vm.handleSaveAs()
    await flush()
    expect(vm.saveAsError).toContain('name already taken')
    expect(routerPush).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('onPortsUpdate stores the panel-emitted ports and onPublished closes the flow and navigates', async () => {
    const wrapper = mountView()
    await flush()
    const vm = wrapper.vm as unknown as {
      ports: Array<Record<string, unknown>>
      onPortsUpdate: (p: Array<Record<string, unknown>>) => void
      onPublished: () => void
    }

    const updated = [{ id: 'port-x', name: 'x', label: 'X', type: 'string', required: false }]
    vm.onPortsUpdate(updated)
    await nextTick()
    expect(vm.ports).toEqual(updated)

    vm.onPublished()
    await nextTick()
    expect(routerPush).toHaveBeenCalledWith({ name: 'library' })
    wrapper.unmount()
  })
})
