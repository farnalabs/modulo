<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'

const props = defineProps<{
  /** The gate config's human description (null for legacy gates → muted fallback). */
  description?: string | null
  /** The fire-time briefing bundle persisted on the claim row (FAR-613). */
  context?: Record<string, unknown> | null
}>()

const { t } = useI18n()

const showDetails = ref(false)

const ctx = computed<Record<string, unknown> | null>(() =>
  props.context && typeof props.context === 'object' && !Array.isArray(props.context) ? props.context : null,
)

const description = computed(() => {
  const value = props.description
  return typeof value === 'string' && value.trim() ? value : null
})

function asString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null
}

const trigger = computed(() => {
  const value = ctx.value?.trigger
  if (value === 'node') return t('components.HitlBriefing.trigger_node')
  if (value === 'condition') return t('components.HitlBriefing.trigger_condition')
  if (value === 'unknown') return t('components.HitlBriefing.trigger_unknown')
  return null
})

const condition = computed(() => asString(ctx.value?.condition))

/**
 * FAR-688: the matched condition value as PRIMARY briefing evidence —
 * what the condition expression actually evaluated to at fire time.
 */
interface ConditionResult {
  expression: string
  value: string
}

const conditionResult = computed<ConditionResult | null>(() => {
  const raw = ctx.value?.condition_result
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const entry = raw as Record<string, unknown>
  const expression = asString(entry.expression)
  const value = asString(entry.value)
  return expression && value ? { expression, value } : null
})

/**
 * FAR-859: the thing under review — resolved at gate-fire time against the
 * run state. Rendered FIRST, above artifacts, as the reviewer's primary focus.
 */
const subject = computed(() => asString(ctx.value?.subject))

/**
 * FAR-859: approve/reject consequences — where the run goes next.
 */
interface ConsequenceTarget {
  node_id: string
  label: string | null
}

interface Consequences {
  approve?: ConsequenceTarget
  reject?: ConsequenceTarget
}

const consequences = computed<Consequences | null>(() => {
  const raw = ctx.value?.consequences
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const entry = raw as Record<string, unknown>
  const result: Consequences = {}
  if (entry.approve && typeof entry.approve === 'object' && !Array.isArray(entry.approve)) {
    const a = entry.approve as Record<string, unknown>
    result.approve = { node_id: asString(a.node_id) ?? '', label: asString(a.label) }
  }
  if (entry.reject && typeof entry.reject === 'object' && !Array.isArray(entry.reject)) {
    const r = entry.reject as Record<string, unknown>
    result.reject = { node_id: asString(r.node_id) ?? '', label: asString(r.label) }
  }
  return result.approve || result.reject ? result : null
})

const consequenceSummary = computed(() => {
  if (!consequences.value) return null
  const parts: string[] = []
  const c = consequences.value
  if (c.approve) {
    const name = c.approve.label || c.approve.node_id
    parts.push(t('components.HitlBriefing.consequence_approve', { target: name }))
  }
  if (c.reject) {
    const name = c.reject.label || c.reject.node_id
    parts.push(t('components.HitlBriefing.consequence_reject', { target: name }))
  }
  return parts.length > 0 ? parts.join('; ') : null
})

const sourceNode = computed(() => {
  const id = asString(ctx.value?.source_node_id)
  const label = asString(ctx.value?.source_node_label)
  return label ? `${label} (${id})` : id
})

const reason = computed(() => asString(ctx.value?.reason))

const pipelineName = computed(() => asString(ctx.value?.pipeline_name))

interface ArtifactEntry {
  node_id: string
  summary: string
}

const artifacts = computed<ArtifactEntry[]>(() => {
  const raw = ctx.value?.artifacts
  if (!Array.isArray(raw)) return []
  return raw
    .filter((entry): entry is Record<string, unknown> => entry !== null && typeof entry === 'object')
    .map((entry) => ({
      node_id: asString(entry.node_id) ?? '',
      summary: asString(entry.summary) ?? '',
    }))
    .filter((entry) => entry.node_id !== '' || entry.summary !== '')
})

const hasDetails = computed(
  () =>
    Boolean(
      subject.value ||
        consequences.value ||
        trigger.value ||
        condition.value ||
        conditionResult.value ||
        sourceNode.value ||
        reason.value ||
        pipelineName.value ||
        artifacts.value.length > 0,
    ),
)
</script>

<template>
  <section
    data-testid="hitl-briefing"
    class="rounded-lg border bg-muted/30 p-3"
    :aria-label="$t('components.HitlBriefing.title')"
  >
    <h4 class="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
      {{ $t('components.HitlBriefing.title') }}
    </h4>
    <p v-if="description" data-testid="hitl-briefing-description" class="text-sm text-foreground">
      {{ description }}
    </p>
    <p v-else data-testid="hitl-briefing-description-fallback" class="text-sm italic text-muted-foreground">
      {{ $t('components.HitlBriefing.no_description') }}
    </p>
    <!-- FAR-859: subject rendered FIRST — the thing the reviewer is deciding on. -->
    <div
      v-if="subject"
      data-testid="hitl-briefing-subject"
      class="mt-2 rounded border border-primary/20 bg-primary/5 p-2"
    >
      <dt class="text-xs font-semibold text-muted-foreground">{{ $t('components.HitlBriefing.subject') }}</dt>
      <dd class="mt-1 break-all font-mono text-sm text-foreground">{{ subject }}</dd>
    </div>
    <!-- FAR-859: consequences summary — one-line routing information. -->
    <p
      v-if="consequenceSummary"
      data-testid="hitl-briefing-consequences"
      class="mt-2 text-xs text-muted-foreground"
    >
      {{ consequenceSummary }}
    </p>
    <template v-if="hasDetails">
      <button
        type="button"
        data-testid="hitl-briefing-toggle"
        class="mt-2 inline-flex items-center gap-1 rounded-md px-1 py-0.5 text-xs font-medium text-primary hover:bg-primary/10"
        :aria-expanded="showDetails ? 'true' : 'false'"
        @click="showDetails = !showDetails"
      >
        {{ showDetails ? $t('components.HitlBriefing.hide_details') : $t('components.HitlBriefing.show_details') }}
      </button>
      <dl v-if="showDetails" data-testid="hitl-briefing-details" class="mt-2 space-y-2 text-sm">
        <div v-if="trigger" class="flex justify-between gap-3">
          <dt class="flex-shrink-0 text-muted-foreground">{{ $t('components.HitlBriefing.trigger') }}</dt>
          <dd class="text-right">{{ trigger }}</dd>
        </div>
        <div v-if="pipelineName" class="flex justify-between gap-3">
          <dt class="flex-shrink-0 text-muted-foreground">{{ $t('components.HitlBriefing.pipeline') }}</dt>
          <dd class="text-right">{{ pipelineName }}</dd>
        </div>
        <div v-if="sourceNode" class="flex justify-between gap-3">
          <dt class="flex-shrink-0 text-muted-foreground">{{ $t('components.HitlBriefing.source_node') }}</dt>
          <dd class="text-right font-mono text-xs">{{ sourceNode }}</dd>
        </div>
        <div v-if="condition" class="flex justify-between gap-3">
          <dt class="flex-shrink-0 text-muted-foreground">{{ $t('components.HitlBriefing.condition') }}</dt>
          <dd class="break-all text-right font-mono text-xs">{{ condition }}</dd>
        </div>
        <!-- FAR-688: PRIMARY evidence — the value the condition matched at fire
             time, above the supplementary artifact excerpts. When the snapshot
             condition row is absent (unresolvable config), the payload's own
             expression is still shown — the builder guarantees it carries the
             best-known expression. -->
        <div v-if="conditionResult" class="rounded bg-background p-2" data-testid="hitl-briefing-condition-result">
          <dt class="text-muted-foreground">{{ $t('components.HitlBriefing.condition_evaluated') }}</dt>
          <dd
            v-if="!condition"
            data-testid="hitl-briefing-condition-result-expression"
            class="break-all font-mono text-xs text-muted-foreground"
          >
            {{ conditionResult.expression }}
          </dd>
          <dd class="mt-1 break-all font-mono text-xs text-foreground">
            {{ conditionResult.value }}
          </dd>
        </div>
        <div v-if="reason" class="flex justify-between gap-3">
          <dt class="flex-shrink-0 text-muted-foreground">{{ $t('components.HitlBriefing.reason') }}</dt>
          <dd class="text-right">{{ reason }}</dd>
        </div>
        <div v-if="artifacts.length > 0" class="space-y-1">
          <dt class="text-muted-foreground">{{ $t('components.HitlBriefing.artifacts') }}</dt>
          <dd
            v-for="(artifact, index) in artifacts"
            :key="`${artifact.node_id}-${index}`"
            class="rounded bg-background p-2"
          >
            <span class="font-mono text-xs text-muted-foreground">{{ artifact.node_id }}</span>
            <pre class="mt-1 overflow-x-auto whitespace-pre-wrap break-all font-mono text-xs text-foreground">{{ artifact.summary }}</pre>
          </dd>
        </div>
        <div v-else-if="ctx" class="text-xs italic text-muted-foreground">
          {{ $t('components.HitlBriefing.artifacts_empty') }}
        </div>
      </dl>
    </template>
  </section>
</template>
