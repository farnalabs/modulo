<template>
  <TooltipProvider :delay-duration="300">
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
      :class="onboardingActive ? 'pt-[8.25rem] md:pt-20' : 'pt-14 md:pt-0'"
      :style="remyDockedStyle"
    >
      <div class="absolute top-0 left-0 right-0 z-10">
        <OnboardingBanner />
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
        <button class="remy-stop-btn" @click="abortUiCommands">{{ $t('components.AppLayout.remy_stop') }}</button>
      </div>
    </div>

    <SpotlightOverlay />
    <CommandPalette ref="commandPaletteRef" />
    <RemyPanel v-if="planStore.devMode" />
  </div>
  </TooltipProvider>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import { getAccessToken, clearAccessToken } from "../lib/api/client";
import { usePlanStore } from "../stores/planStore";
import Breadcrumb from "./Breadcrumb.vue";
import RemyPanel from "./remy/RemyPanel.vue";
import AppSidebar from "./AppSidebar.vue";
import { TooltipProvider } from "./ui/tooltip";
import { useRemyStore } from "../composables/useRemyStore";
import { useOnboardingStore } from "../composables/useOnboarding";
import { abortUiCommands } from "../composables/useUiCommandExecutor";
import OnboardingBanner from "./onboarding/OnboardingBanner.vue";
import CommandPalette from "./CommandPalette.vue";
import SpotlightOverlay from "./onboarding/SpotlightOverlay.vue";

const planStore = usePlanStore();
const remyStore = useRemyStore();
const onboardingStore = useOnboardingStore();

const onboardingActive = computed(() => onboardingStore.isActive);

const commandPaletteRef = ref<InstanceType<typeof CommandPalette> | null>(null);

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

function openCommandPalette() {
  commandPaletteRef.value?.open()
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
