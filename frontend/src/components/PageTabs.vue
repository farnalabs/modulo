<template>
  <nav
    :aria-label="t('components.PageTabs.section_navigation')"
    class="page-tabs-wrapper"
    role="tablist"
  >
    <div class="page-tabs-scroll">
      <router-link
        v-for="tab in tabs"
        :key="tab.to"
        :to="tab.to"
        class="page-tab"
        :class="{ active: isActive(tab.to) }"
        :aria-current="isActive(tab.to) ? 'page' : undefined"
        role="tab"
        :aria-selected="isActive(tab.to) ? 'true' : 'false'"
      >
        <component :is="tab.icon" v-if="tab.icon" class="h-4 w-4 shrink-0" />
        <span>{{ tab.label }}</span>
        <span
          v-if="tab.badge !== undefined"
          class="page-tab-badge"
          :class="badgeClass(tab.badgeVariant)"
        >
          {{ tab.badge }}
        </span>
      </router-link>
    </div>
  </nav>
</template>

<script setup lang="ts">
import { useRoute } from 'vue-router'
import type { Component } from 'vue'
import { useI18n } from 'vue-i18n'

export interface Tab {
  label: string
  to: string
  icon?: Component
  badge?: number | string
  badgeVariant?: 'primary' | 'warning' | 'destructive'
}

const { t } = useI18n()

const props = defineProps<{
  tabs: Tab[]
}>()

const route = useRoute()

function isActive(to: string): boolean {
  if (route.path === to) return true
  if (!route.path.startsWith(to + '/')) return false
  return !props.tabs.some(
    (tab) => tab.to !== to && (route.path.startsWith(tab.to + '/') || route.path === tab.to) && tab.to.length > to.length,
  )
}

function badgeClass(variant?: 'primary' | 'warning' | 'destructive'): string {
  switch (variant) {
    case 'warning':
      return 'badge-warning'
    case 'destructive':
      return 'badge-destructive'
    default:
      return 'badge-primary'
  }
}
</script>

<style scoped>
.page-tabs-wrapper {
  margin-bottom: 1.5rem;
}

.page-tabs-scroll {
  display: flex;
  flex-direction: row;
  gap: 0.25rem;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  -ms-overflow-style: none;
  scrollbar-width: none;
  mask-image: linear-gradient(to right, black calc(100% - 3rem), transparent 100%);
  -webkit-mask-image: linear-gradient(to right, black calc(100% - 3rem), transparent 100%);
  border-radius: var(--radius-lg);
  background-color: hsl(var(--muted) / 0.5);
  padding: 0.375rem;
}

.page-tabs-scroll::-webkit-scrollbar {
  display: none;
}

.page-tab {
  display: inline-flex;
  align-items: center;
  gap: 0.375rem;
  padding: 0.375rem 0.75rem;
  font-size: 0.875rem;
  font-weight: 500;
  color: hsl(var(--muted-foreground));
  border-radius: var(--radius-md);
  border: 1px solid transparent;
  text-decoration: none;
  white-space: nowrap;
  flex-shrink: 0;
  transition: color var(--duration-fast) var(--ease-out),
              background-color var(--duration-fast) var(--ease-out),
              border-color var(--duration-fast) var(--ease-out),
              box-shadow var(--duration-fast) var(--ease-out);
}

@media (hover: hover) and (pointer: fine) {
  .page-tab:hover {
    color: hsl(var(--foreground));
    background-color: hsl(var(--accent));
  }
}

.page-tab:active {
  transform: scale(0.97);
}

.page-tab.active {
  color: hsl(var(--primary));
  background-color: hsl(var(--background));
  border-color: hsl(var(--border));
  box-shadow: 0 1px 3px 0 rgb(0 0 0 / 0.1), 0 1px 2px -1px rgb(0 0 0 / 0.1);
  font-weight: 600;
}

.page-tab-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 1.25rem;
  height: 1.25rem;
  padding: 0 0.375rem;
  border-radius: 9999px;
  font-size: 0.7rem;
  font-weight: 600;
  line-height: 1;
}

.badge-primary {
  background-color: hsla(var(--primary) / 0.12);
  color: hsl(var(--primary));
}

.badge-warning {
  background-color: hsla(var(--warning) / 0.12);
  color: hsl(var(--warning));
}

.badge-destructive {
  background-color: hsla(var(--destructive) / 0.12);
  color: hsl(var(--destructive));
}

@media (max-width: 640px) {
  .page-tabs-wrapper {
    border-bottom: 1px solid hsl(var(--border));
  }

  .page-tabs-scroll {
    background-color: transparent;
    border-radius: 0;
    padding: 0;
    mask-image: none;
    -webkit-mask-image: none;
    /* WCAG 2.5.8 target-offset: more spacing between adjacent tab targets on mobile */
    gap: 0.5rem;
  }

  .page-tab {
    padding: 0.5rem 0.75rem;
    border: none;
    border-radius: 0;
    border-bottom: 2px solid transparent;
    /* WCAG 2.5.8 target size: never below 24px tall on mobile */
    min-height: 2rem;
  }

  .page-tab:hover {
    background-color: transparent;
    color: hsl(var(--foreground));
  }

  .page-tab.active {
    background-color: transparent;
    box-shadow: none;
    border-color: hsl(var(--primary));
  }
}

@media (prefers-reduced-motion: reduce) {
  .page-tab {
    transition: none;
  }
}
</style>
