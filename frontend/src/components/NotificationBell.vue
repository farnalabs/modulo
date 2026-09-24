<template>
  <div class="relative inline-flex">
    <router-link
      to="/notifications"
      class="relative inline-flex items-center justify-center rounded-md p-2 text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
      :aria-label="$t('components.NotificationBell.notifications')"
    >
      <Bell class="h-[18px] w-[18px]" aria-hidden="true" />
      <span
        v-if="unreadCount > 0"
        data-testid="notification-unread-badge"
        class="absolute -right-0.5 -top-0.5 inline-flex h-4 min-w-[16px] items-center justify-center rounded-full bg-destructive px-1 text-[10px] font-bold text-destructive-foreground"
      >
        {{ unreadCount > 99 ? '99+' : unreadCount }}
      </span>
    </router-link>
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
import { ref, onMounted, onUnmounted } from "vue";
import { fetchUnreadCount } from "../lib/api/notifications";
import { api } from "../lib/api/client";
import { throwOnError } from "../lib/api/formatError";
import { eventBus } from "../composables/useEventStream";
import type { EventBusEvent } from "../types/events";
import { Bell } from "@lucide/vue";

const unreadCount = ref(0);
/** Categories this user opted out of (FAR-247 read-time suppression). */
const optOutCategories = ref<Set<string>>(new Set());

let unsubStream: (() => void) | null = null;
let unsubBackfill: (() => void) | null = null;

async function refreshUnreadCount(): Promise<void> {
  try {
    unreadCount.value = await fetchUnreadCount();
  } catch {
    unreadCount.value = 0;
  }
}

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
  void refreshUnreadCount();
}

onMounted(async () => {
  // Prefs BEFORE the first SSE event: the stream connects on the first
  // eventBus.subscribe below, so suppression is active from event one.
  await loadPreferences();
  await refreshUnreadCount();
  unsubBackfill = eventBus.onReconnect(() => {
    void refreshUnreadCount();
  });
  unsubStream = eventBus.subscribe("notification", onNotificationEvent);
});

onUnmounted(() => {
  unsubStream?.();
  unsubStream = null;
  unsubBackfill?.();
  unsubBackfill = null;
});
</script>
