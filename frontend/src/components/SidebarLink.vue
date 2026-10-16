<template>
  <router-link
    :to="to"
    :class="[
      'sidebar-link',
      {
        active: isActive,
        'sidebar-link--collapsed': collapsed,
        'sidebar-link--collapsed--touch': collapsed && size === 'touch',
      },
    ]"
    :aria-current="isActive ? 'page' : undefined"
    :title="collapsed ? labelText : undefined"
    :aria-label="collapsed ? labelText : undefined"
    v-bind="$attrs"
  >
    <span class="h-4 w-4 shrink-0"><SvgIcon :name="icon" /></span>
    <template v-if="!collapsed">
      <span class="truncate" :title="labelText">{{ labelText }}</span>
      <span v-if="visibility === 'public_preview'" class="badge badge-context-preview ml-auto text-[10px] leading-none py-0.5">{{ $t('components.SidebarLink.preview') }}</span>
      <span v-else-if="visibility === 'private_preview'" class="badge badge-context-purple ml-auto text-[10px] leading-none py-0.5">{{ $t('components.SidebarLink.dev_preview') }}</span>
      <span v-else-if="visibility === 'in_dev'" class="badge badge-context-amber ml-auto text-[10px] leading-none py-0.5">{{ $t('components.SidebarLink.in_dev') }}</span>
    </template>
  </router-link>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import { useI18n } from "vue-i18n";
import SvgIcon from "./SvgIcon.vue";

const props = defineProps<{
  to: string;
  icon: string;
  label: string;
  labelKey?: string;
  exact?: boolean;
  visibility?: 'public' | 'public_preview' | 'private_preview' | 'in_dev'
  collapsed?: boolean;
  /**
   * Hit-target profile for the collapsed (rail) variant. `touch` widens the
   * 40x40 collapsed box to 44x44 (WCAG 2.5.8); absent/`default` is unchanged.
   */
  size?: 'default' | 'touch';
}>();

const { t } = useI18n();

const labelText = computed(() => (props.labelKey ? t(props.labelKey) : props.label) || "");

const route = useRoute();

const isActive = computed(() =>
  route.path === props.to ||
  (!props.exact && props.to !== "/" && route.path.startsWith(props.to + "/"))
);
</script>

<style scoped>
.sidebar-link--collapsed {
  justify-content: center;
  width: 2.5rem;
  height: 2.5rem;
  padding: 0.5rem;
}

.sidebar-link--collapsed.active {
  border-left: none;
  padding-left: 0.5rem;
}

/* Touch surface only: widen the collapsed rail box from 40x40 to 44x44.
   Declared after `.sidebar-link--collapsed` so it wins at equal specificity;
   it is never applied unless `size === 'touch'`. */
.sidebar-link--collapsed--touch {
  width: 2.75rem;
  height: 2.75rem;
}
</style>
