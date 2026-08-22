<template>
  <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
    <LibraryPrimitiveCard
      v-for="prim in items"
      :key="prim.id"
      :prim="prim"
      :badge="badge"
      :show-tags="showTags"
      :show-auto-update="showAutoUpdate"
      :toggle-loading="toggleLoading"
      :installed="installedMap ? !!installedMap[prim.id] : false"
      :installing="installingMap ? !!installingMap[prim.id] : false"
      :adapting="adapting"
      @create-pipeline="$emit('create-pipeline', $event)"
      @create-lifecycle-map="$emit('create-lifecycle-map', $event)"
      @view-details="$emit('view-details', $event)"
      @toggle-auto-update="$emit('toggle-auto-update', $event)"
      @install="$emit('install', $event)"
    />
  </div>
</template>

<script setup lang="ts">
import LibraryPrimitiveCard from "./LibraryPrimitiveCard.vue";
import type { LibraryPrimitive } from "./LibraryPrimitiveCard.vue";

withDefaults(
  defineProps<{
    items: LibraryPrimitive[];
    badge: "modulo" | "community" | "preview";
    showTags?: boolean;
    showAutoUpdate?: boolean;
    toggleLoading?: Record<string, boolean>;
    installedMap?: Record<string, boolean>;
    installingMap?: Record<string, boolean>;
    adapting?: Record<string, boolean>;
  }>(),
  {
    showTags: true,
    showAutoUpdate: false,
    toggleLoading: undefined,
    installedMap: undefined,
    installingMap: undefined,
    adapting: () => ({}),
  },
);

defineEmits<{
  "create-pipeline": [prim: LibraryPrimitive];
  "create-lifecycle-map": [prim: LibraryPrimitive];
  "view-details": [prim: LibraryPrimitive];
  "toggle-auto-update": [prim: LibraryPrimitive];
  install: [prim: LibraryPrimitive];
}>();
</script>
