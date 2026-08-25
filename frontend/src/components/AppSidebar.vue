<template>
  <!-- Desktop (>=768px): in-flow collapsible sidebar, default expanded -->
  <aside
    v-if="isDesktop"
    class="h-screen sticky top-0 border-r bg-background flex flex-col overflow-hidden transition-[width] duration-200"
    :class="collapsed ? 'w-16' : 'w-64 p-4 pr-3'"
  >
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

  <!-- Mobile + flag ON: 64px icon rail in-flow + full sidebar as fixed overlay panel -->
  <template v-if="!isDesktop && mobileRailFlag">
    <aside
      class="h-screen sticky top-0 border-r bg-background flex flex-col overflow-hidden w-16"
    >
      <SidebarRail
        ref="railRef"
        :is-system-admin="isSystemAdmin"
        :user-role="userRole"
        :user-permissions="userPermissions"
        :user-email="userEmail"
        :user-initial="userInitial"
        :is-light="isLight"
        class="h-full w-full py-2"
        @expand="mobileExpanded = true"
        @logout="$emit('logout')"
        @toggle-theme="$emit('toggle-theme')"
        @open-command-palette="$emit('open-command-palette')"
      />
    </aside>

    <div
      v-if="mobileExpanded"
      class="fixed inset-0 z-30 bg-black/50 md:hidden"
      @click="mobileExpanded = false"
      aria-hidden="true"
    />

    <aside
      v-if="mobileExpanded"
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

  <!-- Mobile + flag OFF: hamburger header + slide-in drawer + backdrop (restored from main) -->
  <template v-if="!isDesktop && !mobileRailFlag">
    <header
      class="md:hidden fixed top-0 left-0 right-0 z-50 flex items-center justify-between border-b bg-background px-4 h-14"
    >
      <button type="button"
        ref="mobileButtonRef"
        @click="mobileOpen = !mobileOpen"
        class="rounded-md p-2 text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
        :aria-label="mobileOpen ? $t('components.AppLayout.close_navigation') : $t('components.AppLayout.open_navigation')"
        :aria-expanded="mobileOpen"
        aria-controls="mobile-sidebar"
      >
        <Menu v-if="!mobileOpen" class="h-[22px] w-[22px]" aria-hidden="true" />
        <X v-else class="h-[22px] w-[22px]" aria-hidden="true" />
      </button>
      <div class="flex items-center gap-1.5 ml-auto" :inert="mobileOpen">
        <NotificationBell />
        <button type="button"
          @click="$emit('open-command-palette')"
          class="rounded-md p-1.5 text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
          :title="$t('components.AppLayout.search_pages_hint', { modifier: isMac ? 'Cmd' : 'Ctrl' })"
          :aria-label="$t('components.AppLayout.search_pages')"
        >
          <Search class="h-[22px] w-[22px]" aria-hidden="true" />
        </button>
        <label for="applayout-field-1" class="toggle-switch ml-2 mr-2" :class="isLight ? 'light' : 'dark'">
          <span class="track">
            <span class="thumb" />
          </span>
          <input
            id="applayout-field-1"
            type="checkbox"
            class="sr-only"
            :aria-label="$t('components.AppLayout.toggle_theme')"
            @change="$emit('toggle-theme')"
            :checked="isLight"
          />
        </label>
        <router-link to="/" class="flex items-center gap-1.5">
          <div
            class="flex items-center justify-center rounded-lg bg-primary/10 p-1.5"
          >
            <LogoMark :size="24" transparent />
          </div>
          <h2 class="hidden min-[360px]:inline text-lg font-bold tracking-tight">{{ $t('components.AppLayout.modulo') }}</h2>
          <Badge v-if="planStore.currentTier" severity="secondary" class="border border-border hidden min-[380px]:inline-flex text-[10px] px-1.5 py-0 leading-none opacity-70">
            {{ planStore.getTierLabel(planStore.currentTier) }}
          </Badge>
        </router-link>
      </div>
    </header>

    <div
      v-if="mobileOpen"
      class="md:hidden fixed inset-0 z-30 bg-black/50"
      @click="mobileOpen = false"
      aria-hidden="true"
    />

    <aside
      id="mobile-sidebar"
      ref="mobileSidebarRef"
      class="md:hidden fixed top-14 left-0 z-40 h-[calc(100vh-3.5rem)] w-64 border-r bg-background p-4 flex flex-col transition-[transform,visibility] overflow-y-auto"
      :class="mobileOpen ? 'translate-x-0' : '-translate-x-full invisible'"
      :inert="!mobileOpen"
      :aria-hidden="!mobileOpen || undefined"
    >
      <div class="flex items-center gap-2 pt-2 pb-2 border-b mb-2">
        <div class="avatar-ring">
          <div
            class="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary/10 text-xs font-bold text-primary"
            :title="userEmail"
          >
            {{ userInitial }}
          </div>
        </div>
        <router-link
          to="/admin/my-profile"
          class="text-sm text-muted-foreground truncate hover:text-foreground transition-colors flex-1 min-w-0"
          :aria-label="$t('components.AppLayout.user_profile')"
        >
          {{ userEmail }}
        </router-link>
      </div>

      <SidebarNav
        class="flex-1"
        :is-system-admin="isSystemAdmin"
        :user-role="userRole"
        :user-permissions="userPermissions"
        @navigate="mobileOpen = false"
      />

      <SidebarFooter @logout="$emit('logout')" />
    </aside>
  </template>
</template>

<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { FOCUSABLE_SELECTOR, trapTabInElement } from "../composables/useFocusTrap";
import { usePlanStore } from "../stores/planStore";
import Badge from "primevue/badge";
import LogoMark from "./LogoMark.vue";
import NotificationBell from "./NotificationBell.vue";
import SidebarFooter from "./SidebarFooter.vue";
import SidebarFull from "./SidebarFull.vue";
import SidebarNav from "./SidebarNav.vue";
import SidebarRail from "./SidebarRail.vue";
import { useSidebar } from "../composables/useSidebar";
import { useSidebarMode } from "../composables/useSidebarMode";
import { Menu, Search, X } from "@lucide/vue";

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

const planStore = usePlanStore();
const { isDesktop, mobileRailFlag } = useSidebarMode();
const { collapsed, setCollapsed } = useSidebar();

const route = useRoute();

// Flag-ON mobile path: 64px rail + overlay panel state.
const mobileExpanded = ref(false);
const mobilePanelRef = ref<HTMLElement | null>(null);
const railRef = ref<InstanceType<typeof SidebarRail> | null>(null);

// Flag-OFF mobile path: hamburger drawer state.
const mobileOpen = ref(false);
const mobileSidebarRef = ref<HTMLElement | null>(null);
const mobileButtonRef = ref<HTMLElement | null>(null);

const isMac = computed(() =>
  typeof navigator !== "undefined" && navigator.platform.includes("Mac"),
);

const showMobilePanel = computed(
  () => !isDesktop.value && mobileExpanded.value && mobileRailFlag.value,
);

function handleMobileKeydown(e: KeyboardEvent) {
  if (e.key === "Escape") {
    mobileExpanded.value = false;
    return;
  }
  if (e.key !== "Tab") return;
  trapTabInElement(e, mobilePanelRef.value);
}

function handleDrawerKeydown(e: KeyboardEvent) {
  if (e.key === "Escape") {
    mobileOpen.value = false;
    return;
  }
  if (e.key !== "Tab") return;
  trapTabInElement(e, mobileSidebarRef.value);
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

watch(mobileOpen, (open) => {
  nextTick(() => {
    if (open && mobileSidebarRef.value) {
      const firstFocusable = mobileSidebarRef.value.querySelector<HTMLElement>(
        FOCUSABLE_SELECTOR,
      );
      firstFocusable?.focus();
      document.addEventListener("keydown", handleDrawerKeydown);
    } else if (!open && mobileButtonRef.value) {
      mobileButtonRef.value.focus();
      document.removeEventListener("keydown", handleDrawerKeydown);
    }
  });
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
  document.removeEventListener("keydown", handleDrawerKeydown);
});
</script>
