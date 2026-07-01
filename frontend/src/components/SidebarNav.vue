<template>
  <nav class="flex-1 space-y-0.5">
    <template v-for="group in navGroups" :key="group.id">
      <SidebarGroup
        v-if="(group.simpleMode || viewMode === 'advanced') && (!group.systemAdminOnly || isSystemAdmin)"
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
import SidebarLink from './SidebarLink.vue'
import SidebarGroup from './SidebarGroup.vue'
import { navGroups } from '../config/navigation'
import { useSidebar } from '../composables/useSidebar'

defineProps<{
  isSystemAdmin: boolean
}>()

defineEmits<{
  navigate: []
}>()

const { viewMode, toggleGroup, isGroupCollapsed } = useSidebar()
</script>
