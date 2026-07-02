<template>
  <nav aria-label="Section navigation" class="page-tabs">
    <router-link
      v-for="tab in tabs"
      :key="tab.to"
      :to="tab.to"
      class="page-tab"
      :class="{ active: isActive(tab.to) }"
      :aria-current="isActive(tab.to) ? 'page' : undefined"
    >
      {{ tab.label }}
    </router-link>
  </nav>
</template>

<script setup lang="ts">
import { useRoute } from 'vue-router'

interface Tab {
  label: string
  to: string
}

const props = defineProps<{
  tabs: Tab[]
}>()

const route = useRoute()

function isActive(to: string): boolean {
  if (route.path === to) return true
  if (!route.path.startsWith(to + '/')) return false
  return !(props.tabs as Tab[]).some(
    (tab) => tab.to !== to && route.path.startsWith(tab.to + '/') && tab.to.length > to.length,
  )
}
</script>

<style scoped>
.page-tabs {
  display: flex;
  flex-direction: row;
  gap: 0;
  border-bottom: 1px solid hsl(var(--border));
  margin-bottom: 1.5rem;
}
.page-tab {
  padding: 0.5rem 1rem;
  font-size: 0.875rem;
  font-weight: 500;
  color: hsl(var(--muted-foreground));
  border-bottom: 2px solid transparent;
  transition: color 0.15s, background-color 0.15s, border-color 0.15s;
  text-decoration: none;
}
.page-tab:hover {
  color: hsl(var(--foreground));
  background-color: hsl(var(--accent) / 0.5);
}
.page-tab.active {
  color: hsl(var(--primary));
  border-bottom-color: hsl(var(--primary));
  font-weight: 600;
}
</style>
