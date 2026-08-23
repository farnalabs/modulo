<template>
  <div :class="classes">
    <p v-if="isProblem">{{ problem!.title }}: {{ problem!.detail }}</p>
    <p v-else>{{ message }}</p>
    <button
      v-if="variant !== 'success' && onRetry && retryable !== false"
      class="ml-2 underline"
      @click="onRetry"
    >
      Retry
    </button>
    <button
      v-if="onDismiss && dismissLabel"
      type="button"
      class="ml-2 underline"
      data-testid="error-alert-dismiss"
      @click="onDismiss"
    >
      {{ dismissLabel }}
    </button>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { isProblemDetail, type ProblemDetail } from '../../lib/api/formatError'

const props = defineProps<{
  variant?: 'error' | 'success'
  message?: string | ProblemDetail | null
  onRetry?: () => void
  retryable?: boolean
  onDismiss?: () => void
  dismissLabel?: string
}>()

const classes = computed(() =>
  props.variant === 'success'
    ? 'rounded-lg border border-emerald-500/50 bg-emerald-500/10 p-4 text-emerald-600'
    : 'rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive',
)
const isProblem = computed(() => props.message && isProblemDetail(props.message))
const problem = computed(() => isProblem.value ? props.message as ProblemDetail : null)
</script>
