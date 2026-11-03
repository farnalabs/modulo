import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import enUS from '../locales/en-US.js'
import SystemMapHeaderBar from '../components/system-maps/SystemMapHeaderBar.vue'
import SystemMapRevisionSidebar from '../components/system-maps/SystemMapRevisionSidebar.vue'
import SystemMapDiscoveryPanel from '../components/system-maps/SystemMapDiscoveryPanel.vue'
import SystemMapGraphEditor from '../components/system-maps/SystemMapGraphEditor.vue'
import { mergeDiscoverySelections } from '../components/system-maps/discoveryMerge'
import { NODE_TYPES, RELATIONSHIP_TYPES, type SystemMapNode, type SystemMapRelationship } from '../stores/systemMaps'

function pointerEvent(type: string, clientX: number, clientY: number): MouseEvent {
  const event = new MouseEvent(type)
  Object.defineProperty(event, 'clientX', { value: clientX })
  Object.defineProperty(event, 'clientY', { value: clientY })
  return event
}

const i18n = createI18n({ legacy: false, locale: 'en-US', messages: { 'en-US': enUS } })

function node(overrides: Partial<SystemMapNode> = {}): SystemMapNode {
  return {
    id: 'n1',
    organisation_id: '',
    revision_id: '',
    node_key: 'pipeline-1',
    node_type: 'pipeline',
    display_name: 'Pipeline 1',
    config_json: {},
    pipeline_id: null,
    connector_instance_id: null,
    owner_team_id: null,
    eval_id: null,
    position_x: '0',
    position_y: '0',
    deleted_at: null,
    deleted_by: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  } as SystemMapNode
}

function relationship(overrides: Partial<SystemMapRelationship> = {}): SystemMapRelationship {
  return {
    id: 'r1',
    organisation_id: '',
    revision_id: '',
    source_node_key: 'pipeline-1',
    target_node_key: 'connector-2',
    relationship_type: 'flow',
    config_json: {},
    deleted_at: null,
    deleted_by: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  } as SystemMapRelationship
}

function revision(overrides: Record<string, unknown> = {}) {
  return {
    id: 'rev-1',
    revision_number: 1,
    status: 'draft',
    created_at: '2026-01-01T00:00:00Z',
    published_at: null,
    ...overrides,
  }
}

function map() {
  return { id: 'map-1', name: 'Delivery map', published_revision_id: null }
}

describe('SystemMapHeaderBar', () => {
  function mountBar(props: Record<string, unknown>) {
    return mount(SystemMapHeaderBar, { props: props as never, global: { plugins: [i18n] } })
  }

  it('falls back to defaults with no map or revision', () => {
    const wrapper = mountBar({ map: null, revision: null })

    expect(wrapper.text()).toContain('System Map')
    expect(wrapper.get('[data-testid="system-map-publish"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-testid="system-map-archive"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('Read only')
  })

  it('enables save and publish for a draft revision and emits actions', async () => {
    const wrapper = mountBar({ map: map(), revision: revision(), saving: false })

    expect(wrapper.get('[data-testid="system-map-save-draft"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.get('[data-testid="system-map-publish"]').attributes('disabled')).toBeUndefined()

    await wrapper.get('[data-testid="system-map-header-toggle-history"]').trigger('click')
    await wrapper.get('[data-testid="system-map-save-draft"]').trigger('click')
    await wrapper.get('[data-testid="system-map-discover"]').trigger('click')
    await wrapper.get('[data-testid="system-map-publish"]').trigger('click')

    expect(wrapper.emitted('toggle-sidebar')).toBeTruthy()
    expect(wrapper.emitted('save')).toBeTruthy()
    expect(wrapper.emitted('discover')).toBeTruthy()
    expect(wrapper.emitted('publish')).toBeTruthy()
  })

  it('shows saving state and disables save', () => {
    const wrapper = mountBar({ map: map(), revision: revision(), saving: true })

    expect(wrapper.get('[data-testid="system-map-save-draft"]').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('Saving')
  })

  it('marks a published revision read-only and offers archive', async () => {
    const wrapper = mountBar({ map: map(), revision: revision({ status: 'published' }) })

    expect(wrapper.text()).toContain('Read only')
    expect(wrapper.get('[data-testid="system-map-save-draft"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="system-map-publish"]').attributes('disabled')).toBeDefined()

    await wrapper.get('[data-testid="system-map-archive"]').trigger('click')
    expect(wrapper.emitted('archive')).toBeTruthy()
  })

  it('marks an archived revision read-only without an archive button', () => {
    const wrapper = mountBar({ map: map(), revision: revision({ status: 'archived' }) })

    expect(wrapper.text()).toContain('Read only')
    expect(wrapper.find('[data-testid="system-map-archive"]').exists()).toBe(false)
  })
})

describe('SystemMapRevisionSidebar', () => {
  function mountSidebar(props: Record<string, unknown>) {
    return mount(SystemMapRevisionSidebar, { props: props as never, global: { plugins: [i18n] } })
  }

  it('shows an empty message when there are no revisions', () => {
    const wrapper = mountSidebar({ mapId: 'map-1', revisions: [], activeMap: null })

    expect(wrapper.text()).toContain('No revisions')
  })

  it('sorts revisions newest first and styles each status', () => {
    const wrapper = mountSidebar({
      mapId: 'map-1',
      activeRevisionId: 'rev-2',
      activeMap: map(),
      revisions: [
        revision({ id: 'rev-1', revision_number: 1, status: 'draft' }),
        revision({ id: 'rev-3', revision_number: 3, status: 'archived', published_at: '2026-02-01T00:00:00Z' }),
        revision({ id: 'rev-2', revision_number: 2, status: 'published', published_at: '2026-01-15T00:00:00Z' }),
      ],
    })

    const buttons = wrapper.findAll('[data-testid^="system-map-revision-"]')
    expect(buttons.map((button) => button.attributes('data-testid'))).toEqual([
      'system-map-revision-3',
      'system-map-revision-2',
      'system-map-revision-1',
    ])
    expect(wrapper.text()).toContain('Effective from')
    expect(wrapper.html()).toContain('bg-success/10')
    expect(wrapper.html()).toContain('text-muted-foreground')
    expect(wrapper.html()).toContain('bg-warning/10')
    expect(wrapper.get('[data-testid="system-map-revision-2"]').classes()).toContain('border-primary')
  })

  it('emits select, new-draft and discover events', async () => {
    const wrapper = mountSidebar({
      mapId: 'map-1',
      activeRevisionId: 'rev-1',
      activeMap: map(),
      revisions: [revision()],
    })

    await wrapper.get('[data-testid="system-map-revision-1"]').trigger('click')
    await wrapper.get('[data-testid="system-map-new-draft"]').trigger('click')
    await wrapper.get('[data-testid="system-map-sidebar-discover"]').trigger('click')

    expect(wrapper.emitted('select-revision')![0]).toEqual(['rev-1'])
    expect(wrapper.emitted('new-draft')).toBeTruthy()
    expect(wrapper.emitted('discover')).toBeTruthy()
  })
})

describe('SystemMapDiscoveryPanel coverage', () => {
  const discovery = {
    sources_inspected: [{ kind: 'github', name: 'Repo', identifier: 'org/repo' }],
    node_count: 1,
    edge_count: 1,
    inferred_nodes: [{ node_key: 'pipeline:a', node_type: 'pipeline', display_name: 'Pipeline A', config_json: null }],
    inferred_edges: [{ source_node_key: 'pipeline:a', target_node_key: 'connector:b', relationship_type: 'flow' }],
    skipped: [{ source: 'gitlab', reason: 'not configured' }],
    warnings: ['partial scan'],
    draft_map_id: 'map',
    draft_revision_id: 'rev',
    draft_revision_number: 1,
    published: false,
  }

  function mountPanel() {
    return mount(SystemMapDiscoveryPanel, { props: { discovery: discovery as never }, global: { plugins: [i18n] } })
  }

  it('renders sources, skipped sources and warnings', () => {
    const wrapper = mountPanel()

    expect(wrapper.text()).toContain('github: Repo (org/repo)')
    expect(wrapper.text()).toContain('gitlab: not configured')
    expect(wrapper.text()).toContain('partial scan')
  })

  it('switches a decision between accepted and rejected', async () => {
    const wrapper = mountPanel()

    const accept = wrapper.get('[data-testid="system-map-accept-node-pipeline:a"]')
    const reject = wrapper.get('[data-testid="system-map-reject-node-pipeline:a"]')

    await accept.trigger('click')
    expect(accept.attributes('aria-pressed')).toBe('true')
    expect(wrapper.get('[data-testid="system-map-accept-node-pipeline:a"]').classes().join(' ')).toContain('border-success')

    await reject.trigger('click')
    expect(wrapper.html()).toContain('border-destructive')

    await wrapper.get('[data-testid="system-map-accept-edge-0"]').trigger('click')
    expect(wrapper.html()).toContain('border-success')

    await wrapper.get('[data-testid="system-map-reject-edge-0"]').trigger('click')
    await wrapper.get('[data-testid="system-map-discovery-save"]').trigger('click')

    expect(wrapper.emitted('save')![0][0]).toEqual({ acceptedNodes: [], acceptedEdges: [], rejectedNodes: [0], rejectedEdges: [0] })
  })

  it('closes from the overlay, close button and cancel button', async () => {
    const wrapper = mountPanel()

    await wrapper.get('[data-testid="system-map-discovery-backdrop"]').trigger('click')
    await wrapper.get('[data-testid="system-map-discovery-close"]').trigger('click')
    await wrapper.get('[data-testid="system-map-discovery-cancel"]').trigger('click')

    expect(wrapper.emitted('close')).toHaveLength(3)
  })

  it('shows empty state copy when discovery has no suggestions', () => {
    const wrapper = mount(SystemMapDiscoveryPanel, {
      props: {
        discovery: {
          sources_inspected: [],
          inferred_nodes: [],
          inferred_edges: [],
          skipped: [],
          warnings: [],
          draft_map_id: 'map',
          draft_revision_id: 'rev',
        } as never,
      },
      global: { plugins: [i18n] },
    })

    expect(wrapper.text()).toContain('No suggested nodes')
    expect(wrapper.text()).toContain('No suggested relationships')
  })
})

describe('SystemMapGraphEditor coverage', () => {
  const emptyProps = { nodes: [], relationships: [], loopConfigs: [], nodeTypes: NODE_TYPES, relationshipTypes: RELATIONSHIP_TYPES }

  function mountEditor(overrides: Record<string, unknown> = {}) {
    return mount(SystemMapGraphEditor, { props: { ...emptyProps, ...overrides } as never, global: { plugins: [i18n] } })
  }

  it('adds node types that contain an underscore', async () => {
    const wrapper = mountEditor()

    await wrapper.get('[data-testid="system-map-add-node-improvement_loop"]').trigger('click')

    expect(wrapper.find('[data-testid="system-map-node-improvement_loop-1"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('improvement loop 1')
  })

  it('seeds the counter at zero for a non-numeric node key', async () => {
    const wrapper = mountEditor({ nodes: [node({ node_key: 'pipeline:custom' })] })

    await wrapper.get('[data-testid="system-map-add-node-pipeline"]').trigger('click')

    expect(wrapper.find('[data-testid="system-map-node-pipeline-1"]').exists()).toBe(true)
  })

  it('removes a node and its incident relationships', async () => {
    const wrapper = mountEditor({
      nodes: [node(), node({ id: 'n2', node_key: 'connector-2', node_type: 'connector', display_name: 'Connector 2' })],
      relationships: [relationship()],
    })

    await wrapper.get('[data-testid="system-map-remove-node-pipeline-1"]').trigger('click')

    const emitted = wrapper.emitted('relationships-change')!
    expect(emitted[emitted.length - 1][0]).toEqual([])
    expect(wrapper.find('[data-testid="system-map-node-pipeline-1"]').exists()).toBe(false)
  })

  it('skips edges whose endpoint is missing', () => {
    const wrapper = mountEditor({
      nodes: [node()],
      relationships: [relationship({ target_node_key: 'missing-node' })],
    })

    expect(wrapper.find('svg').exists()).toBe(false)
  })

  it('removes a relationship', async () => {
    const wrapper = mountEditor({
      nodes: [node(), node({ id: 'n2', node_key: 'connector-2', node_type: 'connector', display_name: 'Connector 2' })],
      relationships: [relationship()],
    })

    await wrapper.get('[data-testid="system-map-remove-relationship-r1"]').trigger('click')

    expect(wrapper.find('[data-testid="system-map-relationships"]').exists()).toBe(false)
  })

  it('refuses to add a relationship whose endpoints are identical', async () => {
    const wrapper = mountEditor({
      nodes: [node(), node({ id: 'n2', node_key: 'connector-2', node_type: 'connector', display_name: 'Connector 2' })],
    })

    await wrapper.get('[data-testid="system-map-relationship-target"]').setValue('pipeline-1')
    expect(wrapper.get('[data-testid="system-map-add-relationship"]').attributes('disabled')).toBeDefined()

    const vm = wrapper.vm as unknown as { addRelationship: () => void }
    vm.addRelationship()
    expect(wrapper.emitted('relationships-change')).toBeFalsy()
  })

  it('emits node edits for name and metadata changes', async () => {
    const wrapper = mountEditor({ nodes: [node()] })

    await wrapper.get('[aria-label="Node display name"]').setValue('Renamed')
    await wrapper.get('[aria-label="Node metadata"]').setValue('team-a')

    expect(wrapper.emitted('nodes-change')!.length).toBeGreaterThanOrEqual(2)
  })

  it('moves a node by keyboard in each direction and ignores other keys', async () => {
    const wrapper = mountEditor({ nodes: [node()] })
    const handle = wrapper.get('[data-testid="system-map-move-node-pipeline-1"]')

    await handle.trigger('keydown', { key: 'ArrowRight' })
    await handle.trigger('keydown', { key: 'ArrowDown' })
    await handle.trigger('keydown', { key: 'ArrowLeft' })
    await handle.trigger('keydown', { key: 'ArrowUp' })
    const before = wrapper.emitted('nodes-change')!.length
    await handle.trigger('keydown', { key: 'Tab' })

    const payload = wrapper.emitted('nodes-change')!.slice(-1)[0][0] as Array<Record<string, unknown>>
    expect(payload[0].position_x).toBe('0')
    expect(payload[0].position_y).toBe('0')
    expect(wrapper.emitted('nodes-change')!.length).toBe(before)
  })

  it('drags a node and commits the position on pointer up', async () => {
    const wrapper = mountEditor({ nodes: [node()] })
    const handle = wrapper.get('[data-testid="system-map-move-node-pipeline-1"]')

    handle.element.dispatchEvent(pointerEvent('pointerdown', 10, 10))
    window.dispatchEvent(pointerEvent('pointermove', 40, 30))
    window.dispatchEvent(new MouseEvent('pointerup'))

    const emitted = wrapper.emitted('nodes-change')!
    const payload = emitted[emitted.length - 1][0] as Array<Record<string, unknown>>
    expect(payload[0].position_x).toBe('30')
    expect(payload[0].position_y).toBe('20')
  })

  it('cancels a drag on pointercancel', async () => {
    const wrapper = mountEditor({ nodes: [node()] })
    const handle = wrapper.get('[data-testid="system-map-move-node-pipeline-1"]')

    handle.element.dispatchEvent(pointerEvent('pointerdown', 0, 0))
    window.dispatchEvent(pointerEvent('pointermove', 5, 5))
    window.dispatchEvent(new MouseEvent('pointercancel'))

    expect(wrapper.emitted('nodes-change')).toBeTruthy()
  })

  it('ignores a drag move when the dragged node has disappeared', async () => {
    const wrapper = mountEditor({ nodes: [node()] })
    const handle = wrapper.get('[data-testid="system-map-move-node-pipeline-1"]')

    handle.element.dispatchEvent(pointerEvent('pointerdown', 0, 0))
    await wrapper.get('[data-testid="system-map-remove-node-pipeline-1"]').trigger('click')
    window.dispatchEvent(pointerEvent('pointermove', 50, 50))
    window.dispatchEvent(new MouseEvent('pointerup'))

    expect(wrapper.find('[data-testid="system-map-node-pipeline-1"]').exists()).toBe(false)
  })

  it('disables authoring controls when read-only', async () => {
    const wrapper = mountEditor({ nodes: [node()], readOnly: true })

    expect(wrapper.get('[data-testid="system-map-add-node-pipeline"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="system-map-remove-node-pipeline-1"]').attributes('disabled')).toBeDefined()

    const handle = wrapper.get('[data-testid="system-map-move-node-pipeline-1"]')
    handle.element.dispatchEvent(pointerEvent('pointerdown', 0, 0))
    await handle.trigger('keydown', { key: 'ArrowRight' })

    expect(wrapper.emitted('nodes-change')).toBeFalsy()
  })
})

describe('mergeDiscoverySelections edge cases', () => {
  const discovery = {
    sources_inspected: [],
    inferred_nodes: [
      { node_key: 'pipeline:a', node_type: 'pipeline', display_name: 'Pipeline A', config_json: null, pipeline_id: undefined, connector_instance_id: undefined, owner_team_id: undefined },
      { node_key: 'connector:b', node_type: 'connector', display_name: 'Connector B', config_json: null },
    ],
    inferred_edges: [{ source_node_key: 'pipeline:a', target_node_key: 'connector:b', relationship_type: 'flow', config_json: null }],
    skipped: [],
    warnings: [],
  }

  it('ignores out-of-range accepted and rejected indexes', () => {
    const merged = mergeDiscoverySelections([node()], [], discovery as never, {
      acceptedNodes: [9],
      acceptedEdges: [9],
      rejectedNodes: [9],
      rejectedEdges: [9],
    })

    expect(merged.nodes).toHaveLength(1)
    expect(merged.relationships).toHaveLength(0)
  })

  it('defaults nullable inferred fields and positions the node', () => {
    const merged = mergeDiscoverySelections([], [], discovery as never, {
      acceptedNodes: [0, 1],
      acceptedEdges: [0],
      rejectedNodes: [],
      rejectedEdges: [],
    })

    expect(merged.nodes[0].config_json).toEqual({})
    expect(merged.nodes[0].pipeline_id).toBeNull()
    expect(merged.nodes[0].connector_instance_id).toBeNull()
    expect(merged.nodes[0].owner_team_id).toBeNull()
    expect(merged.nodes[0].eval_id).toBeNull()
    expect(merged.relationships[0].config_json).toEqual({})
  })
})

describe('SystemMapDiscoveryPanel decision edge cases', () => {
  it('skips decisions with a non-numeric index and an unknown kind', async () => {
    const wrapper = mount(SystemMapDiscoveryPanel, {
      props: {
        discovery: {
          sources_inspected: [],
          inferred_nodes: [],
          inferred_edges: [],
          skipped: [],
          warnings: [],
          draft_map_id: 'map',
          draft_revision_id: 'rev',
        } as never,
      },
      global: { plugins: [i18n] },
    })

    const vm = wrapper.vm as unknown as { decisions: Record<string, string> }
    vm.decisions['node:not-a-number'] = 'accepted'
    vm.decisions['other:0'] = 'accepted'

    await wrapper.get('[data-testid="system-map-discovery-save"]').trigger('click')

    expect(wrapper.emitted('save')![0][0]).toEqual({ acceptedNodes: [], acceptedEdges: [], rejectedNodes: [], rejectedEdges: [] })
  })

  it('handles a report with no inferred arrays', () => {
    const wrapper = mount(SystemMapDiscoveryPanel, {
      props: {
        discovery: {
          sources_inspected: [],
          skipped: [],
          warnings: [],
          draft_map_id: 'map',
          draft_revision_id: 'rev',
        } as never,
      },
      global: { plugins: [i18n] },
    })

    expect(wrapper.text()).toContain('Suggested nodes (0)')
    expect(wrapper.text()).toContain('Suggested relationships (0)')
  })
})

describe('SystemMapGraphEditor remaining branches', () => {
  const emptyProps = { nodes: [], relationships: [], loopConfigs: [], nodeTypes: NODE_TYPES, relationshipTypes: RELATIONSHIP_TYPES }

  it('removes a node that is only the target of a relationship', async () => {
    const wrapper = mount(SystemMapGraphEditor, {
      props: {
        ...emptyProps,
        nodes: [node(), node({ id: 'n2', node_key: 'connector-2', node_type: 'connector', display_name: 'Connector 2' })],
        relationships: [relationship()],
      } as never,
      global: { plugins: [i18n] },
    })

    await wrapper.get('[data-testid="system-map-remove-node-connector-2"]').trigger('click')

    const emitted = wrapper.emitted('relationships-change')!
    expect(emitted[emitted.length - 1][0]).toEqual([])
  })

  it('ignores drag moves before a drag starts and keyboard moves when read-only', async () => {
    const wrapper = mount(SystemMapGraphEditor, {
      props: { ...emptyProps, nodes: [node()], readOnly: true } as never,
      global: { plugins: [i18n] },
    })

    const vm = wrapper.vm as unknown as {
      onDragMove: (event: PointerEvent) => void
      onMoveKeydown: (event: KeyboardEvent, target: SystemMapNode) => void
    }
    vm.onDragMove(pointerEvent('pointermove', 10, 10) as unknown as PointerEvent)
    vm.onMoveKeydown(new KeyboardEvent('keydown', { key: 'ArrowRight' }), node())

    expect(wrapper.emitted('nodes-change')).toBeFalsy()
  })

  it('removes window listeners when unmounted', () => {
    const removeSpy = vi.spyOn(window, 'removeEventListener')
    const wrapper = mount(SystemMapGraphEditor, {
      props: { ...emptyProps, nodes: [node()] } as never,
      global: { plugins: [i18n] },
    })

    wrapper.unmount()

    expect(removeSpy).toHaveBeenCalledWith('pointermove', expect.any(Function))
    expect(removeSpy).toHaveBeenCalledWith('pointerup', expect.any(Function))
    expect(removeSpy).toHaveBeenCalledWith('pointercancel', expect.any(Function))
    removeSpy.mockRestore()
  })
})

describe('mergeDiscoverySelections rejected edges', () => {
  it('removes an existing relationship that the operator rejected', () => {
    const discovery = {
      sources_inspected: [],
      inferred_nodes: [],
      inferred_edges: [{ source_node_key: 'pipeline-1', target_node_key: 'connector-2', relationship_type: 'flow' }],
      skipped: [],
      warnings: [],
    }
    const existingNodes = [node(), node({ id: 'n2', node_key: 'connector-2', node_type: 'connector', display_name: 'Connector 2' })]

    const merged = mergeDiscoverySelections(existingNodes, [relationship()], discovery as never, {
      acceptedNodes: [],
      acceptedEdges: [],
      rejectedNodes: [],
      rejectedEdges: [0],
    })

    expect(merged.relationships).toHaveLength(0)
  })
})
