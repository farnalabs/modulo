<template>
  <div class="dashboard-notifications rounded-lg border bg-background">
    <button
      type="button"
      class="flex w-full items-center justify-between px-4 py-3 text-sm font-medium text-foreground hover:bg-muted/50 transition-colors"
      @click="toggleCollapsed"
    >
      <div class="flex items-center gap-2">
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="text-muted-foreground"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
        <span>Notifications</span>
        <span v-if="unreadCount > 0" class="inline-flex h-5 min-w-[20px] items-center justify-center rounded-full bg-destructive px-1.5 text-[11px] font-bold text-destructive-foreground">{{ unreadCount }}</span>
      </div>
      <svg
        xmlns="http://www.w3.org/2000/svg"
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        class="transition-transform duration-200"
        :class="{ 'rotate-180': !collapsed }"
      >
        <polyline points="6 15 12 9 18 15" />
      </svg>
    </button>
    <div v-if="!collapsed" class="border-t px-4 py-3">
      <LoadingSpinner v-if="loading" />
      <div v-else-if="error" class="text-sm text-destructive">{{ error }}</div>
      <div v-else-if="notifications.length === 0" class="text-center text-sm text-muted-foreground py-4">
        No notifications
      </div>
      <template v-else>
        <div class="space-y-2 max-h-[400px] overflow-y-auto">
          <NotificationCard
            v-for="n in notifications"
            :key="n.id"
            :notification="n"
            @dismissed="onDismissed"
            @review-later="onReviewLater"
          />
        </div>
        <div v-if="hasMore" class="mt-3 text-center">
          <router-link
            to="/notifications"
            class="text-xs font-medium text-primary hover:underline"
          >
            View all notifications →
          </router-link>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from "vue";
import type { NotificationResponse } from "../lib/api/notifications";
import { fetchDashboardNotifications, reviewLater, fetchUnreadCount } from "../lib/api/notifications";
import { registerHandler } from "../stores/syncRegistry";
import NotificationCard from "./NotificationCard.vue";
import LoadingSpinner from "./shared/LoadingSpinner.vue";

const collapsed = ref(localStorage.getItem("notif-panel-collapsed") !== "false");
const notifications = ref<NotificationResponse[]>([]);
const loading = ref(false);
const error = ref<string | null>(null);
const unreadCount = ref(0);
const hasMore = ref(false);

function toggleCollapsed() {
  collapsed.value = !collapsed.value;
  localStorage.setItem("notif-panel-collapsed", String(collapsed.value));
}

function onDismissed(id: string) {
  notifications.value = notifications.value.filter((n) => n.id !== id);
  if (unreadCount.value > 0) unreadCount.value--;
}

async function onReviewLater(id: string) {
  try {
    await reviewLater(id);
    notifications.value = notifications.value.filter((n) => n.id !== id);
    if (unreadCount.value > 0) unreadCount.value--;
  } catch {
    // Silent
  }
}

let unsubHandler: (() => void) | null = null;

onMounted(async () => {
  await loadDashboard();
  unsubHandler = registerHandler("notification", () => {
    void loadDashboard();
  });
});

onUnmounted(() => {
  if (unsubHandler) unsubHandler();
});

async function loadDashboard() {
  loading.value = true;
  error.value = null;
  try {
    const result = await fetchDashboardNotifications();
    notifications.value = result.notifications;
    unreadCount.value = result.total_unread;
    hasMore.value = result.notifications.length >= 5 || result.total_unread > result.notifications.length;
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    loading.value = false;
  }
}
</script>
