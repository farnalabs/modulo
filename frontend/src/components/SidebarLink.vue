<template>
  <router-link
    :to="to"
    :class="['sidebar-link', { active: isActive }]"
    :aria-current="isActive ? 'page' : undefined"
    v-bind="$attrs"
  >
    <span class="h-4 w-4 shrink-0"><SvgIcon :name="icon" /></span>
    <span class="truncate" :title="labelKey ? $t(labelKey) : label">{{ labelKey ? $t(labelKey) : label }}</span>
    <span v-if="visibility === 'public_preview'" class="badge badge-context-preview ml-auto text-[10px] leading-none py-0.5">{{ $t('components.SidebarLink.preview') }}</span>
    <span v-else-if="visibility === 'private_preview'" class="badge badge-context-purple ml-auto text-[10px] leading-none py-0.5">{{ $t('components.SidebarLink.dev_preview') }}</span>
    <span v-else-if="visibility === 'in_dev'" class="badge badge-context-amber ml-auto text-[10px] leading-none py-0.5">{{ $t('components.SidebarLink.in_dev') }}</span>
  </router-link>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import SvgIcon from "./SvgIcon.vue";

const props = defineProps<{
  to: string;
  icon: string;
  label: string;
  labelKey?: string;
  exact?: boolean;
  visibility?: 'public' | 'public_preview' | 'private_preview' | 'in_dev'
}>();

const route = useRoute();

const isActive = computed(() =>
  route.path === props.to ||
  (!props.exact && props.to !== "/" && route.path.startsWith(props.to + "/"))
);
</script>
