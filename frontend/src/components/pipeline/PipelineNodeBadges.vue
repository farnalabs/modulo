<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

/**
 * Semantic node badges for the pipeline canvas (FAR-2462).
 *
 * Every badge is DERIVED from a field that already exists on the pipeline
 * graph contract (`PipelineGraphNode` in lib/api/schema.ts) — never a
 * hand-authored label and never an inferred claim:
 *
 *  - connector  : `connector_binding.instance_id` is set (a real binding)
 *  - dispatch   : `connector_binding.resource` / `.dispatch_action`
 *  - operation  : `connector_binding.operation` (query | write | dispatch)
 *  - router     : `router_config.rules.length` (router node only)
 *  - hitl       : `hitl_config` present on a hitl node
 *  - eval       : `hitl_config.eval_condition` (an eval-referenced gate)
 *
 * A node with none of these renders nothing at all, so the canvas stays
 * quiet and the extra density only appears where a real binding exists.
 * The human-readable detail is additive and hidden in the tooltip when the
 * card is too narrow; the badge kind alone is still readable.
 */
type BadgeKind = 'connector' | 'dispatch' | 'operation' | 'router' | 'hitl' | 'eval'

type SemanticBadge = {
  kind: BadgeKind
  label: string
  detail?: string
  testId: string
}

const props = defineProps<{
  node: Record<string, any>
  connectors?: Array<Record<string, any>>
}>()

const { t } = useI18n()

function nonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0
}

function isPlainObject(value: unknown): value is Record<string, any> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function connectorBadges(
  binding: Record<string, any> | null,
  connectors: Array<Record<string, any>> | undefined,
): SemanticBadge[] {
  if (!binding || !nonEmptyString(binding.instance_id)) {
    return []
  }

  const result: SemanticBadge[] = [
    {
      kind: 'connector',
      label: t('views.PipelineEditorView.semantic_badge_connector'),
      detail: nonEmptyString(binding.type) ? binding.type : undefined,
      testId: 'pipeline-node-semantic-connector',
    },
  ]

  const connector = connectors?.find((item) => item.id === binding.instance_id)
  const allowedOperations = Array.isArray(connector?.allowed_operations)
    ? connector.allowed_operations.filter(nonEmptyString)
    : []

  if (allowedOperations.length > 0) {
    result.push({
      kind: 'operation',
      label: t('views.PipelineEditorView.semantic_badge_allowed_operations'),
      detail: allowedOperations.join(', '),
      testId: 'pipeline-node-semantic-allowed-operations',
    })
  }

  if (nonEmptyString(binding.operation) && binding.operation !== 'dispatch') {
    result.push({
      kind: 'operation',
      label: t('views.PipelineEditorView.semantic_badge_operation'),
      detail: binding.operation,
      testId: 'pipeline-node-semantic-operation',
    })
  }

  return result
}

function dispatchBadges(
  nodeType: string | undefined,
  binding: Record<string, any> | null,
): SemanticBadge[] {
  if (nodeType !== 'dispatch' || !binding) {
    return []
  }

  const parts = [binding.resource, binding.dispatch_action]
    .filter(nonEmptyString)
    .join(' · ')
  if (parts.length === 0) {
    return []
  }

  return [
    {
      kind: 'dispatch',
      label: t('views.PipelineEditorView.semantic_badge_dispatch'),
      detail: parts,
      testId: 'pipeline-node-semantic-dispatch',
    },
  ]
}

function routerBadges(
  nodeType: string | undefined,
  routerConfig: unknown,
): SemanticBadge[] {
  if (nodeType !== 'router' || !isPlainObject(routerConfig)) {
    return []
  }

  const rules = routerConfig.rules
  if (!Array.isArray(rules) || rules.length === 0) {
    return []
  }

  return [
    {
      kind: 'router',
      label: t('views.PipelineEditorView.semantic_badge_router'),
      detail: t('views.PipelineEditorView.semantic_badge_rule_count', { count: rules.length }, rules.length),
      testId: 'pipeline-node-semantic-router',
    },
  ]
}

function hitlBadges(
  nodeType: string | undefined,
  hitlConfigInput: unknown,
): SemanticBadge[] {
  if (nodeType !== 'hitl' || !isPlainObject(hitlConfigInput)) {
    return []
  }

  const hitlConfig = hitlConfigInput
  const result: SemanticBadge[] = [
    {
      kind: 'hitl',
      label: t('views.PipelineEditorView.semantic_badge_hitl'),
      detail: nonEmptyString(hitlConfig.label) ? hitlConfig.label : undefined,
      testId: 'pipeline-node-semantic-hitl',
    },
  ]

  const evalCondition = isPlainObject(hitlConfig.eval_condition) ? hitlConfig.eval_condition : null
  if (evalCondition && nonEmptyString(evalCondition.eval_name)) {
    result.push({
      kind: 'eval',
      label: t('views.PipelineEditorView.semantic_badge_eval'),
      detail: evalCondition.eval_name,
      testId: 'pipeline-node-semantic-eval',
    })
  }

  return result
}

const badges = computed<SemanticBadge[]>(() => {
  const node = props.node
  const binding = isPlainObject(node?.connector_binding) ? node.connector_binding : null

  return [
    ...connectorBadges(binding, props.connectors),
    ...dispatchBadges(node?.node_type, binding),
    ...routerBadges(node?.node_type, node?.router_config),
    ...hitlBadges(node?.node_type, node?.hitl_config),
  ]
})
</script>

<template>
  <div
    v-if="badges.length > 0"
    class="mt-1 flex max-w-56 flex-wrap gap-1"
    data-testid="pipeline-node-semantic-badges"
  >
    <span
      v-for="badge in badges"
      :key="badge.testId"
      class="inline-flex max-w-full items-center gap-1 rounded bg-background/70 px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground ring-1 ring-inset ring-border/70"
      :data-testid="badge.testId"
      :title="badge.detail ? `${badge.label}: ${badge.detail}` : badge.label"
    >
      <span class="truncate">{{ badge.label }}</span>
      <span v-if="badge.detail" class="max-w-32 truncate font-normal">{{ badge.detail }}</span>
    </span>
  </div>
</template>
