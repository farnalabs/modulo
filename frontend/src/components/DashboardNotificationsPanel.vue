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
        <output v-if="unreadCount > 0" class="inline-flex h-5 min-w-[20px] items-center justify-center rounded-full bg-destructive-fill px-1.5 text-[11px] font-bold text-destructive-foreground" :aria-label="`${unreadCount} unread notifications`">{{ unreadCount }}</output>
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
      <!-- FAR-1926: "Dismiss all" clears the whole live list (for this user
           only), so it sits in the expanded content header, above the list it
           applies to, with visible progress and result. -->
      <div class="flex flex-wrap items-center justify-between gap-x-2 gap-y-1 pb-2">
        <button
          type="button"
          data-testid="panel-dismiss-all"
          class="rounded px-2 py-1 text-xs font-medium text-primary-text hover:bg-muted transition-colors disabled:cursor-not-allowed disabled:opacity-50"
          :disabled="dismissingAll || total === 0"
          @click="onDismissAll"
        >
          {{ dismissingAll ? $t('components.DashboardNotificationsPanel.dismissing_all') : $t('components.DashboardNotificationsPanel.dismiss_all') }}
        </button>
        <!-- Failure, success and no-op are MUTUALLY EXCLUSIVE outcomes of
             this panel's own attempt; dismissAllError is a local ref (never
             shared store state, QA FAR-1926) so a failure here cannot leak
             onto the bell dropdown or the notifications page. -->
        <p
          v-if="dismissAllError"
          data-testid="panel-dismiss-all-error"
          role="alert"
          class="text-xs text-destructive"
        >
          {{ dismissAllError }}
        </p>
        <output
          v-else-if="dismissedAll"
          data-testid="panel-dismiss-all-success"
          :aria-label="$t('components.DashboardNotificationsPanel.dismissed_all_status')"
          aria-live="polite"
          class="text-xs text-muted-foreground"
        >
          {{ $t('components.DashboardNotificationsPanel.dismissed_all_status') }}
        </output>
        <!-- The server marked nothing (no-op re-run): neutral copy, never the
             dismissed-success claim. -->
        <output
          v-else-if="nothingToDismiss"
          data-testid="panel-dismiss-all-nothing"
          :aria-label="$t('components.DashboardNotificationsPanel.nothing_to_dismiss')"
          aria-live="polite"
          class="text-xs text-muted-foreground"
        >
          {{ $t('components.DashboardNotificationsPanel.nothing_to_dismiss') }}
        </output>
      </div>
      <LoadingSpinner v-if="loading" />
      <div v-else-if="error" class="text-sm text-destructive" role="alert">{{ error }}</div>
      <div v-else-if="notifications.length === 0" class="text-center text-sm text-muted-foreground py-4">
        {{ $t('components.DashboardNotificationsPanel.no_notifications') }}
      </div>
      <template v-else>
        <div v-if="reviewLaterError" class="px-4 py-1 text-xs text-destructive" role="alert">{{ reviewLaterError }}</div>
        <div class="max-h-[400px] overflow-y-auto space-y-2">
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
          class="text-xs font-medium text-primary-text hover:underline"
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
import { fetchNotifications, reviewLater, fetchUnreadCount } from "../lib/api/notifications";
import { registerHandler } from "../stores/syncRegistry";
import { useNotificationsStore } from "../stores/notifications";
import { eventBus } from "../composables/useEventStream";
import NotificationCard from "./NotificationCard.vue";
import { formatApiError } from "../lib/api/formatError";
import LoadingSpinner from "./shared/LoadingSpinner.vue";

const PAGE_SIZE = 10;
const collapsed = useStorage('notif-panel-collapsed', true);
const store = useNotificationsStore();
const notifications = ref<NotificationResponse[]>([]);
const loading = ref(false);
const error = ref<string | null>(null);
const reviewLaterError = ref("");
const unreadCount = ref(0);
const page = ref(1);
const total = ref(0);
const dismissingAll = ref(false);
const dismissedAll = ref(false);
/** No-op outcome (server marked 0 rows): mutually exclusive with success. */
const nothingToDismiss = ref(false);
/**
 * THIS panel's dismiss-all error (QA FAR-1926) — a local ref, never shared
 * store state, so a failure here cannot leak onto the bell dropdown or the
 * notifications page. Cleared at the start of each attempt and whenever
 * loadPage() below succeeds.
 */
const dismissAllError = ref<string | null>(null);

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)));

function toggleCollapsed() {
  collapsed.value = !collapsed.value;
}

// The unread badge must reflect the severity-filtered unread count
// (warning+), not the raw count of all active notifications. Use the
// existing dedicated endpoint rather than the paginated list total.
async function refreshUnreadCount() {
  try {
    unreadCount.value = await fetchUnreadCount();
  } catch {
    unreadCount.value = 0;
  }
}

async function onDismissed(id: string) {
  notifications.value = notifications.value.filter((n) => n.id !== id);
  total.value = Math.max(0, total.value - 1);
  // If page is now empty and not the first page, go back one
  if (notifications.value.length === 0 && page.value > 1) {
    page.value--;
    await loadPage();
  } else {
    await refreshUnreadCount();
  }
}

async function onReviewLater(id: string) {
  reviewLaterError.value = "";
  try {
    await reviewLater(id);
    notifications.value = notifications.value.filter((n) => n.id !== id);
    total.value = Math.max(0, total.value - 1);
    if (notifications.value.length === 0 && page.value > 1) {
      page.value--;
      await loadPage();
    } else {
      await refreshUnreadCount();
    }
  } catch (e: unknown) {
    reviewLaterError.value = e instanceof Error ? e.message : "Failed to dismiss notification";
  }
}

async function onDismissAll(): Promise<void> {
  dismissingAll.value = true;
  dismissedAll.value = false;
  nothingToDismiss.value = false;
  dismissAllError.value = null;
  try {
    // One shared action path (FAR-1926): the store action posts the
    // per-user mark-all-read and refreshes the bell's badge/list. The
    // returned count keeps the copy truthful — a no-op (marked === 0)
    // reports "Nothing to dismiss", never the dismissed-success message.
    const marked = await store.dismissAll();
    dismissedAll.value = marked > 0;
    nothingToDismiss.value = marked === 0;
    // This panel keeps its OWN list and badge, so re-read both from the
    // server's post-state — never a locally-invented clear.
    await loadPage();
  } catch (e: unknown) {
    // The store throws an Error carrying the formatted API message; keep it
    // in THIS surface's local ref so no other surface can render it.
    // The list is deliberately left untouched: nothing was cleared.
    dismissAllError.value = e instanceof Error ? e.message : String(e);
  } finally {
    dismissingAll.value = false;
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
let unsubReconnect: (() => void) | null = null;

onMounted(async () => {
  await loadPage();
  unsubHandler = registerHandler("notification", () => {
    void loadPage();
  });
  // FAR-250: after a successful SSE reconnect, REST-backfill the list (the
  // stream has no replay buffer). Debounced centrally in useEventStream.
  unsubReconnect = eventBus.onReconnect(() => {
    void loadPage();
  });
});

onUnmounted(() => {
  if (unsubHandler) unsubHandler();
  if (unsubReconnect) unsubReconnect();
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
    // A successful list load retires any earlier dismiss-all error — the
    // fresh server state is the new truth (a failed load never reaches here,
    // so it cannot clear a genuine failure).
    dismissAllError.value = null;
    // Badge reflects the severity-filtered unread count, not the raw total.
    await refreshUnreadCount();
  } catch (e: unknown) {
    error.value = formatApiError(e);
  } finally {
    loading.value = false;
  }
}
</script>
