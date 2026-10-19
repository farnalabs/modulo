<template>
  <div :class="rootClass">
    <div class="min-w-0">
      <p v-if="isProblem">{{ problem!.title }}: {{ problem!.detail }}</p>
      <p v-else-if="message">{{ message }}</p>
      <button type="button"
        v-if="onRetry && retryable !== false"
        class="ml-2 underline"
        data-testid="error-alert-retry"
        @click="onRetry"
      >
        {{ $t('components.ErrorAlert.retry') }}
      </button>
    </div>
    <button
      v-if="onDismiss && dismissLabel && dismissIcon"
      type="button"
      class="shrink-0 -m-1.5 p-1.5"
      :aria-label="dismissLabel"
      data-testid="error-alert-dismiss"
      @click="onDismiss"
    >
      <X :size="16" aria-hidden="true" />
    </button>
    <button
      v-else-if="onDismiss && dismissLabel"
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
import { X } from '@lucide/vue'
import { isProblemDetail, type ProblemDetail } from '../../lib/api/formatError'

const props = withDefaults(defineProps<{
  message?: string | ProblemDetail | null
  onRetry?: () => void
  retryable?: boolean
  onDismiss?: () => void
  dismissLabel?: string
  compact?: boolean
  dismissIcon?: boolean
}>(), { retryable: true, compact: false, dismissIcon: false })

const isProblem = computed(() => props.message && isProblemDetail(props.message))
const problem = computed(() => isProblem.value ? props.message as ProblemDetail : null)

// compact: the tighter density used inside dense forms/dialogs (p-3 text-sm),
// matching the bespoke boxes this component replaced. dismissIcon: lays the
// root out as a flex row so the icon-only dismiss sits right of the message.
const rootClass = computed(() => [
  'rounded-lg border border-destructive/50 bg-destructive/10 text-destructive-text',
  props.compact ? 'p-3 text-sm' : 'p-4',
  props.dismissIcon ? 'flex items-start justify-between' : '',
].filter(Boolean).join(' '))
</script>
