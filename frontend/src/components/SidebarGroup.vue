<template>
  <div>
    <button @click="$emit('toggle')" class="sidebar-group-header">
      <span class="sidebar-group-label">{{ label }}</span>
      <span class="sidebar-group-chevron" :class="{ rotated: !collapsed }">
        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
      </span>
    </button>
    <Transition name="fade">
      <div v-if="!collapsed" class="sidebar-group-items">
        <slot />
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
defineProps<{
  id: string
  label: string
  collapsed: boolean
}>()

defineEmits<{
  toggle: []
}>()
</script>

<style scoped>
.sidebar-group-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 0.5rem 0.75rem;
  font-size: 0.75rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: hsl(var(--muted-foreground));
  text-align: left;
  cursor: pointer;
  border-radius: var(--radius-md);
  transition: background-color 150ms ease, color 150ms ease;
  border: none;
  background: transparent;
  margin-top: 1.5rem;
  margin-bottom: 0.25rem;
}

.sidebar-group-header:hover {
  background-color: hsl(var(--accent));
  color: hsl(var(--foreground));
}

.sidebar-group-label {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  border-left: 2px solid hsla(var(--primary) / 0.25);
  padding-left: 0.5rem;
}

.sidebar-group-chevron {
  transition: transform 0.2s ease;
  display: flex;
  align-items: center;
}

.sidebar-group-chevron.rotated {
  transform: rotate(180deg);
}

.sidebar-group-items {
  display: flex;
  flex-direction: column;
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.15s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
