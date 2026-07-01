<template>
  <nav aria-label="Main navigation" class="flex-1 space-y-6">
    <template v-for="group in filteredGroups" :key="group.id">
      <SidebarGroup
        :label="group.label"
        :collapsed="isGroupCollapsed(group.id, group.defaultCollapsed)"
        @toggle="toggleGroup(group.id, group.defaultCollapsed)"
      >
        <SidebarLink
          v-for="item in group.items"
          :key="item.to"
          :to="item.to"
          :icon="item.icon"
          :label="item.label"
          @click="$emit('navigate')"
        />
      </SidebarGroup>
    </template>
  </nav>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import SidebarLink from './SidebarLink.vue'
import SidebarGroup from './SidebarGroup.vue'
import { navGroups } from '../config/navigation'
import { useSidebar } from '../composables/useSidebar'

const props = defineProps<{
  isSystemAdmin: boolean
}>()

defineEmits<{
  navigate: []
}>()

const { viewMode, toggleGroup, isGroupCollapsed } = useSidebar()

const filteredGroups = computed(() =>
  navGroups.filter(
    (g) =>
      (g.simpleMode || viewMode.value === 'advanced') &&
      (!g.systemAdminOnly || props.isSystemAdmin),
  ),
)
</script>
