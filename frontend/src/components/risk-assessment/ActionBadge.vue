<template>
  <span
    :class="[
      'inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium',
      badgeClass,
    ]"
    :data-testid="`risk-assessment-action-${action.toLowerCase()}`"
  >
    {{ actionLabel }}
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

interface Props {
  action: string
}

const props = defineProps<Props>()
const { t, te } = useI18n()

const actionKey = computed(
  () => `views.RiskAssessmentView.action_${props.action.toLowerCase()}`,
)

// vue-i18n returns the key itself for a missing message, so `t(key) || action`
// can never fall back. `te()` is the existence check that makes it real.
const actionLabel = computed(() => (te(actionKey.value) ? t(actionKey.value) : props.action))

const badgeClass = computed(() => {
  switch (props.action.toLowerCase()) {
    case 'block':
      return 'bg-destructive/10 text-destructive-text border border-destructive/20'
    case 'warn':
      return 'bg-warning/10 text-warning-text border border-warning/20'
    case 'continue':
      return 'bg-success/10 text-success-text border border-success/20'
    case 'escalate':
      return 'bg-primary/10 text-primary-text border border-primary/20'
    default:
      return 'bg-muted text-muted-foreground border border-muted'
  }
})
</script>
