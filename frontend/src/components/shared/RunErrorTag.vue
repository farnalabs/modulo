<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { errorCodeLabel } from '../../utils/runUtils'

const props = defineProps<{
  code?: string | null
  detail?: string | null
}>()

const { t } = useI18n()

// Error-class → pill styling. The first dotted segment of a canonical code is
// its class (agent / harness / sandbox / node / connector / capacity / eval /
// config / contract / run). Unknown classes fall back to muted.
const CLASS_STYLES: Record<string, string> = {
  agent: 'bg-destructive/10 text-destructive',
  harness: 'bg-muted text-muted-foreground',
  sandbox: 'bg-warning/10 text-warning',
  node: 'bg-warning/10 text-warning',
  connector: 'bg-warning/10 text-warning',
  capacity: 'bg-primary/10 text-primary',
  provider: 'bg-warning/10 text-warning',
  eval: 'bg-destructive/10 text-destructive',
  config: 'bg-warning/10 text-warning',
  contract: 'bg-warning/10 text-warning',
  run: 'bg-muted text-muted-foreground',
}

const normalized = computed(() => props.code ?? '')
const label = computed(() => errorCodeLabel(props.code, t))
const pillClass = computed(() => {
  const cls = normalized.value.split('.')[0]
  return CLASS_STYLES[cls] ?? 'bg-muted text-muted-foreground'
})
// Accessible description: the human label plus the raw detail (e.g. the
// nodeless-zombie cause). Screen readers announce this when the badge is
// focused, and it doubles as the hover tooltip via :title.
const ariaLabel = computed(() =>
  props.detail ? `${label.value}. ${props.detail}` : label.value,
)
</script>

<template>
  <span
    v-if="code"
    role="status"
    class="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium"
    :class="pillClass"
    :aria-label="ariaLabel"
    :title="detail || label"
  >{{ label }}</span>
  <span v-else>—</span>
</template>
