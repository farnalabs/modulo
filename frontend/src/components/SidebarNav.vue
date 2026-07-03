<template>
  <nav :aria-label="$t('components.SidebarNav.main_navigation')" class="flex-1 space-y-6">
    <template v-for="group in visibleSidebarGroups" :key="group.id">
      <SidebarGroup
        :id="group.id"
        :label="group.label"
        :label-key="group.labelKey"
        :collapsed="isGroupCollapsed(group.id, group.defaultCollapsed)"
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
          @click="$emit('navigate')"
        /></SidebarGroup>
    </template>
  </nav>
</template>

<script setup lang="ts">
import { computed } from "vue";
import SidebarLink from "./SidebarLink.vue";
import SidebarGroup from "./SidebarGroup.vue";
import { navGroups, canSeeItem } from "../config/navigation";
import { useSidebar } from "../composables/useSidebar";
import { usePlanStore } from "../stores/planStore";

const props = defineProps<{
  isSystemAdmin: boolean;
  userRole?: string | null;
}>();

defineEmits<{
  navigate: [];
}>();

const { viewMode, toggleGroup, isGroupCollapsed } = useSidebar();
const planStore = usePlanStore();

const tierInfoLoaded = computed(() => Object.keys(planStore.tierRanks).length > 0);

const visibleSidebarGroups = computed(() =>
  navGroups
    .filter(
      (g) =>
        (g.simpleMode || viewMode.value === "advanced") &&
        (!g.systemAdminOnly || props.isSystemAdmin),
    )
    .map((group) => ({
      ...group,
      items: group.items.filter((item) => {
        if (!item.requiredRoles && !item.requiredTier) return true
        if (item.requiredTier && !tierInfoLoaded.value) return true
        return canSeeItem(
          item,
          { role: props.userRole || "" },
          { isAtMinimumTier: (tier: string) => planStore.isAtMinimumTier(tier) },
        )
      }),
    })),
);
</script>
