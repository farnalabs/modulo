<template>
  <OverlayScrollbarsComponent
    defer
    :options="osOptions"
    class="flex-1 min-h-0 relative"
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
          <SidebarLink
            v-for="item in group.items"
            :key="item.to"
            :to="item.to"
            :icon="item.icon"
            :label="item.label"
            :label-key="item.labelKey"
            :exact="item.exact"
            :preview="item.preview"
            @click="$emit('navigate')"
          /></SidebarGroup>
      </template>
    </div>
    <div class="pointer-events-none sticky bottom-0 left-0 right-0 h-10 bg-gradient-to-t from-background to-transparent" aria-hidden="true" />
  </OverlayScrollbarsComponent>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import { OverlayScrollbarsComponent } from "overlayscrollbars-vue";
import SidebarLink from "./SidebarLink.vue";
import SidebarGroup from "./SidebarGroup.vue";
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
const { viewMode, toggleGroup, isGroupCollapsed } = useSidebar();
const planStore = usePlanStore();

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
        (g.simpleMode || viewMode.value === "advanced") &&
        (!g.systemAdminOnly || props.isSystemAdmin),
    )
    .map((group) => ({
      ...group,
      items: group.items.filter((item) => {
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
