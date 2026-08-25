<template>
  <header :data-testid="dataTestId" class="flex items-start justify-between gap-4">
    <div class="min-w-0">
      <button type="button" v-if="backLink" class="mb-2 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground transition-colors" @click="$router.push(backLink)">
        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 18 9 12 15 6"/></svg>
        Back
      </button>
      <h1 class="text-2xl font-semibold tracking-tight">{{ title }}</h1>
      <p v-if="subtitle" class="mt-1 text-sm text-muted-foreground">{{ subtitle }}</p>
    </div>
    <div v-if="$slots.right" class="flex items-center gap-2 shrink-0">
      <slot name="right" />
    </div>
  </header>
</template>

<script setup lang="ts">
defineProps<{
  title: string
  subtitle?: string
  backLink?: string
  dataTestId?: string
}>()
</script>
