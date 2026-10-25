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
const { t } = useI18n()

const actionLabel = computed(() => {
  const key = `views.RiskAssessmentView.action_${props.action.toLowerCase()}`
  return t(key) || props.action
})

const badgeClass = computed(() => {
  switch (props.action.toLowerCase()) {
    case 'block':
      return 'bg-destructive/10 text-destructive border border-destructive/20'
    case 'warn':
      return 'bg-warning/10 text-warning border border-warning/20'
    case 'continue':
      return 'bg-success/10 text-success border border-success/20'
    case 'escalate':
      return 'bg-primary/10 text-primary border border-primary/20'
    case 'risk':
      return 'bg-orange/10 text-orange border border-orange/20'
    case 'assessment':
      return 'bg-blue/10 text-blue border border-blue/20'
    default:
      return 'bg-muted text-muted-foreground border border-muted'
  }
})
</script>
