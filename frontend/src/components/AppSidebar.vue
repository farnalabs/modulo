<template>
  <aside
    class="h-screen sticky top-0 border-r bg-background flex flex-col overflow-hidden transition-[width] duration-200"
    :class="showRail ? 'w-16' : 'w-64 p-4 pr-3'"
  >
    <SidebarRail
      v-if="showRail"
      ref="railRef"
      :is-system-admin="isSystemAdmin"
      :user-role="userRole"
      :user-permissions="userPermissions"
      :user-email="userEmail"
      :user-initial="userInitial"
      :is-light="isLight"
      class="h-full w-full py-2"
      @expand="onExpand"
      @logout="$emit('logout')"
      @toggle-theme="$emit('toggle-theme')"
      @open-command-palette="$emit('open-command-palette')"
    />
    <SidebarFull
      v-else
      :is-system-admin="isSystemAdmin"
      :user-role="userRole"
      :user-permissions="userPermissions"
      :user-email="userEmail"
      :user-initial="userInitial"
      :is-light="isLight"
      class="flex flex-col flex-1 min-h-0"
      @collapse="onCollapse"
      @logout="$emit('logout')"
      @toggle-theme="$emit('toggle-theme')"
      @open-command-palette="$emit('open-command-palette')"
    />
  </aside>

  <div
    v-if="showMobilePanel"
    class="fixed inset-0 z-30 bg-black/50 md:hidden"
    @click="mobileExpanded = false"
    aria-hidden="true"
  />

  <aside
    v-if="showMobilePanel"
    ref="mobilePanelRef"
    role="dialog"
    aria-modal="true"
    :aria-label="$t('components.AppLayout.main_navigation')"
    class="fixed top-0 left-0 z-40 h-full w-64 border-r bg-background p-4 flex flex-col overflow-y-auto md:hidden"
  >
    <SidebarFull
      :is-system-admin="isSystemAdmin"
      :user-role="userRole"
      :user-permissions="userPermissions"
      :user-email="userEmail"
      :user-initial="userInitial"
      :is-light="isLight"
      class="flex flex-col flex-1 min-h-0"
      @collapse="mobileExpanded = false"
      @navigate="mobileExpanded = false"
      @logout="$emit('logout')"
      @toggle-theme="$emit('toggle-theme')"
      @open-command-palette="$emit('open-command-palette')"
    />
  </aside>
</template>

<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from "vue";
import { useMediaQuery } from "@vueuse/core";
import { useRoute } from "vue-router";
import SidebarFull from "./SidebarFull.vue";
import SidebarRail from "./SidebarRail.vue";
import { useSidebar } from "../composables/useSidebar";

const props = defineProps<{
  isSystemAdmin: boolean;
  userRole?: string | null;
  userPermissions?: string[];
  userEmail: string;
  userInitial: string;
  isLight: boolean;
}>();

defineEmits<{
  logout: [];
  "toggle-theme": [];
  "open-command-palette": [];
}>();

const isDesktop = useMediaQuery("(min-width: 768px)");
const { collapsed, setCollapsed } = useSidebar();

const route = useRoute();
const mobileExpanded = ref(false);
const mobilePanelRef = ref<HTMLElement | null>(null);
const railRef = ref<InstanceType<typeof SidebarRail> | null>(null);

const FOCUSABLE_SELECTOR =
  'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

const showRail = computed(() => !isDesktop.value || collapsed.value);
const showMobilePanel = computed(() => !isDesktop.value && mobileExpanded.value);

function onExpand() {
  if (isDesktop.value) setCollapsed(false);
  else mobileExpanded.value = true;
}

function onCollapse() {
  if (isDesktop.value) setCollapsed(true);
  else mobileExpanded.value = false;
}

function handleMobileKeydown(e: KeyboardEvent) {
  if (e.key === "Escape") {
    mobileExpanded.value = false;
    return;
  }
  if (e.key !== "Tab") return;

  const dialog = mobilePanelRef.value;
  if (!dialog) return;

  const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
  if (focusable.length === 0) {
    e.preventDefault();
    return;
  }

  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const active = document.activeElement;
  const focusInside = active instanceof HTMLElement && dialog.contains(active);

  if (e.shiftKey) {
    if (active === first || !focusInside) {
      e.preventDefault();
      last.focus();
    }
  } else if (active === last || !focusInside) {
    e.preventDefault();
    first.focus();
  }
}

function focusRailExpandButton() {
  const railEl = railRef.value?.$el;
  if (railEl instanceof HTMLElement) {
    railEl.querySelector<HTMLElement>("button")?.focus();
  }
}

watch(showMobilePanel, (open) => {
  if (open) {
    document.addEventListener("keydown", handleMobileKeydown);
    nextTick(() => {
      const firstFocusable = mobilePanelRef.value?.querySelector<HTMLElement>(
        FOCUSABLE_SELECTOR,
      );
      firstFocusable?.focus();
    });
  } else {
    document.removeEventListener("keydown", handleMobileKeydown);
    focusRailExpandButton();
  }
});

watch(
  () => route.path,
  () => {
    if (showMobilePanel.value) {
      mobileExpanded.value = false;
    }
  },
);

onUnmounted(() => {
  document.removeEventListener("keydown", handleMobileKeydown);
});
</script>
