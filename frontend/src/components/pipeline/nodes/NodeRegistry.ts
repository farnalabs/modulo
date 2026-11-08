/**
 * Node type registry for the pipeline editor.
 *
 * Single source of truth for node visual identity: label, glyph (badge text),
 * border style, colour theme, icon, and whether the node has a config panel.
 * Mirrors the StageNode pattern from LifecycleMapEditor but exposes the
 * configuration as data rather than computed properties so the base component
 * can stay generic.
 */

/** Visual theme for a node type. */
export interface NodeTheme {
  /** Border Tailwind classes (e.g. 'border-2 border-warning/60'). */
  border: string
  /** Background Tailwind classes (e.g. 'bg-warning/10'). */
  bg: string
  /** Badge/label text colour Tailwind classes (e.g. 'text-warning-text'). */
  labelColour: string
  /** Optional icon component name (for future icon library integration). */
  icon?: string
}

/** Registry entry for a single node type. */
export interface NodeRegistryEntry {
  /** Internal node type id (matches backend node_type). */
  id: string
  /** Human-readable label for palette/tooltips. */
  label: string
  /** Short glyph/badge text shown on the node (e.g. 'MANUAL', 'AGENT'). */
  glyph: string
  /** Border style classes. */
  borderStyle: string
  /** Colour theme classes. */
  colour: string
  /** Icon identifier. */
  icon: string
  /** Visual theme. */
  theme: NodeTheme
  /** Whether this node type has a config panel in the properties sheet. */
  hasConfigPanel: boolean

  /** Optional fallback label when node.data.label is empty. */
  fallbackLabel?: string
}

/** Node type ids supported by the pipeline editor. */
export type NodeTypeId =
  | 'manual'
  | 'agent'
  | 'router'
  | 'hitl'
  | 'dispatch'
  | 'connector'

/** The registry — frozen at build time. */
export const NODE_REGISTRY: Readonly<Record<NodeTypeId, NodeRegistryEntry>> = {
  manual: {
    id: 'manual',
    label: 'Manual',
    glyph: 'MANUAL',
    borderStyle: 'border-2 border-warning/60',
    colour: 'warning',
    icon: 'pencil',
    theme: {
      border: 'border-2 border-warning/60',
      bg: 'bg-warning/10',
      labelColour: 'text-warning-text',
      icon: 'pencil',
    },
    hasConfigPanel: true,
    fallbackLabel: 'Manual Node',
  },
  agent: {
    id: 'agent',
    label: 'Agent',
    glyph: 'AGENT',
    borderStyle: 'border-2 border-primary/60',
    colour: 'primary',
    icon: 'bot',
    theme: {
      border: 'border-2 border-primary/60',
      bg: 'bg-primary/10',
      labelColour: 'text-primary-text',
      icon: 'bot',
    },
    hasConfigPanel: true,
    fallbackLabel: 'Agent Node',
  },
  router: {
    id: 'router',
    label: 'Router',
    glyph: 'ROUTER',
    borderStyle: 'border-2 border-dashed border-indigo-500/60',
    colour: 'indigo',
    icon: 'git-branch',
    theme: {
      border: 'border-2 border-dashed border-indigo-500/60',
      bg: 'bg-indigo-500/10',
      labelColour: 'text-indigo-600 dark:text-indigo-300',
      icon: 'git-branch',
    },
    hasConfigPanel: false,
    fallbackLabel: 'Router',
  },
  hitl: {
    id: 'hitl',
    label: 'HITL',
    glyph: 'HITL',
    borderStyle: 'border-4 border-double border-rose-500/60',
    colour: 'rose',
    icon: 'shield-check',
    theme: {
      border: 'border-4 border-double border-rose-500/60',
      bg: 'bg-rose-500/10',
      labelColour: 'text-rose-600 dark:text-rose-300',
      icon: 'shield-check',
    },
    hasConfigPanel: true,
    fallbackLabel: 'Human-in-the-loop',
  },
  dispatch: {
    id: 'dispatch',
    label: 'Dispatch',
    glyph: 'DISPATCH',
    borderStyle: 'border-2 border-cyan-500/60',
    colour: 'cyan',
    icon: 'send',
    theme: {
      border: 'border-2 border-cyan-500/60',
      bg: 'bg-cyan-500/10',
      labelColour: 'text-cyan-700 dark:text-cyan-300',
      icon: 'send',
    },
    hasConfigPanel: false,
    fallbackLabel: 'Dispatch Node',
  },
  connector: {
    id: 'connector',
    label: 'Connector',
    glyph: 'CONNECTOR',
    borderStyle: 'border-2 border-teal-500/60',
    colour: 'teal',
    icon: 'plug',
    theme: {
      border: 'border-2 border-teal-500/60',
      bg: 'bg-teal-500/10',
      labelColour: 'text-teal-700 dark:text-teal-300',
      icon: 'plug',
    },
    hasConfigPanel: false,
    fallbackLabel: 'Connector Node',
  },
} as const

/** Ordered list for palette rendering. */
export const NODE_REGISTRY_ORDER: NodeTypeId[] = [
  'manual',
  'agent',
  'router',
  'hitl',
  'dispatch',
  'connector',
]

/**
 * Get a registry entry by node type id.
 * Falls back to 'agent' for unknown types to match legacy behaviour.
 */
export function getNodeRegistryEntry(nodeType: string): NodeRegistryEntry {
  return NODE_REGISTRY[nodeType as NodeTypeId] ?? NODE_REGISTRY.agent
}

/**
 * Get the visual theme for a node type.
 * Convenience helper for components that only need styling.
 */
export function getNodeTheme(nodeType: string): NodeTheme {
  return getNodeRegistryEntry(nodeType).theme
}

/**
 * Check if a node type has a config panel.
 */
export function nodeHasConfigPanel(nodeType: string): boolean {
  return getNodeRegistryEntry(nodeType).hasConfigPanel
}

/**
 * Get the fallback label for a node type.
 */
export function getNodeFallbackLabel(nodeType: string): string {
  return getNodeRegistryEntry(nodeType).fallbackLabel ?? 'Node'
}

/**
 * Get the glyph/badge text for a node type.
 */
export function getNodeGlyph(nodeType: string): string {
  return getNodeRegistryEntry(nodeType).glyph
}

/**
 * Type guard for NodeData to access node_type safely.
 */
export function getNodeType(data: Record<string, unknown>): string {
  return (data.node_type as string | undefined) ?? 'agent'
}
