<template>
  <header :data-testid="dataTestId" class="page-header">
    <div class="flex min-w-0 flex-1 flex-col">
      <button type="button" v-if="backLink" class="mb-2 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground transition-colors" @click="$router.push(backLink)">
        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true" focusable="false"><polyline points="15 18 9 12 15 6"/></svg>
        Back
      </button>
      <h1 class="text-2xl font-semibold tracking-tight">{{ title }}</h1>
      <p v-if="subtitle" class="mt-1 text-sm text-muted-foreground">{{ subtitle }}</p>
      <div v-if="$slots.default" class="mt-4">
        <slot />
      </div>
    </div>
    <div v-if="$slots.right" data-testid="page-header-right" class="flex w-full flex-wrap items-center gap-2 sm:w-auto sm:shrink-0 sm:flex-nowrap">
      <slot name="right" />
    </div>
  </header>
  <PageTabs v-if="tabs?.length" :tabs="tabs" />
</template>

<script setup lang="ts">
import PageTabs, { type Tab } from '../PageTabs.vue'

defineProps<{
  title: string
  subtitle?: string
  backLink?: string
  dataTestId?: string
  tabs?: Tab[]
}>()
</script>

<style scoped>
.page-header {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

@media (min-width: 640px) {
  .page-header {
    flex-direction: row;
    align-items: flex-start;
    justify-content: space-between;
  }
}
</style>
