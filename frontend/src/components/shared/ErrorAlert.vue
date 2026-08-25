<template>
  <div
    class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive"
  >
    <p v-if="isProblem">{{ problem!.title }}: {{ problem!.detail }}</p>
    <p v-else>{{ message }}</p>
    <button type="button"
      v-if="onRetry && retryable !== false"
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
  message?: string | ProblemDetail
  onRetry?: () => void
  retryable?: boolean
  onDismiss?: () => void
  dismissLabel?: string
}>()

const isProblem = computed(() => props.message && isProblemDetail(props.message))
const problem = computed(() => isProblem.value ? props.message as ProblemDetail : null)
</script>
