<template>
  <div
    id="notification-dropdown"
    ref="dropdownRef"
    data-testid="notification-dropdown"
    role="dialog"
    :aria-label="$t('components.NotificationDropdown.title')"
    class="absolute right-0 top-full z-50 mt-1 w-80 rounded-md border border-border bg-popover text-popover-foreground shadow-md"
    @keydown.esc.stop.prevent="$emit('close')"
  >
    <div class="flex items-center justify-between border-b border-border px-3 py-2">
      <h2 class="text-sm font-semibold text-foreground">
        {{ $t("components.NotificationDropdown.title") }}
      </h2>
      <button
        type="button"
        data-testid="notification-mark-all-read"
        class="rounded px-2 py-1 text-xs font-medium text-primary hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50"
        :disabled="store.unreadCount === 0 || markingAll"
        @click="onMarkAllRead"
      >
        {{ markingAll ? $t("components.NotificationDropdown.marking_all_read") : $t("components.NotificationDropdown.mark_all_read") }}
      </button>
    </div>

    <p
      v-if="store.markAllReadError"
      data-testid="notification-mark-all-read-error"
      role="alert"
      class="border-b border-border px-3 py-2 text-xs text-destructive"
    >
      {{ $t("components.NotificationDropdown.failed_to_mark_all_read") }}
    </p>
    <p
      v-else-if="markedAllRead"
      data-testid="notification-mark-all-read-success"
      role="status"
      aria-live="polite"
      class="border-b border-border px-3 py-2 text-xs text-muted-foreground"
    >
      {{ $t("components.NotificationDropdown.marked_all_read_status") }}
    </p>

    <LoadingSpinner v-if="store.loadingRecent" />
    <!-- Fetch failed AND there is nothing previously loaded to fall back to:
         an error state, never the "No notifications" empty state, which would
         falsely claim the feed is clear (FAR-249 QA). When earlier items ARE
         kept (fail-open) the list below stays visible instead. -->
    <div
      v-else-if="store.recentNotifications.length === 0 && store.fetchError"
      data-testid="notification-dropdown-error"
      role="alert"
      class="px-3 py-4 text-center text-sm text-destructive"
    >
      <p>{{ $t("components.NotificationDropdown.failed_to_load") }}</p>
      <button
        type="button"
        data-testid="notification-dropdown-retry"
        class="mt-2 rounded px-2 py-1 text-xs font-medium text-primary hover:bg-muted"
        @click="store.fetchRecent()"
      >
        {{ $t("components.NotificationDropdown.retry") }}
      </button>
    </div>
    <p
      v-else-if="store.recentNotifications.length === 0"
      data-testid="notification-dropdown-empty"
      class="px-3 py-4 text-center text-sm text-muted-foreground"
    >
      {{ $t("components.NotificationDropdown.no_notifications") }}
    </p>
    <ul v-else class="max-h-80 overflow-y-auto divide-y divide-border">
      <li v-for="n in store.recentNotifications" :key="n.id">
        <component
          :is="n.action_url ? 'router-link' : 'div'"
          :to="n.action_url || undefined"
          class="block px-3 py-2 transition-colors hover:bg-muted/50"
          @click="$emit('close')"
        >
          <div class="flex items-start gap-2">
            <span
              class="mt-1.5 h-2 w-2 shrink-0 rounded-full"
              :class="levelDotClass(n.level)"
              aria-hidden="true"
            />
            <div class="min-w-0">
              <p class="truncate text-sm font-medium text-foreground">{{ n.title }}</p>
              <p class="line-clamp-2 text-xs text-muted-foreground">{{ n.body }}</p>
              <p class="mt-0.5 text-[11px] text-muted-foreground">
                {{ formatRelativeTime(n.created_at) }}
              </p>
            </div>
          </div>
        </component>
      </li>
    </ul>

    <div class="border-t border-border px-3 py-2 text-center">
      <router-link
        to="/notifications"
        data-testid="notification-dropdown-view-all"
        class="text-xs font-medium text-primary hover:underline"
        @click="$emit('close')"
      >
        {{ $t("components.NotificationDropdown.view_all") }}
      </router-link>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useNotificationsStore } from "../stores/notifications";
import { formatRelativeTime } from "../lib/formatDate";
import LoadingSpinner from "./shared/LoadingSpinner.vue";

defineEmits<{ close: [] }>();

const store = useNotificationsStore();
const dropdownRef = ref<HTMLElement | null>(null);
const markingAll = ref(false);
const markedAllRead = ref(false);

onMounted(() => {
  // The bell fetches on open; a direct mount (tests, future embedders) still
  // gets a list without a second click.
  if (!store.loadingRecent && store.recentNotifications.length === 0) {
    void store.fetchRecent();
  }
  dropdownRef.value
    ?.querySelector<HTMLElement>('button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])')
    ?.focus();
});

async function onMarkAllRead(): Promise<void> {
  markingAll.value = true;
  markedAllRead.value = false;
  try {
    await store.markAllRead();
    markedAllRead.value = true;
  } catch {
    // store.markAllReadError already holds the user-facing message; the
    // alert region above renders it. Focus stays on the button so the user
    // can retry.
  } finally {
    markingAll.value = false;
  }
}

/** Level severity dot — matches the dashboard panel's colour language. */
function levelDotClass(level: string): string {
  switch (level) {
    case "error":
      return "bg-destructive";
    case "warning":
      return "bg-amber-500";
    case "info":
      return "bg-blue-500";
    default:
      return "bg-muted-foreground";
  }
}
</script>
