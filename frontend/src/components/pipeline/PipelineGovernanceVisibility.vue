<script setup lang="ts">
import { computed } from 'vue'
import { ShieldCheck } from '@lucide/vue'
import { useI18n } from 'vue-i18n'

interface GovernanceRecord {
  [key: string]: unknown
}

const props = withDefaults(defineProps<{
  pipeline?: GovernanceRecord | null
  node?: GovernanceRecord | null
  /** Team id → name, from the teams ALREADY loaded by the editor. Never a claim. */
  teamNames?: Record<string, string>
  mode: 'toolbar' | 'node'
}>(), {
  pipeline: null,
  node: null,
  teamNames: () => ({}),
})

const { t } = useI18n()

const AUTONOMY_LEVEL_KEYS: Record<string, string> = {
  manual_approval: 'views.PipelineEditorView.autonomy_manual_approval',
  notify_on_complete: 'views.PipelineEditorView.autonomy_notify_on_complete',
  fully_autonomous: 'views.PipelineEditorView.autonomy_fully_autonomous',
}

function autonomyLabel(level: unknown): string | null {
  if (typeof level !== 'string' || !level) return null
  return t(AUTONOMY_LEVEL_KEYS[level] ?? 'views.PipelineEditorView.autonomy_panel_unknown')
}

const autonomyLevel = computed(() => {
  const max = autonomyLabel(props.pipeline?.max_autonomy_level)
  return max ?? autonomyLabel(props.pipeline?.default_autonomy_level)
})
const autonomyInherited = computed(() => !props.pipeline?.max_autonomy_level && Boolean(autonomyLevel.value))
const autonomyTitle = computed(() =>
  autonomyInherited.value
    ? t('views.PipelineEditorView.governance_autonomy_inherited_hint')
    : t('views.PipelineEditorView.governance_autonomy_hint'),
)

const threshold = computed(() => {
  const value = props.pipeline?.circuit_breaker_threshold
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null
})
const concurrency = computed(() => {
  const value = props.pipeline?.max_concurrent_runs
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null
})
const hasHitlConfig = computed(() => props.node?.hitl_config && typeof props.node.hitl_config === 'object' && !Array.isArray(props.node.hitl_config))
const hitlConfig = computed(() => (hasHitlConfig.value ? props.node?.hitl_config as GovernanceRecord : null))
const humanOnly = computed(() => typeof hitlConfig.value?.human_only === 'boolean' ? hitlConfig.value.human_only : null)
const teamName = computed(() => {
  const config = hitlConfig.value
  if (!config) return null
  const inline = config.required_team_name ?? config.claim_team_name ?? config.team_name
  if (typeof inline === 'string' && inline.trim()) return inline.trim()
  // The graph contract carries the team ID; resolve the name only from the
  // teams the editor has actually loaded, never from a guess.
  const id = config.required_team_id ?? config.claim_team_id
  if (typeof id !== 'string' || !id) return null
  return props.teamNames[id] ?? null
})
const teamAssigned = computed(() => {
  const config = hitlConfig.value
  return Boolean(config?.required_team_id ?? config?.claim_team_id) && !teamName.value
})
const expiryMinutes = computed(() => {
  const config = hitlConfig.value
  const value = config?.claim_expiry_minutes ?? config?.claim_expiry_min
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null
})
const expiryLabel = computed(() => {
  if (expiryMinutes.value == null) return null
  const minutes = expiryMinutes.value
  if (minutes % 60 === 0) return t('views.PipelineEditorView.governance_expiry_hours', { count: minutes / 60 })
  if (minutes > 60) return t('views.PipelineEditorView.governance_expiry_hours_minutes', { hours: Math.floor(minutes / 60), minutes: minutes % 60 })
  return t('views.PipelineEditorView.governance_expiry_minutes', { count: minutes })
})
const policyGate = computed(() => props.node?.policy_gate && typeof props.node.policy_gate === 'object' && !Array.isArray(props.node.policy_gate))
// Restrained progressive disclosure: the node row is driven entirely by the
// graph contract, so it is absent whenever the node declares no gate/binding
// rather than rendering an empty badge container on every card.
const hasNodeDetails = computed(() => humanOnly.value !== null || teamName.value !== null || teamAssigned.value || expiryLabel.value !== null || policyGate.value)
</script>

<template>
  <div v-if="mode === 'toolbar'" class="flex min-w-0 flex-wrap items-center gap-1.5" role="group" :aria-label="$t('views.PipelineEditorView.governance_visibility_label')" data-testid="pipeline-editor-governance-visibility">
    <output
      v-if="autonomyLevel"
      class="badge badge-status-primary whitespace-nowrap"
      :title="autonomyTitle"
      :aria-label="$t('views.PipelineEditorView.governance_autonomy_label', { level: autonomyLevel })"
      data-testid="pipeline-editor-governance-autonomy"
    >
      {{ $t('views.PipelineEditorView.governance_autonomy_chip', { level: autonomyLevel }) }}
    </output>
    <output
      v-if="concurrency"
      class="badge badge-status-muted whitespace-nowrap"
      :title="$t('views.PipelineEditorView.governance_capacity_hint')"
      :aria-label="$t('views.PipelineEditorView.governance_capacity_label', { count: concurrency })"
      data-testid="pipeline-editor-governance-capacity"
    >
      {{ $t('views.PipelineEditorView.governance_capacity_chip', { count: concurrency }) }}
    </output>
    <output
      v-if="threshold"
      class="badge badge-status-muted whitespace-nowrap"
      :title="$t('views.PipelineEditorView.governance_breaker_hint')"
      :aria-label="$t('views.PipelineEditorView.governance_breaker_label', { amount: threshold.toFixed(2) })"
      data-testid="pipeline-editor-governance-breaker"
    >
      {{ $t('views.PipelineEditorView.governance_breaker_chip', { amount: threshold.toFixed(2) }) }}
    </output>
  </div>

  <div v-else-if="mode === 'node' && hasNodeDetails" class="mt-1 flex flex-wrap gap-1" data-testid="pipeline-node-governance">
    <span
      v-if="humanOnly === true"
      class="rounded bg-rose-500/10 px-1.5 py-0.5 text-[10px] font-medium text-rose-600 dark:text-rose-300"
      :aria-label="$t('views.PipelineEditorView.governance_human_only')"
      data-testid="pipeline-node-governance-human-only"
    >{{ $t('views.PipelineEditorView.governance_human_only') }}</span>
    <span
      v-else-if="humanOnly === false"
      class="rounded bg-blue-500/10 px-1.5 py-0.5 text-[10px] font-medium text-blue-600 dark:text-blue-300"
      :aria-label="$t('views.PipelineEditorView.governance_agent_may_approve')"
      data-testid="pipeline-node-governance-agent-approval"
    >{{ $t('views.PipelineEditorView.governance_agent_may_approve') }}</span>
    <span
      v-if="teamName || teamAssigned"
      class="rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground"
      :aria-label="$t('views.PipelineEditorView.governance_team_label', { team: teamName ?? $t('views.PipelineEditorView.governance_team_assigned') })"
      data-testid="pipeline-node-governance-team"
    >{{ $t('views.PipelineEditorView.governance_team', { team: teamName ?? $t('views.PipelineEditorView.governance_team_assigned') }) }}</span>
    <span
      v-if="expiryLabel"
      class="rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground"
      :aria-label="$t('views.PipelineEditorView.governance_expiry_label', { expiry: expiryLabel })"
      data-testid="pipeline-node-governance-expiry"
    >{{ $t('views.PipelineEditorView.governance_expiry', { expiry: expiryLabel }) }}</span>
    <span
      v-if="policyGate"
      class="inline-flex items-center gap-1 rounded bg-amber-500/10 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 dark:text-amber-300"
      :aria-label="$t('views.PipelineEditorView.governance_guardrail')"
      :title="$t('views.PipelineEditorView.governance_guardrail_hint')"
      data-testid="pipeline-node-governance-guardrail"
    ><ShieldCheck class="h-3 w-3" aria-hidden="true" />{{ $t('views.PipelineEditorView.governance_guardrail') }}</span>
  </div>
</template>
