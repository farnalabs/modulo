<template>
  <div class="flex items-start min-h-screen overflow-x-clip">
    <AppSidebar
      :is-system-admin="isSystemAdmin"
      :user-role="userRole"
      :user-permissions="userPermissions"
      :user-email="userEmail"
      :user-initial="userInitial"
      :is-light="isLight"
      @logout="logout"
      @toggle-theme="toggleTheme"
      @open-command-palette="openCommandPalette"
    />

    <main
      class="flex-1 min-w-0 overflow-auto bg-background relative"
      :class="mainPaddingClass"
      :style="remyDockedStyle"
    >
      <div class="relative z-10 space-y-2">
        <DbCapacityBanner />
        <OnboardingBanner />
        <div class="px-6">
          <ProductAnalyticsConsentPrompt />
        </div>
      </div>
      <Breadcrumb class="px-6 pt-4 pb-3" />
      <router-view v-slot="{ Component, route }">
        <transition name="page">
          <component :is="Component" :key="route.fullPath" />
        </transition>
      </router-view>
    </main>

    <div v-if="planStore.devMode && remyStore.isExecutingUi" class="remy-execution-overlay">
      <div class="remy-execution-banner">
        <span>{{ $t('components.AppLayout.remy_performing_actions') }}</span>
        <button type="button" class="remy-stop-btn" @click="abortUiCommands">{{ $t('components.AppLayout.remy_stop') }}</button>
      </div>
    </div>

    <SpotlightOverlay />
    <CommandPalette ref="commandPaletteRef" />
    <RemyPanel v-if="planStore.devMode" />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import { getAccessToken, clearAccessToken } from "../lib/api/client";
import { decodeJwtPayload } from "../lib/jwt";
import { usePlanStore } from "../stores/planStore";
import Breadcrumb from "./Breadcrumb.vue";
import RemyPanel from "./remy/RemyPanel.vue";
import AppSidebar from "./AppSidebar.vue";
import { useRemyStore } from "../composables/useRemyStore";
import { useSidebarMode } from "../composables/useSidebarMode";
import { useOnboardingStore } from "../composables/useOnboarding";
import { abortUiCommands } from "../composables/useUiCommandExecutor";
import { applyPrimeVueTokenBridge } from "../lib/primevue-theme";
import OnboardingBanner from "./onboarding/OnboardingBanner.vue";
import DbCapacityBanner from "./DbCapacityBanner.vue";
import CommandPalette from "./CommandPalette.vue";
import SpotlightOverlay from "./onboarding/SpotlightOverlay.vue";
import ProductAnalyticsConsentPrompt from "./product-analytics/ProductAnalyticsConsentPrompt.vue";

const planStore = usePlanStore();
const remyStore = useRemyStore();
const onboardingStore = useOnboardingStore();

const onboardingActive = computed(() => onboardingStore.isActive);

const commandPaletteRef = ref<InstanceType<typeof CommandPalette> | null>(null);

const isLight = ref(document.documentElement.classList.contains("light"));

// The mobile hamburger header (drawn by AppSidebar when the mobile rail flag is
// OFF) is a fixed h-14 overlay, so main content needs a pt-14 offset on mobile
// only when that header is shown. When the mobile rail flag is ON there is no
// fixed header — the rail is in-flow — so no offset. Shared with AppSidebar via
// useSidebarMode so the two can't drift.
const { showMobileHeader } = useSidebarMode();

// Single mutually-exclusive padding source: the fixed mobile header (3.5rem)
// and the onboarding banner (8.25rem mobile / 5rem desktop) are both offsets
// that would collide if applied as separate additive class bindings — when both
// conditions are true on mobile one class silently wins and the other is lost.
// Only one computed drives <main>'s padding-top.
const mainPaddingClass = computed(() => {
  if (onboardingActive.value) {
    // Banner is in-flow at the top of <main>; on mobile it must clear the fixed
    // header (3.5rem) AND reserve its own height (8.25rem).
    return showMobileHeader.value
      ? 'pt-[calc(3.5rem+8.25rem)] md:pt-20'
      : 'pt-[8.25rem] md:pt-20'
  }
  return showMobileHeader.value ? 'pt-14 md:pt-0' : ''
});

const remyDockedStyle = computed(() =>
  remyStore.panelState === "docked" ? { paddingRight: `${remyStore.panelSize.width}px` } : undefined,
);

function toggleTheme() {
  const root = document.documentElement;
  if (root.classList.contains("dark")) {
    // Switch to light: remove .dark, add .light
    root.classList.remove("dark");
    root.classList.add("light");
  } else {
    // Switch to dark: remove .light, add .dark
    root.classList.remove("light");
    root.classList.add("dark");
  }
  isLight.value = root.classList.contains("light");
  // Re-apply the PrimeVue token bridge so the `--p-*` mappings re-read the
  // now-active (light/dark) source HSL variables (ADR 024 Decision 4).
  applyPrimeVueTokenBridge();
}

function openCommandPalette() {
  commandPaletteRef.value?.open()
}

function logout() {
  clearAccessToken();
  window.location.reload();
}

interface AppLayoutJwtPayload {
  sub?: string;
  is_system_admin?: boolean;
  org_role?: string | null;
  permissions?: unknown;
}

const jwtPayload = computed<AppLayoutJwtPayload | null>(() =>
  decodeJwtPayload(getAccessToken()) as AppLayoutJwtPayload | null,
);

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

const userPermissions = computed<string[]>(() => {
  const perms = jwtPayload.value?.permissions;
  return Array.isArray(perms) ? (perms as string[]) : [];
});

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
