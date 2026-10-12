<template>
  <div ref="rootRef" class="relative inline-flex">
    <button
      ref="bellButtonRef"
      type="button"
      data-testid="notification-bell-button"
      class="relative inline-flex items-center justify-center rounded-md p-2 text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
      :aria-label="$t('components.NotificationBell.notifications')"
      aria-haspopup="true"
      :aria-expanded="dropdownOpen"
      aria-controls="notification-dropdown"
      @click="toggleDropdown"
    >
      <Bell class="h-[18px] w-[18px]" aria-hidden="true" />
      <span
        v-if="store.unreadCount > 0"
        data-testid="notification-unread-badge"
        aria-hidden="true"
        class="absolute -right-0.5 -top-0.5 inline-flex h-4 min-w-[16px] items-center justify-center rounded-full bg-destructive px-1 text-[10px] font-bold text-destructive-foreground"
      >
        {{ store.unreadCount > 99 ? '99+' : store.unreadCount }}
      </span>
    </button>
    <!-- Unread-count live region (FAR-249 a11y): the visual badge above is
         aria-hidden so the button's accessible name stays "Notifications";
         this sr-only status region announces count *changes* to screen
         readers (WCAG 4.1.3). -->
    <output
      data-testid="notification-unread-live"
      :aria-label="$t('components.NotificationBell.notifications')"
      aria-live="polite"
      class="sr-only"
    >
      {{ $t('components.NotificationBell.unread_count_status', { count: store.unreadCount }) }}
    </output>
    <NotificationDropdown v-if="dropdownOpen" @close="closeDropdown" />
    <!-- FAR-250: classified-reconnect banner — shown when the SSE stream stopped on a 4xx -->
    <div
      v-if="eventBus.reconnectRequired"
      data-testid="sse-reconnect-banner"
      role="status"
      aria-live="polite"
      class="absolute left-full top-0 z-50 ml-2 w-56 rounded-md border border-border bg-popover p-3 text-xs shadow-md"
    >
      <p class="mb-2 text-foreground">{{ $t('components.NotificationBell.reconnect_banner') }}</p>
      <button
        type="button"
        data-testid="sse-reconnect-button"
        class="rounded bg-primary px-2 py-1 text-xs font-medium text-primary-foreground hover:opacity-90"
        @click="eventBus.reconnect()"
      >
        {{ $t('components.NotificationBell.reconnect_action') }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { api } from "../lib/api/client";
import { throwOnError } from "../lib/api/formatError";
import { eventBus } from "../composables/useEventStream";
import { useNotificationsStore } from "../stores/notifications";
import type { EventBusEvent } from "../types/events";
import NotificationDropdown from "./NotificationDropdown.vue";
import { Bell } from "@lucide/vue";

const store = useNotificationsStore();
const dropdownOpen = ref(false);
const rootRef = ref<HTMLElement | null>(null);
const bellButtonRef = ref<HTMLButtonElement | null>(null);
/** Categories this user opted out of (FAR-247 read-time suppression). */
const optOutCategories = ref<Set<string>>(new Set());

let unsubStream: (() => void) | null = null;
let unsubBackfill: (() => void) | null = null;

/** Fail-open: on any error the set stays empty (the server still filters reads). */
async function loadPreferences(): Promise<void> {
  try {
    const data = throwOnError(
      await api.GET("/api/v1/notifications/in-app/preferences"),
    ) as { notification_opt_outs?: Record<string, boolean> };
    const optedOut = Object.entries(data.notification_opt_outs ?? {})
      .filter(([, enabled]) => enabled)
      .map(([category]) => category);
    optOutCategories.value = new Set(optedOut);
  } catch {
    optOutCategories.value = new Set();
  }
}

function onNotificationEvent(event: EventBusEvent): void {
  // Read-time suppression (client side): an event for an opted-out category
  // changes nothing visible, so skip the refetch. The SSE payload carries
  // only {notification_id, category, created_at} — never content.
  if (event.category && optOutCategories.value.has(event.category)) return;
  void store.refreshUnreadCount();
}

function toggleDropdown(): void {
  dropdownOpen.value = !dropdownOpen.value;
  if (dropdownOpen.value) void store.fetchRecent();
}

function closeDropdown(): void {
  if (!dropdownOpen.value) return;
  dropdownOpen.value = false;
  // Return focus to the trigger so keyboard users are not stranded (the
  // dropdown is a non-modal popover; focus follows the control that closes it).
  bellButtonRef.value?.focus();
}

function onClickOutside(event: MouseEvent): void {
  if (rootRef.value && !rootRef.value.contains(event.target as Node)) {
    closeDropdown();
  }
}

onMounted(async () => {
  // Prefs BEFORE the first SSE event: the stream connects on the first
  // eventBus.subscribe below, so suppression is active from event one.
  await loadPreferences();
  await store.refreshUnreadCount();
  unsubBackfill = eventBus.onReconnect(() => {
    void store.refreshUnreadCount();
  });
  unsubStream = eventBus.subscribe("notification", onNotificationEvent);
  document.addEventListener("click", onClickOutside);
});

onUnmounted(() => {
  unsubStream?.();
  unsubStream = null;
  unsubBackfill?.();
  unsubBackfill = null;
  document.removeEventListener("click", onClickOutside);
});
</script>
