<template>
  <div class="pointer-events-none absolute inset-0">
    <div v-if="hasHitl" class="absolute -translate-y-4 translate-x-2 flex max-w-56 flex-wrap gap-1">
      <span class="rounded bg-warning/20 px-1.5 py-0.5 text-[10px] font-medium text-warning-text" data-testid="edge-badge-hitl">
        {{ t('views.PipelineEditorView.node_hitl_badge') }}
      </span>
      <!-- FAR-2462: the gate's real mode is additive to the kind badge, so the
           human-only marker and the eval condition render only when declared. -->
      <span v-if="humanOnly" class="rounded bg-warning/20 px-1.5 py-0.5 text-[10px] font-medium text-warning-text" data-testid="pipeline-edge-semantic-hitl">
        · {{ t('views.PipelineEditorView.semantic_badge_human_only') }}
      </span>
      <span v-if="evalName" class="rounded bg-purple-500/10 px-1.5 py-0.5 text-[10px] font-medium text-purple-700 dark:text-purple-300" data-testid="pipeline-edge-semantic-eval">
        {{ t('views.PipelineEditorView.semantic_badge_eval') }} · {{ evalName }}
      </span>
    </div>
    <div v-if="isLoop" class="absolute translate-y-4 translate-x-2">
      <span class="rounded bg-blue-100 px-1.5 py-0.5 text-[10px] font-medium text-blue-600 dark:bg-blue-900 dark:text-blue-300" data-testid="edge-badge-loop">
        {{ t('views.PipelineEditorView.loop') }}{{ maxIterations ? ` (${maxIterations})` : '' }}
      </span>
    </div>
    <div v-if="isLlm" class="absolute translate-y-4 translate-x-2">
      <span class="rounded bg-purple-100 px-1.5 py-0.5 text-[10px] font-medium text-purple-600 dark:bg-purple-900 dark:text-purple-300" data-testid="edge-badge-llm">
        {{ t('views.PipelineEditorView.llm_badge') }}{{ routingLabel ? `: ${routingLabel}` : '' }}
      </span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

interface EdgeReviewConfig {
  description?: string
  human_only?: boolean
  eval_condition?: { eval_name?: string }
}

interface EdgeData {
  hitl_review_config?: EdgeReviewConfig | null
  edge_type?: string
  max_iterations?: number
  routing_label?: string
}

const props = defineProps<{ data?: EdgeData | null }>()
const { t } = useI18n()
const data = computed(() => props.data ?? {})
const hasHitl = computed(() => Boolean(data.value.hitl_review_config))
const humanOnly = computed(() => data.value.hitl_review_config?.human_only === true)
const evalName = computed(() => data.value.hitl_review_config?.eval_condition?.eval_name ?? '')
const isLoop = computed(() => data.value.edge_type === 'loop')
const isLlm = computed(() => data.value.edge_type === 'llm')
const maxIterations = computed(() => data.value.max_iterations ?? 0)
const routingLabel = computed(() => data.value.routing_label ?? '')
</script>
