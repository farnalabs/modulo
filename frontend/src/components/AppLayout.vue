<template>
  <TooltipProvider :delay-duration="300">
  <div class="flex items-start min-h-screen">
    <!-- Sidebar -->
    <aside class="hidden md:flex w-64 border-r bg-background p-4 flex-col h-screen sticky top-0">
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

        <div class="flex items-center justify-between pb-2 mb-1">
          <label for="applayout-field-2" class="toggle-switch" :class="isLight ? 'light' : 'dark'">
            <span class="track">
              <span class="thumb" />
            </span>
            <span class="flex items-center gap-1">
              <svg
                v-if="isLight"
                xmlns="http://www.w3.org/2000/svg"
                width="12"
                height="12"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
              >
                <circle cx="12" cy="12" r="5" />
                <line x1="12" y1="1" x2="12" y2="3" />
                <line x1="12" y1="21" x2="12" y2="23" />
                <line x1="4.22" y1="4.22" x2="5.64" y2="5.64" />
                <line x1="18.36" y1="18.36" x2="19.78" y2="19.78" />
                <line x1="1" y1="12" x2="3" y2="12" />
                <line x1="21" y1="12" x2="23" y2="12" />
                <line x1="4.22" y1="19.78" x2="5.64" y2="18.36" />
                <line x1="18.36" y1="5.64" x2="19.78" y2="4.22" />
              </svg>
              <svg
                v-else
                xmlns="http://www.w3.org/2000/svg"
                width="12"
                height="12"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
              >
                <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
              </svg>
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

        <ViewModeToggle
          :model-value="viewMode"
          :options="viewModeOptions"
          @update:model-value="setViewMode"
        />

        <SidebarNav class="flex-1" :is-system-admin="isSystemAdmin" :user-role="userRole" />
      </div>

      <SidebarFooter @logout="logout" />
    </aside>

    <!-- Mobile header -->
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
        <svg
          v-if="!mobileOpen"
          xmlns="http://www.w3.org/2000/svg"
          width="22"
          height="22"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
        >
          <line x1="3" y1="6" x2="21" y2="6" />
          <line x1="3" y1="12" x2="21" y2="12" />
          <line x1="3" y1="18" x2="21" y2="18" />
        </svg>
        <svg
          v-else
          xmlns="http://www.w3.org/2000/svg"
          width="22"
          height="22"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
        >
          <line x1="18" y1="6" x2="6" y2="18" />
          <line x1="6" y1="6" x2="18" y2="18" />
        </svg>
      </button>
      <NotificationBell class="ml-auto mr-2" />
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

    <!-- Mobile overlay -->
    <div
      v-if="mobileOpen"
      class="md:hidden fixed inset-0 z-30 bg-black/50"
      @click="mobileOpen = false"
      aria-hidden="true"
    />

    <!-- Mobile sidebar -->
    <!-- The sidebar owns Escape handling while focus is within its descendants. -->
    <!-- eslint-disable-next-line vuejs-accessibility/no-static-element-interactions -->
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

      <div class="flex items-center justify-between pb-2 mb-1">
        <label for="applayout-field-1" class="toggle-switch" :class="isLight ? 'light' : 'dark'">
          <span class="track">
            <span class="thumb" />
          </span>
          <span class="flex items-center gap-1">
            <svg
              v-if="isLight"
              xmlns="http://www.w3.org/2000/svg"
              width="12"
              height="12"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
            >
              <circle cx="12" cy="12" r="5" />
              <line x1="12" y1="1" x2="12" y2="3" />
              <line x1="12" y1="21" x2="12" y2="23" />
              <line x1="4.22" y1="4.22" x2="5.64" y2="5.64" />
              <line x1="18.36" y1="18.36" x2="19.78" y2="19.78" />
              <line x1="1" y1="12" x2="3" y2="12" />
              <line x1="21" y1="12" x2="23" y2="12" />
              <line x1="4.22" y1="19.78" x2="5.64" y2="18.36" />
              <line x1="18.36" y1="5.64" x2="19.78" y2="4.22" />
            </svg>
            <svg
              v-else
              xmlns="http://www.w3.org/2000/svg"
              width="12"
              height="12"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
            >
              <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
            </svg>
            <span>{{ isLight ? $t('common.light') : $t('common.dark') }}</span>
          </span>
          <input id="applayout-field-1"
            type="checkbox"
            class="sr-only"
            @change="toggleTheme"
            :checked="isLight"
          />
        </label>
      </div>

      <ViewModeToggle
        :model-value="viewMode"
        :options="viewModeOptions"
        @update:model-value="setViewMode"
      />

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

    <!-- Remy execution overlay -->
    <div v-if="remyStore.isExecutingUi" class="remy-execution-overlay">
      <div class="remy-execution-banner">
        <span>Remy is performing actions on this page</span>
        <button class="remy-stop-btn" @click="abortUiCommands">Stop</button>
      </div>
    </div>

    <SpotlightOverlay />
    <RemyPanel />
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
import ViewModeToggle from "./ViewModeToggle.vue";
import { TooltipProvider } from "./ui/tooltip";
import { useSidebar } from "../composables/useSidebar";
import { useRemyStore } from "../composables/useRemyStore";
import { abortUiCommands } from "../composables/useUiCommandExecutor";
import OnboardingBanner from "./onboarding/OnboardingBanner.vue";
import SpotlightOverlay from "./onboarding/SpotlightOverlay.vue";

const viewModeOptions = [
  { label: 'Essentials', value: 'simple' },
  { label: 'All Features', value: 'advanced' },
] as const;

const { viewMode, setViewMode } = useSidebar();

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
