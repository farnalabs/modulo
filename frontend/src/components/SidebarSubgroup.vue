<template>
  <details :open="defaultOpen ? true : undefined" class="sidebar-subgroup">
    <summary class="sidebar-subgroup-header">
      <span class="sidebar-subgroup-label">{{ label }}</span>
      <span class="sidebar-subgroup-chevron">▸</span>
    </summary>
    <div class="sidebar-subgroup-items">
      <slot />
    </div>
  </details>
</template>

<script setup lang="ts">
defineProps<{
  label: string;
  defaultOpen?: boolean;
}>();
</script>

<style scoped>
.sidebar-subgroup {
  border: none;
  background: none;
}

.sidebar-subgroup-header {
  font-size: 0.7rem;
  font-weight: 500;
  text-transform: uppercase;
  color: hsl(var(--muted-foreground));
  cursor: pointer;
  user-select: none;
  display: flex;
  align-items: center;
  gap: 4px;
  margin-left: 8px;
  padding: 2px 0;
  list-style: none;
}

.sidebar-subgroup-header::-webkit-details-marker {
  display: none;
}

.sidebar-subgroup-header:hover {
  color: hsl(var(--foreground));
}

.sidebar-subgroup-chevron {
  display: inline-block;
  transition: transform 0.2s;
  font-size: 0.6rem;
}

.sidebar-subgroup[open] .sidebar-subgroup-chevron {
  transform: rotate(90deg);
}

.sidebar-subgroup-items {
  padding-left: 8px;
  border-left: 1px solid hsl(var(--border));
}
</style>
