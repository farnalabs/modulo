<template>
  <OverlayScrollbarsComponent
    defer
    :options="osOptions"
    class="flex-1 min-h-0 relative"
    :class="collapsed ? 'w-full' : 'pr-3'"
    element="nav"
    :aria-label="$t('components.SidebarNav.main_navigation')"
  >
    <div v-if="collapsed" class="flex flex-col items-center gap-1">
      <template v-for="group in visibleSidebarGroups" :key="group.id">
        <button
          type="button"
          class="sidebar-group-rail-toggle"
          :class="{ 'sidebar-group-rail-toggle--touch': size === 'touch' }"
          :aria-expanded="!isGroupCollapsed(group.id, group.defaultCollapsed)"
          :aria-controls="`sidebar-group-rail-${group.id}`"
          :title="groupToggleLabel(group)"
          :aria-label="groupToggleLabel(group)"
          @click="toggleGroup(group.id, group.defaultCollapsed)"
        >
          <span
            class="sidebar-group-rail-chevron"
            :class="{ rotated: !isGroupCollapsed(group.id, group.defaultCollapsed) }"
          >
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
              aria-hidden="true"
              focusable="false"
            >
              <polyline points="6 9 12 15 18 9" />
            </svg>
          </span>
        </button>
        <section
          v-if="!isGroupCollapsed(group.id, group.defaultCollapsed)"
          :id="`sidebar-group-rail-${group.id}`"
          class="flex flex-col items-center gap-1"
          :aria-label="groupLabel(group)"
        >
          <SidebarLink
            v-for="item in group.items"
            :key="item.to"
            :to="item.to"
            :icon="item.icon"
            :label="item.label"
            :label-key="item.labelKey"
            :exact="item.exact"
            :visibility="item.visibility"
            :collapsed="true"
            :size="linkSize"
            class="w-full"
            @click="$emit('navigate')"
          />
        </section>
      </template>
    </div>
    <template v-else>
      <div class="space-y-6">
        <template v-for="group in visibleSidebarGroups" :key="group.id">
          <SidebarGroup
            :id="group.id"
            :label="group.label"
            :label-key="group.labelKey"
            :collapsed="isGroupCollapsed(group.id, group.defaultCollapsed)"
            :is-active="activeGroupIds.has(group.id)"
            @toggle="toggleGroup(group.id, group.defaultCollapsed)"
          >
            <SidebarLink
              v-for="item in group.items"
              :key="item.to"
              :to="item.to"
              :icon="item.icon"
              :label="item.label"
              :label-key="item.labelKey"
              :exact="item.exact"
              :visibility="item.visibility"
              :size="linkSize"
              @click="$emit('navigate')"
            /></SidebarGroup>
        </template>
      </div>
      <div class="pointer-events-none sticky bottom-0 left-0 right-0 h-10 bg-gradient-to-t from-background to-transparent" aria-hidden="true" />
    </template>
  </OverlayScrollbarsComponent>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import { useI18n } from "vue-i18n";
import { OverlayScrollbarsComponent } from "overlayscrollbars-vue";
import SidebarLink from "./SidebarLink.vue";
import SidebarGroup from "./SidebarGroup.vue";

import { getVisibleNavGroups } from "../config/navigation";
import type { NavGroup } from "../config/navigation";
import { useNavVisibilityContext } from "../composables/useNavVisibilityContext";
import { useSidebar } from "../composables/useSidebar";

const osOptions = {
  scrollbars: {
    autoHide: "never" as const,
    autoHideDelay: 0,
    clickScroll: true,
  },
};

const props = withDefaults(
  defineProps<{
    isSystemAdmin: boolean;
    userRole?: string | null;
    userPermissions?: string[];
    collapsed?: boolean;
    /**
     * Hit-target profile. `touch` widens the collapsed rail's group-disclosure
     * toggles to 44x44 and forwards touch sizing to each rail link; absent or
     * `default` leaves the desktop chrome unchanged. Set by SidebarRail only on
     * the mobile rail path.
     */
    size?: "default" | "touch";
  }>(),
  { size: "default" },
);

defineEmits<{
  navigate: [];
}>();

const route = useRoute();
const { t } = useI18n();
const { toggleGroup, isGroupCollapsed } = useSidebar();

// Forward the touch profile to each rail link; `undefined` keeps the child on
// its own `default` so the desktop rail renders exactly as before.
const linkSize = computed(() => (props.size === "touch" ? "touch" : undefined));

function groupLabel(group: NavGroup): string {
  return group.labelKey ? t(group.labelKey) : group.label;
}

function groupToggleLabel(group: NavGroup): string {
  const collapsed = isGroupCollapsed(group.id, group.defaultCollapsed);
  const hint = collapsed
    ? t("components.SidebarNav.group_collapsed_hint")
    : t("components.SidebarNav.group_expanded_hint");
  const action = collapsed
    ? t("components.SidebarNav.click_to_expand")
    : t("components.SidebarNav.click_to_collapse");
  return `${groupLabel(group)} (${hint}) — ${action}`;
}

const activeGroupIds = computed(() => {
  const ids = new Set<string>()
  const path = route.path
  for (const group of visibleSidebarGroups.value) {
    for (const item of group.items) {
      if (item.exact ? path === item.to : path.startsWith(item.to)) {
        ids.add(group.id)
        break
      }
    }
  }
  return ids
})

// qa iter 2 (FAR-535): consume the shared nav-visibility composable instead
// of hand-building a second NavVisibilityContext inline — the composable is
// the single construction point, so context fields (isDemoSession,
// tierInfoLoaded, devMode) are defined exactly once. Role/admin/permission
// values still come from this component's props (they are passed down from
// AppLayout's decoded JWT), supplied as a getter so prop changes stay
// reactive; every other field uses the composable's own defaults.
const navVisibility = useNavVisibilityContext(() => ({
  isSystemAdmin: props.isSystemAdmin,
  userRole: props.userRole,
  userPermissions: props.userPermissions,
}));

const visibleSidebarGroups = computed(() => getVisibleNavGroups(navVisibility.value));
</script>

<style scoped>
.sidebar-group-rail-toggle {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 2.5rem;
  height: 1.625rem;
  flex-shrink: 0;
  border: 1px dashed hsl(var(--border));
  border-radius: var(--radius-md);
  background: transparent;
  color: hsl(var(--muted-foreground));
  cursor: pointer;
  transition:
    background-color 150ms ease,
    color 150ms ease;
}

/* Touch surface only: raise the rail's group-disclosure control from
   40x26 to 44x44 (WCAG 2.5.8). Declared after the base rule so it wins at
   equal specificity; never applied unless `size === 'touch'`. */
.sidebar-group-rail-toggle--touch {
  width: 2.75rem;
  height: 2.75rem;
}

.sidebar-group-rail-toggle:hover {
  background-color: hsl(var(--accent));
  color: hsl(var(--foreground));
}

.sidebar-group-rail-toggle:focus-visible {
  outline: 2px solid hsl(var(--primary));
  outline-offset: 2px;
}

.sidebar-group-rail-chevron {
  display: flex;
  align-items: center;
  transition: transform 0.2s var(--ease-out);
}

.sidebar-group-rail-chevron.rotated {
  transform: rotate(180deg);
}
</style>
