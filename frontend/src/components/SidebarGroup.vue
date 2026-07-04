<template>
  <div>
    <button
      type="button"
      :aria-expanded="!effectiveCollapsed"
      :aria-controls="`sidebar-group-${id}`"
      @click="$emit('toggle')"
      :disabled="forceExpanded"
      class="sidebar-group-header"
    >
      <span class="sidebar-group-label">{{ labelKey ? $t(labelKey) : label }}</span>
      <span class="sidebar-group-chevron" :class="{ rotated: !effectiveCollapsed }">
        <svg
          xmlns="http://www.w3.org/2000/svg"
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
        >
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </span>
    </button>
    <Transition name="fade">
      <div
        v-show="!effectiveCollapsed"
        :id="`sidebar-group-${id}`"
        class="sidebar-group-items"
        role="region"
        :aria-label="labelKey ? $t(labelKey) : label"
      >
        <slot />
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";

const props = withDefaults(defineProps<{
  id: string;
  label: string;
  labelKey: string;
  collapsed: boolean;
  forceExpanded?: boolean;
}>(), { forceExpanded: false });

defineEmits<{
  toggle: [];
}>();

const effectiveCollapsed = computed(() => {
  if (props.forceExpanded) return false
  return props.collapsed
});
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
  transition:
    background-color 150ms ease,
    color 150ms ease;
  border: none;
  background: transparent;
  margin-bottom: 0.25rem;
}

.sidebar-group-header:hover {
  background-color: hsl(var(--accent));
  color: hsl(var(--foreground));
}

.sidebar-group-header:disabled {
  cursor: default;
  opacity: 0.7;
}

.sidebar-group-header:disabled:hover {
  background: transparent;
}

.sidebar-group-header:focus-visible {
  outline: 2px solid hsl(var(--primary));
  outline-offset: 2px;
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
  transition:
    opacity 0.15s ease,
    transform 0.15s ease;
  overflow: hidden;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}
</style>
