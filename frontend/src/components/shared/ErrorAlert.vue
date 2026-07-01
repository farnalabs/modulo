<template>
  <div
    class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive"
  >
    <p>{{ message }}</p>
    <button
      v-if="onRetry && retryable !== false"
      class="ml-2 underline"
      @click="onRetry"
    >
      Retry
    </button>
  </div>
</template>

<script setup lang="ts">
defineProps<{
  message: string;
  onRetry?: () => void;
  retryable?: boolean;
}>();
</script>
