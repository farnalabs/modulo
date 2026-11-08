<template>
  <div
    :class="[
      'relative min-w-[160px] max-w-[280px] rounded-lg px-4 py-2.5 shadow-sm transition-shadow hover:shadow-md',
      'font-brand-mono text-[11px] font-medium lowercase tracking-wide',
      theme.border,
      theme.bg,
      selected ? 'ring-2 ring-primary' : '',
    ]"
    :data-testid="testId"
  >
    <div class="flex items-center justify-between gap-2">
      <div class="flex items-center gap-1.5 min-w-0">
        <span :class="theme.labelColour">{{ glyph }}</span>
        <slot name="extra-badges" />
      </div>
      <slot name="actions" />
    </div>
    <div
      class="mt-1 text-sm font-semibold text-foreground truncate"
      :title="description || undefined"
    >
      {{ label || fallbackLabel }}
    </div>
    <div v-if="description" class="mt-0.5 line-clamp-2 text-xs text-muted-foreground">
      {{ description }}
    </div>
    <PipelineGovernanceVisibility :node="data" :team-names="teamNames" mode="node" />
    <slot name="extra-content" />
  </div>
</template>

<script setup lang="ts">
import { computed, inject } from 'vue'
import { useI18n } from 'vue-i18n'
import PipelineGovernanceVisibility from '../PipelineGovernanceVisibility.vue'
import { PIPELINE_TEAM_NAMES_KEY, getNodeRegistryEntry, getNodeType } from './NodeRegistry'

interface Props {
  /** The node data from Vue Flow. */
  data: Record<string, unknown>
  /** Whether the node is currently selected. */
  selected?: boolean
  /** Unique node id. */
  id: string
  /** Optional explicit test id for product-map coverage. */
  testId?: string
}

const props = defineProps<Props>()
defineSlots<{
  /** Additional badges to show next to the glyph. */
  'extra-badges': (props: Record<string, never>) => unknown
  /** Additional actions (e.g. expand button for composites). */
  actions: (props: Record<string, never>) => unknown
  /** Extra content below the description. */
  'extra-content': (props: Record<string, never>) => unknown
}>()

const { t } = useI18n()

const nodeType = computed(() => getNodeType(props.data))

const testId = computed(() => props.testId ?? `pipeline-node-${nodeType.value}`)

const registryEntry = computed(() => getNodeRegistryEntry(nodeType.value))
const theme = computed(() => registryEntry.value.theme)
const glyph = computed(() => t(registryEntry.value.glyphKey))
const fallbackLabel = computed(() => t(registryEntry.value.fallbackLabelKey))

const label = computed(() => props.data.label as string | undefined)
const description = computed(() => props.data.description as string | undefined)

// The editor provides its already-loaded team map; cards rendered in isolation
// (tests, storybook) resolve no names rather than inventing one.
const teamNames = inject(
  PIPELINE_TEAM_NAMES_KEY,
  computed<Record<string, string>>(() => ({})),
)
</script>
