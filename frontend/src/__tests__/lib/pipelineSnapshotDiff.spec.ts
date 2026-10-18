import { describe, it, expect } from 'vitest'
import {
  SNAPSHOT_CHANGE_CLASSES,
  hasStructuralChanges,
  isBlockingSeverity,
  readBreakingChanges,
  readImpactedNodes,
  readNodeSignatureNodeIds,
  readUnchangedNodeIds,
  severityLabel,
  shortId,
  type SnapshotDiffResponse,
} from '../../lib/pipelineSnapshotDiff'

// FAR-1893 — coverage for the ONE shared interpretation of the snapshot-diff
// response. Both the version timeline and the compare view bind to this module,
// so these tests pin the field names, the change-class enumeration, the
// `port_changes` `kind` discriminator (FAR-1894) and the severity/id display
// rules that used to be duplicated (and had diverged).

const t = (key: string): string => `label:${key}`

function diff(overrides: Partial<SnapshotDiffResponse> = {}): SnapshotDiffResponse {
  return {
    snapshot_a: {
      version: 1,
      graph: {
        nodes: [
          { id: 'n1', label: 'Start' },
          { id: 'n2', label: 'Steady' },
          { id: 'n3', label: 'Old' },
        ],
        edges: [],
      },
    },
    snapshot_b: {
      version: 2,
      graph: {
        nodes: [
          { id: 'n1', label: 'Start' },
          { id: 'n2', label: 'Steady' },
          { id: 'n4', label: 'New' },
        ],
        edges: [],
      },
    },
    nodes_added: [],
    nodes_removed: [],
    nodes_modified: [],
    edges_added: [],
    edges_removed: [],
    edges_modified: [],
    semantic: { port_changes: [], impacted_nodes: [], breaking_changes: [] },
    ...overrides,
  }
}

describe('SNAPSHOT_CHANGE_CLASSES', () => {
  it('enumerates exactly the six structural change classes the endpoint reports', () => {
    expect([...SNAPSHOT_CHANGE_CLASSES]).toEqual([
      'nodes_added',
      'nodes_removed',
      'nodes_modified',
      'edges_added',
      'edges_removed',
      'edges_modified',
    ])
  })
})

describe('hasStructuralChanges', () => {
  it('is false for a missing or all-empty diff', () => {
    expect(hasStructuralChanges(null)).toBe(false)
    expect(hasStructuralChanges(undefined)).toBe(false)
    expect(hasStructuralChanges(diff())).toBe(false)
  })

  it('is true when ANY change class is non-empty', () => {
    for (const changeClass of SNAPSHOT_CHANGE_CLASSES) {
      const populated = diff({ [changeClass]: [{ id: 'x' }] })
      expect(hasStructuralChanges(populated)).toBe(true)
    }
  })
})

describe('semantic layer readers', () => {
  it('defaults breaking changes and impacted nodes to empty lists', () => {
    expect(readBreakingChanges(null)).toHaveLength(0)
    expect(readImpactedNodes(null)).toHaveLength(0)
    expect(readBreakingChanges({})).toHaveLength(0)
    expect(readImpactedNodes({})).toHaveLength(0)
  })

  it('reads the semantic layer verbatim', () => {
    const payload = diff({
      semantic: {
        breaking_changes: [{ severity: 'block', reason: 'output port removed' }],
        impacted_nodes: ['n2', 'n4'],
        port_changes: [],
      },
    })
    expect(readBreakingChanges(payload)).toHaveLength(1)
    expect(readImpactedNodes(payload)).toEqual(['n2', 'n4'])
  })
})

describe('readNodeSignatureNodeIds', () => {
  it('counts a node_signature change on a node present in both versions', () => {
    const payload = diff({
      semantic: {
        port_changes: [
          { kind: 'node_signature', node_id: 'n1', direction: 'output', port: 'out', change: 'removed' },
        ],
      },
    })
    expect(readNodeSignatureNodeIds(payload)).toEqual(['n1'])
  })

  it('does not treat an edge_port change as a node signature change', () => {
    const payload = diff({
      semantic: {
        port_changes: [
          {
            kind: 'edge_port',
            node_id: 'n1',
            direction: 'output',
            port: 'out',
            change: 'edge_source_port_changed',
          },
        ],
      },
    })
    expect(readNodeSignatureNodeIds(payload)).toHaveLength(0)
  })

  it('counts a node once even when several of its ports changed', () => {
    const payload = diff({
      semantic: {
        port_changes: [
          { kind: 'node_signature', node_id: 'n1', direction: 'input', port: 'a', change: 'added' },
          { kind: 'node_signature', node_id: 'n1', direction: 'output', port: 'b', change: 'removed' },
        ],
      },
    })
    expect(readNodeSignatureNodeIds(payload)).toEqual(['n1'])
  })

  it('excludes a signature change for a node absent from one frozen graph', () => {
    const payload = diff({
      snapshot_b: undefined,
      semantic: {
        port_changes: [{ kind: 'node_signature', node_id: 'n1', direction: 'output', port: 'out', change: 'removed' }],
      },
    })
    expect(readNodeSignatureNodeIds(payload)).toHaveLength(0)
  })

  it('excludes a node that exists only in the newer version', () => {
    const payload = diff({
      semantic: {
        port_changes: [{ kind: 'node_signature', node_id: 'n4', direction: 'input', port: 'in', change: 'added' }],
      },
    })
    expect(readNodeSignatureNodeIds(payload)).toHaveLength(0)
  })

  it('accepts a legacy entry without a kind discriminator', () => {
    const payload = diff({
      semantic: {
        port_changes: [{ node_id: 'n1', direction: 'output', port: 'out', change: 'removed' }],
      },
    })
    expect(readNodeSignatureNodeIds(payload)).toEqual(['n1'])
  })

  it('excludes an unknown future kind rather than assuming a signature change', () => {
    const payload = diff({
      semantic: {
        port_changes: [{ kind: 'something_new', node_id: 'n1', direction: 'output', port: 'out' }],
      },
    })
    expect(readNodeSignatureNodeIds(payload)).toHaveLength(0)
  })
})

describe('readUnchangedNodeIds', () => {
  it('lists common nodes with no changed field in version-B order', () => {
    const payload = diff({
      nodes_modified: [{ node_id: 'n1', changes: { label: { old: 'Start', new: 'Begin' } } }],
    })
    expect(readUnchangedNodeIds(payload)).toEqual(['n2'])
  })

  it('is empty when the newer frozen graph is absent', () => {
    expect(readUnchangedNodeIds(diff({ snapshot_b: undefined }))).toHaveLength(0)
  })
})

describe('severity display', () => {
  it('maps the known severities to their shared i18n labels', () => {
    expect(severityLabel('block', t)).toBe('label:components.PipelineSnapshotCompare.severity_block')
    expect(severityLabel('warning', t)).toBe('label:components.PipelineSnapshotCompare.severity_warning')
  })

  it('surfaces an unknown severity raw rather than mislabelling it', () => {
    expect(severityLabel('info', t)).toBe('info')
  })

  it('treats an absent severity as a warning', () => {
    expect(severityLabel(undefined, t)).toBe('label:components.PipelineSnapshotCompare.severity_warning')
  })

  it('treats only block as blocking', () => {
    expect(isBlockingSeverity('block')).toBe(true)
    expect(isBlockingSeverity('warning')).toBe(false)
    expect(isBlockingSeverity('info')).toBe(false)
    expect(isBlockingSeverity(undefined)).toBe(false)
  })
})

describe('shortId', () => {
  it('is the app-wide id-shortening rule re-exported for both diff surfaces', () => {
    expect(shortId('node-aaaaaaaa-1')).toBe('#node-aaa')
    expect(shortId('')).toBe('\u2014')
    expect(shortId(null)).toBe('\u2014')
  })
})
