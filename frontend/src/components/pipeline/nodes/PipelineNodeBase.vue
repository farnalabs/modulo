<template>
  <div
    :class="[
      'relative rounded-lg px-4 py-2 shadow-sm transition-shadow hover:shadow-md',
      theme.radius,
      theme.border,
      theme.bg,
      selected ? 'ring-2 ring-primary' : '',
    ]"
    :data-testid="testId"
  >
    <div class="flex items-center justify-between gap-2">
      <div :class="['flex items-center gap-1.5 min-w-0 font-brand-mono text-[11px] font-medium lowercase tracking-wide', theme.labelColour]">
        <slot name="glyph"><span>{{ glyph }}</span></slot>
        <slot name="extra-badges" />
      </div>
      <slot name="actions" />
    </div>
    <div
      class="text-sm font-semibold text-foreground truncate"
      :title="description || undefined"
    >
      {{ label || fallbackLabel }}
    </div>
    <div v-if="description" class="mt-0.5 line-clamp-2 text-xs text-muted-foreground">
      {{ description }}
    </div>
    <PipelineNodeBadges :node="data" :connectors="connectors" />
    <PipelineGovernanceVisibility :node="data" :team-names="teamNames" mode="node" />
    <slot name="extra-content" />
  </div>
</template>

<script setup lang="ts">
import { computed, inject, ref, type Ref } from 'vue'
import { useI18n } from 'vue-i18n'
import PipelineGovernanceVisibility from '../PipelineGovernanceVisibility.vue'
import PipelineNodeBadges from '../PipelineNodeBadges.vue'
import {
  PIPELINE_CONNECTORS_KEY,
  PIPELINE_TEAM_NAMES_KEY,
  getNodeRegistryEntry,
  getNodeType,
} from './NodeRegistry'

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
  /** Override the default kind glyph (e.g. a shape icon plus its label). */
  glyph: (props: Record<string, never>) => unknown
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

// Same contract for the connector list the semantic badges resolve against:
// the editor provides its loaded connectors; an isolated render shows the
// binding's own fields and no derived connector detail.
const connectors = inject<Ref<Array<Record<string, any>>>>(
  PIPELINE_CONNECTORS_KEY,
  ref<Array<Record<string, any>>>([]),
)
</script>
