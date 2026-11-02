<template>
  <div v-if="items?.length" class="rounded border p-3" :class="tone === 'warning' ? 'border-warning bg-warning/5' : ''">
    <div class="flex items-center justify-between"><span class="text-sm font-medium">{{ label }}</span><span class="rounded-full bg-muted px-2 py-0.5 text-xs">{{ items.length }}</span></div>
    <ul class="mt-2 text-xs text-muted-foreground space-y-1"><li v-for="(item, index) in items" :key="index">{{ displayItem(item) }}</li></ul>
  </div>
</template>
<script setup lang="ts">
defineProps<{ label: string; items?: unknown[]; tone?: string }>()
function displayItem(item: unknown): string { return typeof item === 'string' ? item : JSON.stringify(item) }
</script>
