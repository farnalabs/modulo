<template>
  <TooltipProvider :delay-duration="300">
  <div class="flex items-start min-h-screen">
    <!-- Sidebar -->
    <aside class="hidden md:flex w-64 border-r bg-background p-4 flex-col h-screen sticky top-0 pr-3">
      <div class="mb-6 flex items-center gap-2.5 pl-1">
        <router-link to="/" class="flex items-center gap-2.5">
          <div
          class="flex items-center justify-center rounded-lg bg-primary/10 p-1.5"
        >
          <LogoMark :size="24" transparent />
        </div>
        <h2 class="text-lg font-bold tracking-tight">Modulo</h2>
          <Badge v-if="planStore.currentTier" variant="outline" class="text-[10px] px-1.5 py-0 leading-none opacity-70">
            {{ planStore.getTierLabel(planStore.currentTier) }}
          </Badge>
        </router-link>
      </div>

      <div class="flex flex-col flex-1 min-h-0">
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
            aria-label="User profile"
          >
            {{ userEmail }}
          </router-link>
        </div>

        <div class="flex items-center justify-between pb-2 mb-1 gap-2">
          <NotificationBell />
          <label for="applayout-field-2" class="toggle-switch" :class="isLight ? 'light' : 'dark'">
            <span class="track">
              <span class="thumb" />
            </span>
            <span class="flex items-center gap-1">
              <Sun v-if="isLight" class="h-3 w-3" aria-hidden="true" />
              <Moon v-else class="h-3 w-3" aria-hidden="true" />
              <span>{{ isLight ? $t('common.light') : $t('common.dark') }}</span>
            </span>
            <input id="applayout-field-2"
              type="checkbox"
              class="sr-only"
              @change="toggleTheme"
              :checked="isLight"
            />
          </label>
        </div>

        <SidebarNav class="flex-1" :is-system-admin="isSystemAdmin" :user-role="userRole" />
      </div>

      <SidebarFooter @logout="logout" />
    </aside>

    <header
      class="md:hidden fixed top-0 left-0 right-0 z-50 flex items-center justify-between border-b bg-background px-4 h-14"
    >
      <button
        ref="mobileButtonRef"
        @click="mobileOpen = !mobileOpen"
        class="rounded-md p-2 text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
        :aria-label="$t('components.AppLayout.mobileopen_close_navigation_open_navigation')"
        :aria-expanded="mobileOpen"
        aria-controls="mobile-sidebar"
      >
        <Menu v-if="!mobileOpen" class="h-[22px] w-[22px]" aria-hidden="true" />
        <X v-else class="h-[22px] w-[22px]" aria-hidden="true" />
      </button>
      <NotificationBell class="ml-auto" />
      <label for="applayout-field-1" class="toggle-switch ml-2" :class="isLight ? 'light' : 'dark'">
        <span class="track">
          <span class="thumb" />
        </span>
        <input id="applayout-field-1"
          type="checkbox"
          class="sr-only"
          @change="toggleTheme"
          :checked="isLight"
        />
      </label>
      <router-link to="/" class="flex items-center gap-2.5">
        <div
          class="flex items-center justify-center rounded-lg bg-primary/10 p-1.5"
        >
          <LogoMark :size="24" transparent />
        </div>
        <h2 class="text-lg font-bold tracking-tight">Modulo</h2>
        <Badge v-if="planStore.currentTier" variant="outline" class="text-[10px] px-1.5 py-0 leading-none opacity-70">
          {{ planStore.getTierLabel(planStore.currentTier) }}
        </Badge>
      </router-link>
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
      class="md:hidden fixed top-14 left-0 z-40 h-[calc(100vh-3.5rem)] w-64 border-r bg-background p-4 flex flex-col transition-transform overflow-y-auto"
      :class="mobileOpen ? 'translate-x-0' : '-translate-x-full'"
      @keydown.escape="mobileOpen = false"
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
          aria-label="User profile"
        >
          {{ userEmail }}
        </router-link>
      </div>

      <SidebarNav class="flex-1"
        :is-system-admin="isSystemAdmin"
        :user-role="userRole"
        @navigate="mobileOpen = false"
      />

      <SidebarFooter
        @logout="logout"
      />
    </aside>

    <main
      class="flex-1 overflow-auto bg-background pt-14 md:pt-0 relative"
      :style="remyDockedStyle"
    >
      <OnboardingBanner />
      <Breadcrumb class="px-6 pt-4 pb-3" />
      <router-view v-slot="{ Component, route }">
        <transition name="page">
          <component :is="Component" :key="route.fullPath" />
        </transition>
      </router-view>
    </main>

    <div v-if="planStore.devMode && remyStore.isExecutingUi" class="remy-execution-overlay">
      <div class="remy-execution-banner">
        <span>Remy is performing actions on this page</span>
        <button class="remy-stop-btn" @click="abortUiCommands">Stop</button>
      </div>
    </div>

    <SpotlightOverlay />
    <CommandPalette />
    <RemyPanel v-if="planStore.devMode" />
  </div>
  </TooltipProvider>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch, nextTick } from "vue";
import { getAccessToken, clearAccessToken } from "../lib/api/client";
import { usePlanStore } from "../stores/planStore";
import Badge from "./ui/badge/Badge.vue";
import Breadcrumb from "./Breadcrumb.vue";
import LogoMark from "./LogoMark.vue";
import NotificationBell from "./NotificationBell.vue";
import RemyPanel from "./remy/RemyPanel.vue";
import SidebarFooter from "./SidebarFooter.vue";
import SidebarNav from "./SidebarNav.vue";
import { TooltipProvider } from "./ui/tooltip";
import { useRemyStore } from "../composables/useRemyStore";
import { abortUiCommands } from "../composables/useUiCommandExecutor";
import OnboardingBanner from "./onboarding/OnboardingBanner.vue";
import CommandPalette from "./CommandPalette.vue";
import SpotlightOverlay from "./onboarding/SpotlightOverlay.vue";
import Sun from "@lucide/vue/icons/sun";
import Moon from "@lucide/vue/icons/moon";
import Menu from "@lucide/vue/icons/menu";
import X from "@lucide/vue/icons/x";

const planStore = usePlanStore();
const remyStore = useRemyStore();

const mobileOpen = ref(false);
const mobileSidebarRef = ref<HTMLElement | null>(null);
const mobileButtonRef = ref<HTMLElement | null>(null);

const isLight = ref(document.documentElement.classList.contains("light"));

const remyDockedStyle = computed(() =>
  remyStore.panelState === "docked" ? { paddingRight: `${remyStore.panelSize.width}px` } : undefined,
);

function toggleTheme() {
  const root = document.documentElement;
  root.classList.toggle("light");
  root.classList.toggle("dark");
  isLight.value = root.classList.contains("light");
}

function logout() {
  clearAccessToken();
  window.location.reload();
}

function decodeBase64Url(s: string): string {
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  const pad = s.length % 4;
  if (pad) s += "=".repeat(4 - pad);
  return atob(s);
}

const jwtPayload = computed(() => {
  const token = getAccessToken();
  if (!token) return null;
  try {
    return JSON.parse(decodeBase64Url(token.split(".")[1]));
  } catch {
    return null;
  }
});

watch(mobileOpen, (open) => {
  nextTick(() => {
    if (open && mobileSidebarRef.value) {
      const firstFocusable = mobileSidebarRef.value!.querySelector<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      );
      firstFocusable?.focus();
    } else if (!open && mobileButtonRef.value) {
      mobileButtonRef.value.focus();
    }
  });
});

const userEmail = computed(() => jwtPayload.value?.sub || "");

const userInitial = computed(() => {
  const email = userEmail.value;
  if (!email) return "?";
  return email.charAt(0).toUpperCase();
});

const isSystemAdmin = computed(
  () => jwtPayload.value?.is_system_admin === true,
);

const userRole = computed(() => jwtPayload.value?.org_role || null);

onMounted(() => {
  planStore.fetchPlan().catch(() => {});
});
</script>

<style scoped>
.remy-execution-overlay {
  position: fixed;
  inset: 0;
  z-index: 40;
  pointer-events: none;
}
.remy-execution-banner {
  position: fixed;
  top: 0; left: 0; right: 0;
  z-index: 41;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 16px;
  background: hsl(var(--warning) / 0.95);
  color: hsl(var(--warning-foreground));
  font-size: 13px;
  pointer-events: auto;
}
.remy-stop-btn {
  background: hsl(var(--destructive));
  color: hsl(var(--destructive-foreground));
  border: none;
  border-radius: 6px;
  padding: 4px 12px;
  font-size: 12px;
  cursor: pointer;
}
</style>
