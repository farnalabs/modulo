<template>
  <OverlayScrollbarsComponent
    defer
    :options="osOptions"
    class="flex-1 min-h-0 relative pr-3"
    element="nav"
    :aria-label="$t('components.SidebarNav.main_navigation')"
  >
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
          <template v-for="item in group.items" :key="item.to">
            <SidebarLink
              v-if="!item.children"
              :to="item.to"
              :icon="item.icon"
              :label="item.label"
              :label-key="item.labelKey"
              :exact="item.exact"
              :visibility="item.visibility"
              @click="$emit('navigate')"
            />
            <div v-else class="sidebar-subgroup">
              <button
                type="button"
                :aria-expanded="expandedSubItems.has(item.to)"
                :aria-controls="`sidebar-sub-${item.to.replace(/\//g, '-')}`"
                @click="toggleSubItem(item.to)"
                class="sidebar-subgroup-header"
              >
                <span class="h-4 w-4 shrink-0"><SvgIcon :name="item.icon" /></span>
                <span class="truncate flex-1 text-left">{{ item.labelKey ? $t(item.labelKey) : item.label }}</span>
                <span class="sidebar-subgroup-chevron" :class="{ rotated: expandedSubItems.has(item.to) }">
                  <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <polyline points="6 9 12 15 18 9" />
                  </svg>
                </span>
              </button>
              <Transition name="fade">
                <div
                  v-show="expandedSubItems.has(item.to)"
                  :id="`sidebar-sub-${item.to.replace(/\//g, '-')}`"
                  class="sidebar-subgroup-items"
                  role="region"
                  :aria-label="item.labelKey ? $t(item.labelKey) : item.label"
                >
                  <SidebarLink
                    v-for="child in item.children"
                    :key="child.to"
                    :to="child.to"
                    :icon="child.icon"
                    :label="child.label"
                    :label-key="child.labelKey"
                    :exact="child.exact"
                    :visibility="child.visibility"
                    class="sidebar-subitem"
                    @click="$emit('navigate')"
                  />
                </div>
              </Transition>
            </div>
          </template></SidebarGroup>
      </template>
    </div>
    <div class="pointer-events-none sticky bottom-0 left-0 right-0 h-10 bg-gradient-to-t from-background to-transparent" aria-hidden="true" />
  </OverlayScrollbarsComponent>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";
import { useRoute } from "vue-router";
import { OverlayScrollbarsComponent } from "overlayscrollbars-vue";
import SidebarLink from "./SidebarLink.vue";
import SidebarGroup from "./SidebarGroup.vue";
import SvgIcon from "./SvgIcon.vue";
import { getNavGroups, canSeeItem } from "../config/navigation";
import { useSidebar } from "../composables/useSidebar";
import { usePlanStore } from "../stores/planStore";

const osOptions = {
  scrollbars: {
    autoHide: "never" as const,
    autoHideDelay: 0,
    clickScroll: true,
  },
};

const props = defineProps<{
  isSystemAdmin: boolean;
  userRole?: string | null;
  userPermissions?: string[];
}>();

defineEmits<{
  navigate: [];
}>();

const route = useRoute();
const { toggleGroup, isGroupCollapsed } = useSidebar();
const planStore = usePlanStore();

const expandedSubItems = ref(new Set<string>())

function toggleSubItem(path: string) {
  const next = new Set(expandedSubItems.value)
  if (next.has(path)) {
    next.delete(path)
  } else {
    next.add(path)
  }
  expandedSubItems.value = next
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

const tierInfoLoaded = computed(() => planStore.tierRanks ? Object.keys(planStore.tierRanks).length > 0 : false);

const visibleSidebarGroups = computed(() =>
  getNavGroups()
    .filter(
      (g) =>
        (!g.systemAdminOnly || props.isSystemAdmin),
    )
    .map((group) => ({
      ...group,
      items: group.items.filter((item) => {
        if ((item.visibility === 'private_preview' || item.visibility === 'in_dev') && !planStore.devMode) return false
        if (!item.requiredRoles && !item.requiredTier) return true
        if (item.requiredTier && !tierInfoLoaded.value) return false
        return canSeeItem(
          item,
          {
            role: props.userRole || "",
            permissions: props.userPermissions,
          },
          { isAtMinimumTier: (tier: string) => planStore.isAtMinimumTier(tier) },
        )
      }),
    }))
    .filter(g => g.items.length > 0)
);
</script>

<style scoped>
.sidebar-subgroup {
  margin-bottom: 0.125rem;
}

.sidebar-subgroup-header {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  width: 100%;
  padding: 0.375rem 0.75rem;
  font-size: 0.875rem;
  color: hsl(var(--foreground));
  border-radius: var(--radius-md);
  transition: background-color 150ms ease;
  border: none;
  background: transparent;
  cursor: pointer;
  text-align: left;
}

.sidebar-subgroup-header:hover {
  background-color: hsl(var(--accent));
}

.sidebar-subgroup-header:focus-visible {
  outline: 2px solid hsl(var(--primary));
  outline-offset: 2px;
}

.sidebar-subgroup-chevron {
  transition: transform 0.2s var(--ease-out);
  display: flex;
  align-items: center;
  color: hsl(var(--muted-foreground));
}

.sidebar-subgroup-chevron.rotated {
  transform: rotate(180deg);
}

.sidebar-subgroup-items {
  display: flex;
  flex-direction: column;
  padding-left: 1.5rem;
}

:deep(.sidebar-subitem) {
  font-size: 0.8125rem;
  padding: 0.25rem 0.75rem;
}
</style>
