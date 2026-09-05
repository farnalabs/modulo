import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import type { LifecycleMap, LifecycleMapStage, LifecycleMapTransition } from '../../stores/lifecycleMaps'
import type { JourneySummary } from '../../types/lifecycleMap'

// Heavy third-party widget: mock @vue-flow/core as a stub that renders the
// component's #node-stage slot for each node so the stage-node template is
// exercised without pulling the real flow renderer into jsdom.
vi.mock('@vue-flow/core', () => ({
  MarkerType: { ArrowClosed: 'arrowclosed' },
  VueFlow: {
    name: 'VueFlowStub',
    props: ['nodes', 'edges', 'defaultEdgeOptions'],
    template: `
      <div class="vueflow-stub">
        <template v-for="n in nodes" :key="n.id">
          <slot name="node-stage" :id="n.id" :data="n.data" />
        </template>
      </div>`,
  },
}))
vi.mock('@vue-flow/background', () => ({
  Background: { name: 'BackgroundStub', template: '<div class="bg-stub" />' },
}))
vi.mock('@vue-flow/controls', () => ({
  Controls: { name: 'ControlsStub', template: '<div class="controls-stub" />' },
}))
vi.mock('../../components/lifecycle-map/JourneyCard.vue', () => ({
  default: {
    name: 'JourneyCardStub',
    props: ['journey'],
    emits: ['open'],
    template: '<button type="button" class="journey-card-stub" @click="$emit(\'open\')">{{ journey.ref }}</button>',
  },
}))

import LifecycleMapRenderer from '../../components/lifecycle-map/LifecycleMapRenderer.vue'

function makeStage(overrides: Partial<LifecycleMapStage> = {}): LifecycleMapStage {
  return {
    id: 'stage-1',
    name: 'Ingest',
    description: 'Bring data in',
    type: 'modulo',
    owner_badge: 'Duncan',
    graduated: false,
    pipeline_id: 'pipe-1',
    external_url: null,
    x: 100,
    y: 200,
    ...overrides,
  }
}

function makeMap(overrides: { stages?: LifecycleMapStage[]; transitions?: LifecycleMapTransition[] } = {}): LifecycleMap {
  return {
    id: 'map-1',
    name: 'Delivery Map',
    description: null,
    owner: null,
    owner_team_id: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    stages: [makeStage()],
    transitions: [],
    versions: [],
    current_version: 1,
    ...overrides,
  }
}

function makeJourney(overrides: Partial<JourneySummary> = {}): JourneySummary {
  return {
    kind: 'pipeline',
    ref: 'nightly-etl',
    canonical_work_item_id: 'cwi-1',
    current_stage: { map_id: 'map-1', version: 1, stage_id: 'stage-1' } as JourneySummary['current_stage'],
    status: 'complete',
    provenance: null,
    run_count: 3,
    latest_run_id: null,
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

interface RendererProps {
  mapData: LifecycleMap | null
  journeys?: JourneySummary[]
  onModuloStageClick?: (stage: LifecycleMapStage) => void
  onExternalStageClick?: (stage: LifecycleMapStage) => void
}

function mountRenderer(props: RendererProps) {
  return mount(LifecycleMapRenderer, { props })
}

describe('LifecycleMapRenderer', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows the no-data state when mapData is null', () => {
    const wrapper = mountRenderer({ mapData: null })
    expect(wrapper.text()).toContain('No map data provided.')
    expect(wrapper.find('.vueflow-stub').exists()).toBe(false)
  })

  it('builds flow nodes from stages preserving explicit positions', () => {
    const wrapper = mountRenderer({ mapData: makeMap() })
    const flow = wrapper.findComponent({ name: 'VueFlowStub' })
    const nodes = flow.props('nodes') as Array<{ id: string; position: { x: number; y: number }; data: Record<string, unknown> }>
    expect(nodes).toHaveLength(1)
    expect(nodes[0].id).toBe('stage-1')
    expect(nodes[0].position).toEqual({ x: 100, y: 200 })
    expect(nodes[0].data).toMatchObject({
      stageId: 'stage-1',
      label: 'Ingest',
      description: 'Bring data in',
      type: 'modulo',
      ownerBadge: 'Duncan',
      graduated: false,
      pipelineId: 'pipe-1',
    })
  })

  it('auto-lays out stages that have no explicit position', () => {
    const wrapper = mountRenderer({
      mapData: makeMap({ stages: [makeStage({ id: 's1', x: null, y: null }), makeStage({ id: 's2', name: 'Review', x: null, y: null })] }),
    })
    const flow = wrapper.findComponent({ name: 'VueFlowStub' })
    const nodes = flow.props('nodes') as Array<{ position: { x: number; y: number } }>
    expect(nodes.every(n => Number.isFinite(n.position.x) && Number.isFinite(n.position.y))).toBe(true)
  })

  it('maps transitions to edges with trigger labels', () => {
    const transitions: LifecycleMapTransition[] = [
      { id: 't1', source_stage_id: 'stage-1', target_stage_id: 'stage-2', trigger_type: 'pipeline_completed', description: 'When ingest finishes' },
    ]
    const wrapper = mountRenderer({
      mapData: makeMap({ stages: [makeStage(), makeStage({ id: 'stage-2', name: 'Review' })], transitions }),
    })
    const flow = wrapper.findComponent({ name: 'VueFlowStub' })
    const edges = flow.props('edges') as Array<Record<string, unknown>>
    expect(edges).toHaveLength(1)
    expect(edges[0]).toMatchObject({ id: 't1', source: 'stage-1', target: 'stage-2', label: 'pipeline_completed', title: 'When ingest finishes' })
  })

  it('renders stage nodes with graduated and planned badges', async () => {
    const wrapper = mountRenderer({
      mapData: makeMap({
        stages: [
          makeStage({ graduated: true }),
          makeStage({ id: 'stage-2', name: 'Planned Stage', type: 'placeholder', description: null, owner_badge: null }),
        ],
      }),
    })
    await nextTick()
    const text = wrapper.text()
    expect(text).toContain('Ingest')
    expect(text).toContain('Graduated')
    expect(text).toContain('Planned Stage')
    expect(text).toContain('Planned')
    expect(text).toContain('Bring data in')
    expect(text).toContain('Duncan')
  })

  it('calls onModuloStageClick for a modulo stage and onExternalStageClick for an external stage', async () => {
    const onModulo = vi.fn()
    const onExternal = vi.fn()
    const wrapper = mountRenderer({
      mapData: makeMap({
        stages: [makeStage(), makeStage({ id: 'stage-2', name: 'Vendor', type: 'external', pipeline_id: null, external_url: 'https://x' })],
      }),
      onModuloStageClick: onModulo,
      onExternalStageClick: onExternal,
    })
    const stages = wrapper.findAll('.stage-node')
    expect(stages).toHaveLength(2)
    await stages[0].trigger('click')
    expect(onModulo).toHaveBeenCalledTimes(1)
    expect(onModulo.mock.calls[0][0].id).toBe('stage-1')
    await stages[1].trigger('click')
    expect(onExternal).toHaveBeenCalledTimes(1)
    expect(onExternal.mock.calls[0][0].id).toBe('stage-2')
  })

  it('invokes the click handler via keyboard enter and space', async () => {
    const onModulo = vi.fn()
    const wrapper = mountRenderer({ mapData: makeMap(), onModuloStageClick: onModulo })
    const stage = wrapper.find('.stage-node')
    await stage.trigger('keydown.enter')
    await stage.trigger('keydown.space')
    expect(onModulo).toHaveBeenCalledTimes(2)
  })

  it('does nothing on click for manual or placeholder stages', async () => {
    const onModulo = vi.fn()
    const onExternal = vi.fn()
    const wrapper = mountRenderer({
      mapData: makeMap({ stages: [makeStage({ type: 'manual', pipeline_id: null })] }),
      onModuloStageClick: onModulo,
      onExternalStageClick: onExternal,
    })
    await wrapper.find('.stage-node').trigger('click')
    expect(onModulo).not.toHaveBeenCalled()
    expect(onExternal).not.toHaveBeenCalled()
  })

  it('groups journeys onto their current stage and emits journey-open', async () => {
    const journey = makeJourney()
    const wrapper = mountRenderer({
      mapData: makeMap({
        stages: [makeStage(), makeStage({ id: 'stage-2', name: 'Review' })],
      }),
      journeys: [journey, makeJourney({ ref: 'other-flow', current_stage: { map_id: 'map-1', version: 1, stage_id: 'stage-2' } as JourneySummary['current_stage'] })],
    })
    const cards = wrapper.findAll('.journey-card-stub')
    expect(cards).toHaveLength(2)
    expect(cards[0].text()).toBe('nightly-etl')
    await cards[0].trigger('click')
    expect(wrapper.emitted('journey-open')![0]).toEqual([journey])
  })

  it('ignores journeys without a current stage', () => {
    const wrapper = mountRenderer({
      mapData: makeMap(),
      journeys: [makeJourney({ current_stage: null })],
    })
    expect(wrapper.findAll('.journey-card-stub')).toHaveLength(0)
  })

  it('applies type-specific styling classes to stage nodes', () => {
    const wrapper = mountRenderer({
      mapData: makeMap({
        stages: [
          makeStage(),
          makeStage({ id: 's2', name: 'Ext', type: 'external', pipeline_id: null }),
          makeStage({ id: 's3', name: 'Manual', type: 'manual', pipeline_id: null }),
          makeStage({ id: 's4', name: 'Ph', type: 'placeholder', pipeline_id: null }),
        ],
      }),
    })
    const stages = wrapper.findAll('.stage-node')
    expect(stages[0].classes().join(' ')).toContain('border-blue-500')
    expect(stages[1].classes().join(' ')).toContain('border-emerald-500')
    expect(stages[2].classes().join(' ')).toContain('border-amber-500')
    expect(stages[3].classes().join(' ')).toContain('opacity-60')
  })
})
