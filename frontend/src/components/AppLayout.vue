<template>
  <div class="flex items-start min-h-screen overflow-x-clip">
    <AppSidebar
      :is-system-admin="isSystemAdmin"
      :user-role="userRole"
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
    >
      <div class="relative z-10 space-y-2">
        <DbCapacityBanner v-if="isAdminUser" />
        <!--
          FAR-2075: the renewal countdown / grace dialog belongs to the shell,
          not to /settings/license. An administrator who never opens the licence
          page would otherwise never see it, and the warning has to stay until
          the licence is renewed. The state comes from the shell's status query
          below (the notice itself only renders it), so renewal clears the
          warning; dismissing the grace dialog only hides it until the next
          visit.
        -->
        <div v-if="showsLicenceNotice" class="px-6">
          <LicenceRenewalNotice
            :status="licenceStatusValue"
            @install-key="openLicenceKeyInput"
          />
        </div>
        <DemoBanner />
        <OnboardingBanner />
        <div class="px-6">
          <ProductAnalyticsConsentPrompt />
        </div>
      </div>
      <Breadcrumb class="px-6 pt-4 pb-3" />
      <router-view v-slot="{ Component, route }">
        <transition name="page" mode="out-in">
          <component :is="Component" :key="route.fullPath" />
        </transition>
      </router-view>
    </main>

    <div v-if="planStore.devMode && assistantStore.isExecutingUi" class="assistant-execution-overlay">
      <div class="assistant-execution-banner">
        <span>{{ $t('components.AppLayout.assistant_performing_actions') }}</span>
        <button type="button" class="assistant-stop-btn" @click="abortUiCommands">{{ $t('components.AppLayout.assistant_stop') }}</button>
      </div>
    </div>

    <SpotlightOverlay />
    <CommandPalette ref="commandPaletteRef" />
    <AssistantPanel v-if="planStore.devMode" />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, nextTick, onMounted, provide, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api, clearAccessTokenForLogout } from "../lib/api/client";
import { useCurrentUser } from "../composables/useCurrentUser";
import { useDataFetch } from "../composables/useDataFetch";
import { useLicenceAccess } from "../composables/useLicenceAccess";
import { usePlanStore } from "../stores/planStore";
import Breadcrumb from "./Breadcrumb.vue";
import AssistantPanel from "./assistant/AssistantPanel.vue";
import AppSidebar from "./AppSidebar.vue";
import { useAssistantStore } from "../composables/useAssistantStore";
import { useSidebarMode } from "../composables/useSidebarMode";
import { abortUiCommands } from "../composables/useUiCommandExecutor";
import { applyPrimeVueTokenBridge } from "../lib/primevue-theme";
import OnboardingBanner from "./onboarding/OnboardingBanner.vue";
import DbCapacityBanner from "./DbCapacityBanner.vue";
import DemoBanner from "./DemoBanner.vue";
import CommandPalette from "./CommandPalette.vue";
import SpotlightOverlay from "./onboarding/SpotlightOverlay.vue";
import ProductAnalyticsConsentPrompt from "./product-analytics/ProductAnalyticsConsentPrompt.vue";
import LicenceRenewalNotice from "./licence/LicenceRenewalNotice.vue";
import type { components } from "../lib/api/schema";

const planStore = usePlanStore();
const assistantStore = useAssistantStore();

const commandPaletteRef = ref<InstanceType<typeof CommandPalette> | null>(null);

const isLight = ref(document.documentElement.classList.contains("light"));

// The fixed mobile chrome — the legacy hamburger header AND the neutral
// pending placeholder AppSidebar draws while the nav flag is unresolved
// (FAR-1237) — is a fixed h-14 overlay, so main content needs a pt-14 offset
// on mobile whenever that chrome is shown. When the resolved mode is the left
// rail there is no fixed header (the rail is in-flow) so no offset. Shared
// with AppSidebar via useSidebarMode so the two can't drift.
const { showMobileHeader } = useSidebarMode();

// <main>'s only top offset is the fixed mobile chrome described above
// (h-14 / 3.5rem). The onboarding banner needs NO offset: it is in normal flow
// (OnboardingBanner.vue `.onboarding-banner { position: relative }`) and
// reserves its own height, so padding <main> for it double-counts the gap. The
// old 8.25rem/5rem banner terms were a stale leftover from when the banner was
// an overlay.
const mainPaddingClass = computed(() =>
  showMobileHeader.value ? 'pt-14 md:pt-0' : '',
);

// The Assistant panel is a position:fixed overlay (AssistantPanel .assistant-panel) in every
// open state, including "docked" — it floats above the page on the right with
// an elevation shadow and never reserves layout space. Main content therefore
// always spans the full available width (no padding-right reservation, no
// max-width cap: Duncan explicitly wants pages to use the full screen width).

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
  // qa iter 2: an explicit user logout must NOT persist the demo-ended
  // tombstone — a demo visitor clicking Sign out chose to leave, so after the
  // reload they land on the normal login flow (App.vue sees no token, no demo
  // marker, no tombstone) instead of being re-minted into /demo against their
  // will. Involuntary clears (token expiry, 401 recovery) keep the default
  // tombstone so auto-login cannot escalate a dead demo session.
  clearAccessTokenForLogout();
  window.location.reload();
}

const { userId, isSystemAdmin: isSystemAdminFlag, orgRole } = useCurrentUser();

const userEmail = computed(() => userId.value || "");

const userInitial = computed(() => {
  const email = userEmail.value;
  if (!email) return "?";
  return email.charAt(0).toUpperCase();
});

const isSystemAdmin = isSystemAdminFlag;

const userRole = computed(() => orgRole.value || null);

// FAR-2020: the permission list is deliberately NOT derived here and passed
// down. The access token mints no `permissions` claim, so a JWT-derived array
// was always `[]` — and an empty override used to win over the endpoint's
// resolved list, hiding every permission-gated nav item. The sidebar's nav
// context now reads the list from the plan store (the caller-scoped
// `/api/v1/auth/permissions` read), which is the single source.

// Mirrors the backend guard on GET /api/v1/admin/db-capacity
// (require_system_or_org_admin): system admin OR org role "admin". Non-admins
// never mount DbCapacityBanner, so its poll request is never made and the
// browser console stays free of 401/403 erroring-request noise.
const isAdminUser = computed(
  () => isSystemAdmin.value || userRole.value === "admin",
);

// ---------------------------------------------------------------------------
// FAR-2075 — licence renewal notice (shell-level, admin-only)
//
// The notice has to be visible on every authenticated route, not only on
// /settings/license, so the status read belongs here: this component mounts
// once per authenticated session, never per route change. The same query is
// PROVIDED to the licence page, which consumes it instead of issuing a second
// GET against /api/v1/admin/license on every visit.
//
// The key is a documented string shared with frontend/src/views/
// SettingsLicenseView.vue — there is no module both files can import (a
// per-SFC export would drag the whole shell into the page's chunk).
// ---------------------------------------------------------------------------
const LICENCE_STATUS_INJECTION_KEY = "modulo:licence-status";

// FAR-2104: the notice's audience is the set of principals that can act on
// it — derived from `org.license.manage`, the permission the licence read and
// the org-scoped writes enforce, never from a parallel role check. A principal
// without it never fetches the status (the request would 403) and never sees
// licence-administration copy.
const { canManageOrgLicence } = useLicenceAccess();

const licenceStatus = useDataFetch<components["schemas"]["LicenseStatusResponse"]>(
  () => (api as any).GET("/api/v1/admin/license"),
  {
    initialValue: {
      has_license: false,
      tier: "community",
      features: [],
      expires_at: null,
      org_id: null,
      renewal_mode: "manual",
      licence_state: "none",
    },
    // The permission list resolves asynchronously (GET /auth/permissions), so
    // the query starts disabled and is kicked off by the watcher below the
    // moment the capability becomes true. `immediate: true` here would read
    // the empty bootstrap list and never fetch.
    immediate: false,
  },
);

watch(
  canManageOrgLicence,
  (canManage) => {
    if (canManage) void licenceStatus.load();
  },
  { immediate: true },
);

const licenceStatusValue = computed(() => licenceStatus.data.value);

const showsLicenceNotice = computed(
  () =>
    canManageOrgLicence.value &&
    ["renewal_due", "grace", "lapsed"].includes(licenceStatusValue.value.licence_state),
);

// Provided unconditionally: the query is inert until the permission resolves
// (the watcher above starts it), and the licence page — reachable only with
// the same permission — consumes it instead of issuing a second read.
provide(LICENCE_STATUS_INJECTION_KEY, {
  data: licenceStatus.data,
  loading: licenceStatus.loading,
  error: licenceStatus.error,
  load: licenceStatus.load,
});

const route = useRoute();
const router = useRouter();

/**
 * Send the administrator to the licence key input from anywhere in the app.
 *
 * The licence page renders its textarea only after its own status read
 * resolves, so poll for the node on a bounded number of frames rather than
 * focusing one that is not there yet. When the page is already mounted (the
 * shared status is resolved) the first frame finds it.
 */
async function openLicenceKeyInput() {
  if (route.name !== "settings-license") {
    await router.push({ name: "settings-license" });
  }
  const findAndFocusInput = async (attempt: number): Promise<void> => {
    // The textarea's own ``data-testid`` — not PrimeVue's generated element
    // id, which changes with the rendered field order. The org-scoped input is
    // the primary target; the system-scoped one is the fallback for a principal
    // that holds only the system permission.
    const input = document.querySelector<HTMLElement>(
      '[data-testid="license-key-input"], [data-testid="license-key-input-system"]',
    );
    if (input) {
      input.scrollIntoView?.({ behavior: "smooth", block: "center" });
      input.focus();
      return;
    }
    if (attempt >= 119) return;
    await nextTick();
    await nextFrame();
    await findAndFocusInput(attempt + 1);
  };
  await findAndFocusInput(0);
}

function nextFrame(): Promise<void> {
  return new Promise((resolve) => {
    // jsdom (unit tests) and older embedded webviews may not expose rAF.
    if (typeof requestAnimationFrame === "function") {
      requestAnimationFrame(() => resolve());
      return;
    }
    setTimeout(resolve, 16);
  });
}

onMounted(() => {
  planStore.fetchPlan().catch(() => {});
});
</script>

<style scoped>
.assistant-execution-overlay {
  position: fixed;
  inset: 0;
  z-index: 40;
  pointer-events: none;
}
.assistant-execution-banner {
  position: fixed;
  inset: 0 0 auto;
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
.assistant-stop-btn {
  background: hsl(var(--destructive-fill));
  color: hsl(var(--destructive-foreground));
  border: none;
  border-radius: 6px;
  padding: 4px 12px;
  font-size: 12px;
  cursor: pointer;
}
</style>
