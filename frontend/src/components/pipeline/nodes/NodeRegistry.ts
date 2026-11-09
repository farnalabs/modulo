import type { ComputedRef, InjectionKey, Ref } from 'vue'

/**
 * Injection key for the editor's already-loaded team id → name map.
 *
 * The extracted node components are rendered through Vue Flow's `nodeTypes`
 * registry, which does not forward arbitrary props, so the editor provides the
 * map (a computed ref) and the cards inject it. Governance pills resolve a HITL
 * gate's team name only from teams the editor has actually loaded.
 */
export const PIPELINE_TEAM_NAMES_KEY: InjectionKey<ComputedRef<Record<string, string>>> =
  Symbol('pipelineTeamNames')

/**
 * Injection key for the editor's already-loaded connector list.
 *
 * Semantic node badges (FAR-2462) resolve a bound node's connector by
 * ``instance_id`` to show that connector's allowed operations. Vue Flow's
 * ``nodeTypes`` registry cannot forward arbitrary props, so the editor provides
 * the list and the cards inject it; isolated renders resolve no connector
 * rather than inventing one.
 */
export const PIPELINE_CONNECTORS_KEY: InjectionKey<Ref<Array<Record<string, any>>>> =
  Symbol('pipelineConnectors')

/**
 * Node type registry for the pipeline editor.
 *
 * Single source of truth for node visual identity: translatable label/glyph
 * keys, colour theme, and whether the node has a config panel. Mirrors the
 * StageNode pattern from LifecycleMapEditor but exposes the configuration as
 * data rather than computed properties so the base component can stay generic.
 *
 * Every user-facing string is an i18n key (never literal English) so the
 * extracted components keep the editor's existing translations.
 */

/** Visual theme for a node type. */
export interface NodeTheme {
  /** Border Tailwind classes (e.g. 'border-2 border-warning/60'). */
  border: string
  /** Background Tailwind classes (e.g. 'bg-warning/10'). */
  bg: string
  /** Badge/label text colour Tailwind classes (e.g. 'text-warning-text'). */
  labelColour: string
  /** Optional extra corner-radius classes (e.g. 'rounded-r-3xl'). */
  radius?: string
}

/** Registry entry for a single node type. */
export interface NodeRegistryEntry {
  /** Internal node type id (matches backend node_type). */
  id: string
  /** vue-i18n key for the human-readable label (palette/tooltips). */
  labelKey: string
  /** vue-i18n key for the short glyph/badge text (e.g. 'MANUAL', 'AGENT'). */
  glyphKey: string
  /** vue-i18n key for the fallback label when node.data.label is empty. */
  fallbackLabelKey: string
  /** Visual theme. */
  theme: NodeTheme
  /** Whether this node type has a config panel in the properties sheet. */
  hasConfigPanel: boolean
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
    labelKey: 'views.PipelineEditorView.manual',
    glyphKey: 'views.PipelineEditorView.node_manual_badge',
    fallbackLabelKey: 'views.PipelineEditorView.manual',
    theme: {
      border: 'border-2 border-warning/60',
      bg: 'bg-warning/10',
      labelColour: 'text-warning-text',
    },
    hasConfigPanel: true,
  },
  agent: {
    id: 'agent',
    labelKey: 'views.PipelineEditorView.agent',
    glyphKey: 'views.PipelineEditorView.node_agent_badge',
    fallbackLabelKey: 'views.PipelineEditorView.agent',
    theme: {
      border: 'border-2 border-primary/60',
      bg: 'bg-primary/10',
      labelColour: 'text-primary-text',
    },
    hasConfigPanel: true,
  },
  router: {
    id: 'router',
    labelKey: 'views.PipelineEditorView.node_router_label',
    glyphKey: 'views.PipelineEditorView.node_router_badge',
    fallbackLabelKey: 'views.PipelineEditorView.node_router_label',
    theme: {
      border: 'border-2 border-dashed border-indigo-500/60',
      bg: 'bg-indigo-500/10',
      labelColour: 'text-indigo-600 dark:text-indigo-300',
      radius: 'rounded-r-3xl',
    },
    hasConfigPanel: false,
  },
  hitl: {
    id: 'hitl',
    labelKey: 'views.PipelineEditorView.node_hitl_label',
    glyphKey: 'views.PipelineEditorView.node_hitl_badge',
    fallbackLabelKey: 'views.PipelineEditorView.node_hitl_label',
    theme: {
      border: 'border-4 border-double border-rose-500/60',
      bg: 'bg-rose-500/10',
      labelColour: 'text-rose-600 dark:text-rose-300',
    },
    hasConfigPanel: true,
  },
  dispatch: {
    id: 'dispatch',
    labelKey: 'views.PipelineEditorView.node_dispatch_label',
    glyphKey: 'views.PipelineEditorView.node_dispatch_badge',
    fallbackLabelKey: 'views.PipelineEditorView.node_dispatch_label',
    theme: {
      border: 'border-2 border-cyan-500/60',
      bg: 'bg-cyan-500/10',
      labelColour: 'text-cyan-700 dark:text-cyan-300',
    },
    hasConfigPanel: false,
  },
  connector: {
    id: 'connector',
    labelKey: 'views.PipelineEditorView.node_connector_label',
    glyphKey: 'views.PipelineEditorView.node_connector_badge',
    fallbackLabelKey: 'views.PipelineEditorView.node_connector_label',
    theme: {
      border: 'border-2 border-teal-500/60',
      bg: 'bg-teal-500/10',
      labelColour: 'text-teal-700 dark:text-teal-300',
    },
    hasConfigPanel: false,
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
 * Type guard for NodeData to access node_type safely.
 */
export function getNodeType(data: Record<string, unknown>): string {
  return (data.node_type as string | undefined) ?? 'agent'
}
