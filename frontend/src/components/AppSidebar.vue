<template>
  <!-- Desktop (>=768px): in-flow collapsible sidebar, default expanded -->
  <aside
    v-if="isDesktop"
    class="h-screen sticky top-0 border-r bg-background flex flex-col overflow-hidden transition-[width] duration-200 motion-reduce:transition-none"
    :class="collapsed ? 'w-16' : 'w-64 p-4 pr-3'"
    :aria-label="$t('components.AppLayout.main_navigation')"
    aria-labelledby="desktop-navigation-heading"
  >
    <h2 id="desktop-navigation-heading" class="sr-only">{{ $t('components.AppLayout.main_navigation') }}</h2>
    <SidebarRail
      v-if="collapsed"
      ref="railRef"
      :is-system-admin="isSystemAdmin"
      :user-role="userRole"
      :user-permissions="userPermissions"
      :user-email="userEmail"
      :user-initial="userInitial"
      :is-light="isLight"
      class="h-full w-full py-2"
      @expand="setCollapsed(false)"
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
      @collapse="setCollapsed(true)"
      @logout="$emit('logout')"
      @toggle-theme="$emit('toggle-theme')"
      @open-command-palette="$emit('open-command-palette')"
    />
  </aside>

  <!-- Mobile: 64px icon rail in-flow + full sidebar as fixed overlay panel -->
  <template v-else>
    <aside
      class="h-screen sticky top-0 border-r bg-background flex flex-col overflow-hidden w-16"
      :aria-label="$t('components.AppLayout.main_navigation')"
      aria-labelledby="mobile-rail-navigation-heading"
    >
      <h2 id="mobile-rail-navigation-heading" class="sr-only">{{ $t('components.AppLayout.main_navigation') }}</h2>
      <SidebarRail
        ref="railRef"
        :is-system-admin="isSystemAdmin"
        :user-role="userRole"
        :user-permissions="userPermissions"
        :user-email="userEmail"
        :user-initial="userInitial"
        :is-light="isLight"
        size="touch"
        class="h-full w-full py-2"
        @expand="mobileExpanded = true"
        @logout="$emit('logout')"
        @toggle-theme="$emit('toggle-theme')"
        @open-command-palette="$emit('open-command-palette')"
      />
    </aside>

    <dialog
      v-if="mobileExpanded"
      id="mobile-nav-panel"
      ref="mobilePanelRef"
      :aria-label="$t('components.AppLayout.main_navigation')"
      aria-labelledby="mobile-panel-navigation-heading"
      class="fixed top-0 left-0 z-40 m-0 h-full w-64 max-h-none max-w-none border-r bg-background p-4 flex flex-col overflow-y-auto text-foreground backdrop:bg-black/50 md:hidden"
      @click="onMobilePanelBackdropClick"
      @keydown.escape="closeMobilePanel"
      @cancel="closeMobilePanel"
      @close="mobileExpanded = false"
    >
      <h2 id="mobile-panel-navigation-heading" class="sr-only">{{ $t('components.AppLayout.main_navigation') }}</h2>
      <SidebarFull
        :is-system-admin="isSystemAdmin"
        :user-role="userRole"
        :user-permissions="userPermissions"
        :user-email="userEmail"
        :user-initial="userInitial"
        :is-light="isLight"
        class="flex flex-col flex-1 min-h-0"
        @collapse="closeMobilePanel"
        @navigate="closeMobilePanel"
        @logout="$emit('logout')"
        @toggle-theme="$emit('toggle-theme')"
        @open-command-palette="onOpenCommandPalette"
      />
    </dialog>
  </template>
</template>

<script setup lang="ts">
import { onUnmounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { useSidebar } from "../composables/useSidebar";
import { useSidebarMode } from "../composables/useSidebarMode";
import SidebarFull from "./SidebarFull.vue";
import SidebarRail from "./SidebarRail.vue";

defineProps<{
  isSystemAdmin: boolean;
  userRole?: string | null;
  userPermissions?: string[];
  userEmail: string;
  userInitial: string;
  isLight: boolean;
}>();

const emit = defineEmits<{
  logout: [];
  "toggle-theme": [];
  "open-command-palette": [];
}>();

const { isDesktop } = useSidebarMode();
const { collapsed, setCollapsed } = useSidebar();
const route = useRoute();

const mobileExpanded = ref(false);
const mobilePanelRef = ref<HTMLDialogElement | null>(null);
const railRef = ref<InstanceType<typeof SidebarRail> | null>(null);

function focusRailExpandButton() {
  const railEl = railRef.value?.$el;
  if (railEl instanceof HTMLElement) {
    railEl.querySelector<HTMLElement>("button")?.focus();
  }
}

function onMobilePanelBackdropClick(event: MouseEvent) {
  if (event.target === event.currentTarget) closeMobilePanel();
}

function closeMobilePanel(): void {
  mobileExpanded.value = false;
  const dialog = mobilePanelRef.value;
  if (dialog?.open) dialog.close();
}

function onOpenCommandPalette(): void {
  closeMobilePanel();
  emit("open-command-palette");
}

watch(
  () => !isDesktop.value && mobileExpanded.value,
  (open) => {
    if (open) {
      const dialog = mobilePanelRef.value;
      if (dialog && !dialog.open) dialog.showModal();
    } else {
      focusRailExpandButton();
    }
  },
  { flush: "post" },
);

watch(
  () => route.path,
  () => {
    if (mobileExpanded.value) closeMobilePanel();
  },
);

onUnmounted(() => {
  mobileExpanded.value = false;
});
</script>
