<template>
  <nav aria-label="Main navigation" class="flex-1 space-y-6">
    <template v-for="group in filteredGroups" :key="group.id">
      <SidebarGroup
        :id="group.id"
        :label="group.label"
        :collapsed="isGroupCollapsed(group.id, group.defaultCollapsed)"
        @toggle="toggleGroup(group.id, group.defaultCollapsed)"
      >
        <template v-if="group.items && group.items.length">
          <SidebarLink
            v-for="item in group.items"
            :key="item.to"
            :to="item.to"
            :icon="item.icon"
            :label="item.label"
            :exact="item.exact"
            @click="$emit('navigate')"
          />
        </template>
        <template v-else-if="group.subgroups && group.subgroups.length">
          <template v-for="sub in group.subgroups" :key="sub.label">
            <SidebarSubgroup
              v-if="sub.label"
              :label="sub.label"
              :default-open="sub.defaultOpen ?? false"
            >
              <SidebarLink
                v-for="item in sub.items"
                :key="item.to"
                :to="item.to"
                :icon="item.icon"
                :label="item.label"
                :exact="item.exact"
                @click="$emit('navigate')"
              />
            </SidebarSubgroup>
            <template v-else>
              <SidebarLink
                v-for="item in sub.items"
                :key="item.to"
                :to="item.to"
                :icon="item.icon"
                :label="item.label"
                :exact="item.exact"
                @click="$emit('navigate')"
              />
            </template>
          </template>
        </template>
      </SidebarGroup>
    </template>
  </nav>
</template>

<script setup lang="ts">
import { computed } from "vue";
import SidebarLink from "./SidebarLink.vue";
import SidebarGroup from "./SidebarGroup.vue";
import SidebarSubgroup from "./SidebarSubgroup.vue";
import { navGroups } from "../config/navigation";
import { useSidebar } from "../composables/useSidebar";

const props = defineProps<{
  isSystemAdmin: boolean;
}>();

defineEmits<{
  navigate: [];
}>();

const { viewMode, toggleGroup, isGroupCollapsed } = useSidebar();

const filteredGroups = computed(() =>
  navGroups.filter(
    (g) =>
      (g.simpleMode || viewMode.value === "advanced") &&
      (!g.systemAdminOnly || props.isSystemAdmin),
  ),
);
</script>
