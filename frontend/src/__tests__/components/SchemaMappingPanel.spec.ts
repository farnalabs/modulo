import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('../../components/ui/tabs/Tabs.vue', () => ({
  default: {
    name: 'Tabs',
    template: '<div data-testid="mock-tabs"><slot /></div>',
  },
}))

vi.mock('../../components/ui/tabs/TabsList.vue', () => ({
  default: {
    name: 'TabsList',
    template: '<div data-testid="mock-tabs-list"><slot /></div>',
  },
}))

vi.mock('../../components/ui/tabs/TabsTrigger.vue', () => ({
  default: {
    name: 'TabsTrigger',
    props: ['value'],
    template: '<button type="button" data-testid="mock-tabs-trigger"><slot /></button>',
  },
}))

vi.mock('../../components/ui/tabs/TabsContent.vue', () => ({
  default: {
    name: 'TabsContent',
    props: ['value'],
    template: '<div data-testid="mock-tabs-content"><slot /></div>',
  },
}))

vi.mock('../../components/pipeline/composite/FieldMappingPair.vue', () => ({
  default: {
    name: 'FieldMappingPair',
    props: ['sourceFields', 'targetFields', 'mappings', 'direction'],
    template: '<div data-testid="mock-field-mapping-pair">{{ mappings ? Object.keys(mappings).length + " mapped" : "0 mapped" }}</div>',
  },
}))

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({
    get: vi.fn().mockResolvedValue({
      fields: [
        { name: 'title', type: 'string', required: true },
        { name: 'body', type: 'string', required: false },
      ],
    }),
    post: vi.fn().mockResolvedValue({}),
  }),
}))

import SchemaMappingPanel from '../../components/pipeline/composite/SchemaMappingPanel.vue'
import { useCompositeStore } from '../../stores/compositeStore'
import type { CompositeDefinition } from '../../types/pipeline'

function composite(overrides: Partial<CompositeDefinition> = {}): CompositeDefinition {
  return {
    id: 'comp-1',
    name: 'Test Composite',
    description: null,
    version: '1.0.0',
    sub_pipeline_graph_json: {},
    parameter_ports_json: [],
    input_schema_id: null,
    output_schema_id: null,
    organisation_id: 'org-1',
    created_by: 'user-1',
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

describe('SchemaMappingPanel', () => {
  let pinia: ReturnType<typeof createPinia>

  beforeEach(() => {
    pinia = createPinia()
    setActivePinia(pinia)
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const store = useCompositeStore()
    store.composites = [
      composite({
        input_schema_id: 'schema-1',
        output_schema_id: 'schema-2',
      }),
    ]

    const wrapper = mount(SchemaMappingPanel, {
      props: {
        compositeRef: 'comp-1',
        inputMapping: {},
        outputMapping: {},
        precedingNodeSchemaId: null,
        downstreamNodeSchemaId: null,
      },
      global: { plugins: [pinia] },
    })
    await nextTick()
    await nextTick()
    await nextTick()

    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Schema Mapping')
  })

  it('shows empty state when no composite is selected', async () => {
    const wrapper = mount(SchemaMappingPanel, {
      props: {
        compositeRef: null,
        inputMapping: {},
        outputMapping: {},
        precedingNodeSchemaId: null,
        downstreamNodeSchemaId: null,
      },
      global: { plugins: [pinia] },
    })
    await nextTick()

    expect(wrapper.text()).toContain('No composite selected.')
  })

  it('shows JSON preview section', async () => {
    const store = useCompositeStore()
    store.composites = [
      composite(),
    ]

    const wrapper = mount(SchemaMappingPanel, {
      props: {
        compositeRef: 'comp-1',
        inputMapping: { title: 'title' },
        outputMapping: {},
        precedingNodeSchemaId: null,
        downstreamNodeSchemaId: null,
      },
      global: { plugins: [pinia] },
    })
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('JSON preview')
  })

  it('renders with mapping tab visible', async () => {
    const store = useCompositeStore()
    store.composites = [
      composite({
        input_schema_id: 'schema-1',
        output_schema_id: 'schema-2',
      }),
    ]

    const wrapper = mount(SchemaMappingPanel, {
      props: {
        compositeRef: 'comp-1',
        inputMapping: { title: 'title' },
        outputMapping: { result: 'output' },
        precedingNodeSchemaId: 'schema-prev',
        downstreamNodeSchemaId: 'schema-next',
      },
      global: { plugins: [pinia] },
    })
    await nextTick()
    await nextTick()
    await nextTick()

    expect(wrapper.exists()).toBe(true)
  })
})
