<template>
  <nav
    :aria-label="t('components.PageTabs.section_navigation')"
    class="page-tabs-wrapper"
    role="tablist"
  >
    <div class="page-tabs-scroll">
      <router-link
        v-for="entry in renderedTabs"
        :key="entry.tab.to"
        :to="entry.href"
        class="page-tab"
        :class="{ active: isActive(entry.tab.to), locked: !!entry.lock }"
        :aria-current="isActive(entry.tab.to) ? 'page' : undefined"
        role="tab"
        :aria-selected="isActive(entry.tab.to) ? 'true' : 'false'"
        :aria-label="entry.lock ? entry.lock.aria : undefined"
        :title="entry.lock ? entry.lock.tooltip : undefined"
        v-bind="entry.lock ? { 'data-testid': 'page-tab-locked' } : {}"
      >
        <!-- FAR-47: a plan-locked tab keeps its space in the bar (never
             removed) and leads with the padlock; the tab itself links to the
             license page so the upgrade path is one click away. -->
        <LockIcon :locked="!!entry.lock" :tooltip="entry.lock?.tooltip" />
        <component :is="entry.tab.icon" v-if="entry.tab.icon" class="h-4 w-4 shrink-0" />
        <span>{{ entry.tab.label }}</span>
        <span
          v-if="entry.tab.badge !== undefined"
          class="page-tab-badge"
          :class="badgeClass(entry.tab.badgeVariant)"
        >
          {{ entry.tab.badge }}
        </span>
      </router-link>
    </div>
  </nav>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import type { Component } from 'vue'
import { useI18n } from 'vue-i18n'
import { usePlanStore } from '../stores/planStore'
import LockIcon from './LockIcon.vue'
import manifest from '@/manifest.yaml'

export interface Tab {
  label: string
  /**
   * Exact `manifest.routes` key for this tab's destination (the bare route
   * path, e.g. `/admin/costs`). The plan-lock lookup resolves against it
   * verbatim, so a `to` carrying a query/hash (or a trailing-slash variant)
   * does not match a manifest entry and the tab is read as ungated.
   */
  to: string
  icon?: Component
  badge?: number | string
  badgeVariant?: 'primary' | 'warning' | 'destructive'
}

/** Copy + accessible name for a plan-locked tab. */
interface TabLock {
  tooltip: string
  aria: string
}

/** Where a plan-locked tab sends the user (mirrors FeatureGate's pricingUrl). */
const UPGRADE_PATH = '/settings/license'

interface ManifestRouteEntry {
  required_tier?: string | null
}

const manifestRoutes =
  (manifest as { routes?: Record<string, ManifestRouteEntry | undefined> }).routes ?? {}

function requiredTier(to: string): string | null {
  return manifestRoutes[to]?.required_tier ?? null
}

const { t } = useI18n()

const props = defineProps<{
  tabs: Tab[]
}>()

const route = useRoute()
const planStore = usePlanStore()

/**
 * FAR-47: tier-gated tabs are never hidden — they stay in the bar, occupy
 * their space, and render a padlock + "Available on the <plan> plan" tooltip
 * linking to the license page. The locked state is resolved once per render
 * from the manifest's route metadata (the same source the router guard and
 * sidebar use), so every PageTabs consumer gets it without per-view wiring.
 */
const renderedTabs = computed(() =>
  props.tabs.map((tab) => {
    const lock = lockFor(tab)
    return { tab, lock, href: lock ? UPGRADE_PATH : tab.to }
  }),
)

function lockFor(tab: Tab): TabLock | null {
  const tier = requiredTier(tab.to)
  // A community-tier route is free on every plan, so it is never plan-locked.
  // The explicit tier-id check also keeps it unlocked while the tier catalog
  // is unknown (an unranked currentTier makes isAtMinimumTier return false).
  if (!tier || tier === 'community') return null
  // Until the plan sources resolve, the active tier is not yet known (it
  // defaults to `community`), so `isAtMinimumTier` cannot distinguish an
  // entitled user from an under-tier one. Locking here would flash a padlock
  // at a tier-entitled user and then flip it off (FAR-47 review). Leave the
  // tab unlocked until the plan has resolved — the analogue of the sidebar's
  // `tierInfoLoaded` gating in config/navigation.ts.
  if (!planStore.loaded) return null
  if (planStore.isAtMinimumTier(tier)) return null
  const plan = planStore.getTierLabel(tier)
  return {
    tooltip: t('components.PageTabs.available_on_plan', { plan }),
    aria: t('components.PageTabs.locked_aria', { label: tab.label, plan }),
  }
}

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
  color: hsl(var(--warning-text));
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
