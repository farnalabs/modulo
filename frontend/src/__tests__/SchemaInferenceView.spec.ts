import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { useRouter } from 'vue-router'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn(),
    POST: vi.fn(),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import { api } from '../lib/api/client'
import SchemaInferenceView from '../views/SchemaInferenceView.vue'

type Connector = {
  id: string
  name: string
  connector_type_id: string
}

const CONNECTORS: Connector[] = [
  { id: 'conn-1', name: 'GitHub', connector_type_id: 'github' },
  { id: 'conn-2', name: 'Jira', connector_type_id: 'jira' },
]

const INFER_OK = {
  definition_json: {
    type: 'object',
    properties: {
      title: { type: 'string', description: 'Issue title' },
      priority: { type: 'number' },
    },
    required: ['title'],
  },
  sample_count: 2,
  suggestion_name: 'Inferred from GitHub',
  suggestion_description: 'Auto-inferred schema from GitHub (issues, 2 samples)',
}

describe('SchemaInferenceView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.GET as any).mockResolvedValue({ data: { items: [] }, error: undefined })
    ;(api.POST as any).mockResolvedValue({ data: null, error: undefined })
  })

  async function mountView() {
    const wrapper = mount(SchemaInferenceView, {
      global: {
        stubs: { RouterLink: true },
      },
    })
    await nextTick()
    await nextTick()
    return wrapper
  }

  it('renders without crashing', async () => {
    const wrapper = await mountView()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Schema Inference')
  })

  it('loads connectors on mount and populates the dropdown list', async () => {
    ;(api.GET as any).mockResolvedValue({ data: { items: CONNECTORS }, error: undefined })
    const wrapper = await mountView()
    const connectors = (wrapper.vm as any).connectors as Connector[]
    expect(connectors).toHaveLength(2)
    expect(connectors.map((c) => c.name)).toEqual(['GitHub', 'Jira'])
  })

  it('shows the no-connectors hint when the connector list is empty', async () => {
    const wrapper = await mountView()
    expect(wrapper.text()).toContain('No connectors available')
  })

  it('shows an error message when connector loading fails', async () => {
    ;(api.GET as any).mockResolvedValue({ data: undefined, error: { detail: 'connector fetch exploded' } })
    const wrapper = await mountView()
    expect(wrapper.text()).toContain('Failed to load connectors')
    expect(wrapper.text()).toContain('connector fetch exploded')
  })

  it('disables the infer button until a connector and resource type are selected', async () => {
    ;(api.GET as any).mockResolvedValue({ data: { items: CONNECTORS }, error: undefined })
    const wrapper = await mountView()
    const button = wrapper.find('[data-testid="schema-inference-infer-schema"]')
    expect(button.attributes('disabled')).toBeDefined()

    ;(wrapper.vm as any).selectedConnectorId = 'conn-1'
    await wrapper.find('[data-testid="schema-inference-resource-type"]').setValue('issues')
    await nextTick()
    expect(wrapper.find('[data-testid="schema-inference-infer-schema"]').attributes('disabled')).toBeUndefined()
  })

  it('calls the infer endpoint with the sample_query body and renders the draft fields', async () => {
    ;(api.GET as any).mockResolvedValue({ data: { items: CONNECTORS }, error: undefined })
    ;(api.POST as any).mockResolvedValue({ data: INFER_OK, error: undefined })
    const wrapper = await mountView()

    ;(wrapper.vm as any).selectedConnectorId = 'conn-1'
    await wrapper.find('[data-testid="schema-inference-resource-type"]').setValue('issues')
    await wrapper.find('[data-testid="schema-inference-infer-schema"]').trigger('click')
    await nextTick()
    await nextTick()

    expect(api.POST).toHaveBeenCalledWith('/api/v1/schemas/infer', {
      body: {
        connector_instance_id: 'conn-1',
        sample_query: { resource: 'issues', filters: {}, limit: 10, query: undefined },
      },
    })
    expect(wrapper.text()).toContain('Inferred from GitHub')
    expect(wrapper.text()).toContain('title')
    expect(wrapper.text()).toContain('priority')
  })

  it('shows the inference error message when the API call fails', async () => {
    ;(api.GET as any).mockResolvedValue({ data: { items: CONNECTORS }, error: undefined })
    ;(api.POST as any).mockResolvedValue({ data: undefined, error: { detail: 'LLM returned garbage' } })
    const wrapper = await mountView()

    ;(wrapper.vm as any).selectedConnectorId = 'conn-1'
    await wrapper.find('[data-testid="schema-inference-resource-type"]').setValue('issues')
    await wrapper.find('[data-testid="schema-inference-infer-schema"]').trigger('click')
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('Schema inference failed')
    expect(wrapper.text()).toContain('LLM returned garbage')
  })

  it('publishes the draft as a schema envelope + version and navigates to the library', async () => {
    ;(api.GET as any).mockResolvedValue({ data: { items: CONNECTORS }, error: undefined })
    ;(api.POST as any)
      .mockResolvedValueOnce({ data: INFER_OK, error: undefined })
      .mockResolvedValueOnce({ data: { id: 'schema-1', name: 'Inferred from GitHub' }, error: undefined })
      .mockResolvedValueOnce({ data: {}, error: undefined })
    const router = useRouter()
    const wrapper = await mountView()

    ;(wrapper.vm as any).selectedConnectorId = 'conn-1'
    await wrapper.find('[data-testid="schema-inference-resource-type"]').setValue('issues')
    await wrapper.find('[data-testid="schema-inference-infer-schema"]').trigger('click')
    await nextTick()
    await nextTick()

    await wrapper.find('[data-testid="schema-inference-publish"]').trigger('click')
    await nextTick()
    await nextTick()

    expect(api.POST).toHaveBeenCalledWith('/api/v1/schemas', {
      body: { name: 'Inferred from GitHub', description: 'Auto-inferred schema from GitHub (issues, 2 samples)' },
    })
    expect(api.POST).toHaveBeenCalledWith('/api/v1/schemas/{schema_id}/versions', {
      params: { path: { schema_id: 'schema-1' } },
      body: {
        version: 'v1',
        version_number: 1,
        definition_json: INFER_OK.definition_json,
        published: true,
      },
    })
    expect(wrapper.text()).toContain('published')

    await vi.waitFor(() => expect(router.push).toHaveBeenCalledWith({ name: 'library' }), { timeout: 3000 })
  })

  it('shows the publish error when creating the schema envelope fails', async () => {
    ;(api.GET as any).mockResolvedValue({ data: { items: CONNECTORS }, error: undefined })
    ;(api.POST as any)
      .mockResolvedValueOnce({ data: INFER_OK, error: undefined })
      .mockResolvedValueOnce({ data: undefined, error: { detail: 'name already exists' } })
    const wrapper = await mountView()

    ;(wrapper.vm as any).selectedConnectorId = 'conn-1'
    await wrapper.find('[data-testid="schema-inference-resource-type"]').setValue('issues')
    await wrapper.find('[data-testid="schema-inference-infer-schema"]').trigger('click')
    await nextTick()
    await nextTick()

    await wrapper.find('[data-testid="schema-inference-publish"]').trigger('click')
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('Publish failed')
    expect(wrapper.text()).toContain('name already exists')
  })
})
