<template>
  <div class="dashboard-notifications rounded-lg border bg-background">
    <button
      type="button"
      data-testid="notifications-panel-toggle"
      class="flex w-full items-center justify-between px-4 py-3 text-sm font-medium text-foreground hover:bg-muted/50 transition-colors"
      :aria-expanded="!collapsed"
      aria-controls="notifications-panel-content"
      @click="toggleCollapsed"
    >
      <div class="flex items-center gap-2">
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="text-muted-foreground" aria-hidden="true"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
        <span>{{ $t('components.DashboardNotificationsPanel.notifications') }}</span>
        <span v-if="unreadCount > 0" class="inline-flex h-5 min-w-[20px] items-center justify-center rounded-full bg-destructive px-1.5 text-[11px] font-bold text-destructive-foreground" role="status" :aria-label="`${unreadCount} unread notifications`">{{ unreadCount }}</span>
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
        aria-hidden="true"
      >
        <polyline points="6 15 12 9 18 15" />
      </svg>
    </button>
    <div v-if="!collapsed" id="notifications-panel-content" class="border-t px-4 py-3">
      <LoadingSpinner v-if="loading" />
      <div v-else-if="error" class="text-sm text-destructive" role="alert">{{ error }}</div>
      <div v-else-if="notifications.length === 0" class="text-center text-sm text-muted-foreground py-4">
        {{ $t('components.DashboardNotificationsPanel.no_notifications') }}
      </div>
      <template v-else>
        <div v-if="reviewLaterError" class="px-4 py-1 text-xs text-destructive" role="alert">{{ reviewLaterError }}</div>
        <div class="space-y-2">
          <NotificationCard
            v-for="n in notifications"
            :key="n.id"
            :notification="n"
            @dismissed="onDismissed"
            @review-later="onReviewLater"
          />
        </div>
        <!-- Paging controls -->
        <div v-if="totalPages > 1" class="flex items-center justify-between pt-3">
          <p class="text-xs text-muted-foreground">
            {{ $t('components.DashboardNotificationsPanel.page_x_of_y', { current: page, total: totalPages }) }}
          </p>
          <div class="flex items-center gap-1">
            <button
              type="button"
              data-testid="panel-prev-page"
              class="rounded border px-2 py-1 text-xs font-medium text-muted-foreground hover:bg-muted transition-colors disabled:opacity-50"
              :disabled="page <= 1"
              :aria-label="$t('components.DashboardNotificationsPanel.previous_page')"
              @click="prevPage"
            >
              &lsaquo;
            </button>
            <button
              type="button"
              data-testid="panel-next-page"
              class="rounded border px-2 py-1 text-xs font-medium text-muted-foreground hover:bg-muted transition-colors disabled:opacity-50"
              :disabled="page >= totalPages"
              :aria-label="$t('components.DashboardNotificationsPanel.next_page')"
              @click="nextPage"
            >
              &rsaquo;
            </button>
          </div>
        </div>
      </template>
      <div v-if="!loading && !error" class="mt-3 text-center">
        <router-link
          to="/notifications"
          class="text-xs font-medium text-primary hover:underline"
        >
          {{ $t('components.DashboardNotificationsPanel.view_all') }} &rarr;
        </router-link>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from "vue";
import { useStorage } from '@vueuse/core';
import type { NotificationResponse } from "../lib/api/notifications";
import { fetchNotifications, reviewLater } from "../lib/api/notifications";
import { registerHandler } from "../stores/syncRegistry";
import NotificationCard from "./NotificationCard.vue";
import { formatApiError } from "../lib/api/formatError";
import LoadingSpinner from "./shared/LoadingSpinner.vue";

const PAGE_SIZE = 10;
const collapsed = useStorage('notif-panel-collapsed', true);
const notifications = ref<NotificationResponse[]>([]);
const loading = ref(false);
const error = ref<string | null>(null);
const reviewLaterError = ref("");
const unreadCount = ref(0);
const page = ref(1);
const total = ref(0);

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)));

function toggleCollapsed() {
  collapsed.value = !collapsed.value;
}

function onDismissed(id: string) {
  notifications.value = notifications.value.filter((n) => n.id !== id);
  total.value = Math.max(0, total.value - 1);
  if (unreadCount.value > 0) unreadCount.value--;
  // If page is now empty and not the first page, go back one
  if (notifications.value.length === 0 && page.value > 1) {
    page.value--;
    void loadPage();
  }
}

async function onReviewLater(id: string) {
  reviewLaterError.value = "";
  try {
    await reviewLater(id);
    notifications.value = notifications.value.filter((n) => n.id !== id);
    total.value = Math.max(0, total.value - 1);
    if (unreadCount.value > 0) unreadCount.value--;
    if (notifications.value.length === 0 && page.value > 1) {
      page.value--;
      void loadPage();
    }
  } catch (e: unknown) {
    reviewLaterError.value = e instanceof Error ? e.message : "Failed to dismiss notification";
  }
}

function prevPage() {
  if (page.value > 1) {
    page.value--;
    void loadPage();
  }
}

function nextPage() {
  if (page.value < totalPages.value) {
    page.value++;
    void loadPage();
  }
}

let unsubHandler: (() => void) | null = null;

onMounted(async () => {
  await loadPage();
  unsubHandler = registerHandler("notification", () => {
    void loadPage();
  });
});

onUnmounted(() => {
  if (unsubHandler) unsubHandler();
});

async function loadPage() {
  loading.value = true;
  error.value = null;
  try {
    const result = await fetchNotifications({
      page: page.value,
      page_size: PAGE_SIZE,
      status: "active",
    });
    notifications.value = result.items;
    total.value = result.total;
    // unreadCount: count active notifications not yet loaded (rough approximation)
    unreadCount.value = result.total;
  } catch (e: unknown) {
    error.value = formatApiError(e);
  } finally {
    loading.value = false;
  }
}
</script>
