<template>
  <div class="page-wide">
    <PageHeader :title="$t('views.NotificationsPage.title')" :subtitle="$t('views.NotificationsPage.view_and_manage_your_notifications')" />

    <!-- Filters -->
    <FilterBar
      :filters="[
        { key: 'level', label: $t('views.NotificationsPage.level'), options: [
          { value: 'error', label: $t('views.NotificationsPage.level_error') },
          { value: 'warning', label: $t('views.NotificationsPage.level_warning') },
          { value: 'info', label: $t('views.NotificationsPage.level_info') },
          { value: 'debug', label: $t('views.NotificationsPage.level_debug') },
        ]},
        { key: 'scope', label: $t('views.NotificationsPage.scope'), options: [
          { value: 'user', label: $t('views.NotificationsPage.scope_personal') },
          { value: 'org', label: $t('components.OwnershipPicker.orgwide') },
          { value: 'admin', label: $t('views.NotificationsPage.scope_admin') },
        ]},
        { key: 'status', label: $t('views.NotificationsPage.status'), options: [
          { value: 'active', label: $t('common.active') },
          { value: 'dismissed_self', label: $t('views.NotificationsPage.dismissed_self') },
          { value: 'dismissed_scope', label: $t('views.NotificationsPage.dismissed_scope') },
        ]},
      ]"
      :filter-values="{ level: filterLevel, scope: filterScope, status: filterStatus }"
      @update:filter="handleFilterUpdate"
    >
      <template #after>
        <!-- FAR-1926: Dismiss all clears every live notification FOR THIS
             USER ONLY (the shared store action posts the per-user
             mark-all-read). Sits beside the filters, applies to the whole
             live list — not just the current page. -->
        <button
          type="button"
          data-testid="notifications-dismiss-all"
          class="rounded-md border px-4 py-2 text-sm font-medium text-primary-text hover:bg-muted transition-colors disabled:cursor-not-allowed disabled:opacity-50"
          :disabled="dismissingAll || loading || total === 0 || filterStatus !== 'active'"
          @click="onDismissAll"
        >
          {{ dismissingAll ? $t('views.NotificationsPage.dismissing_all') : $t('views.NotificationsPage.dismiss_all') }}
        </button>
        <button type="button" data-testid="notifications-reset-filters" class="rounded-md border px-4 py-2 text-sm font-medium text-muted-foreground hover:bg-muted transition-colors" @click="resetFilters">{{ $t('common.reset') }}</button>
      </template>
    </FilterBar>

    <!-- Failure, success and no-op are three MUTUALLY EXCLUSIVE outcomes of
         this page's OWN dismiss-all attempt — the error ref is local (never
         shared store state, QA FAR-1926) and only ever set here. -->
    <p
      v-if="dismissAllError"
      data-testid="notifications-dismiss-all-error"
      role="alert"
      class="text-sm text-destructive"
    >
      {{ dismissAllError }}
    </p>
    <output
      v-else-if="dismissedAll"
      data-testid="notifications-dismiss-all-success"
      :aria-label="$t('views.NotificationsPage.dismissed_all_status')"
      aria-live="polite"
      class="block text-sm text-muted-foreground"
    >
      {{ $t('views.NotificationsPage.dismissed_all_status') }}
    </output>
    <!-- The server marked nothing (no-op re-run): say so neutrally rather
         than claiming a clear that did not happen. -->
    <output
      v-else-if="nothingToDismiss"
      data-testid="notifications-dismiss-all-nothing"
      :aria-label="$t('views.NotificationsPage.nothing_to_dismiss')"
      aria-live="polite"
      class="block text-sm text-muted-foreground"
    >
      {{ $t('views.NotificationsPage.nothing_to_dismiss') }}
    </output>

    <!-- States -->
    <div v-if="loading" data-testid="notifications-loading">
      <output class="sr-only" aria-live="polite">{{ $t('views.NotificationsPage.loading_notifications') }}</output>
      <div class="space-y-2" aria-busy="true">
        <div
          v-for="n in 3"
          :key="`notifications-skeleton-${n}`"
          class="rounded-lg border bg-card p-4"
        >
          <div class="space-y-2">
            <SkeletonBlock height-class="h-4 w-48" />
            <SkeletonBlock height-class="h-3 w-72" />
            <SkeletonBlock height-class="h-3 w-40" />
          </div>
        </div>
      </div>
    </div>
    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadNotifications" />
    <EmptyState v-else-if="notifications.length === 0" :title="$t('views.NotificationsPage.no_notifications')" :description="$t('views.NotificationsPage.no_notifications_matching_filters')">
      <button type="button" data-testid="notifications-clear-filters" class="text-sm text-primary-text hover:underline" @click="resetFilters">{{ $t('views.NotificationsPage.clear_filters') }}</button>
    </EmptyState>
    <template v-else>
      <div class="space-y-2">
        <NotificationCard
          v-for="n in notifications"
          :key="n.id"
          :notification="n"
          :show-body="true"
          @dismissed="onDismissed"
          @review-later="onReviewLater"
        />
      </div>
      <!-- Pagination -->
      <div class="flex items-center justify-between pt-4">
        <p class="text-sm text-muted-foreground">
          {{ $t('views.NotificationsPage.showing_x_of_y_notifications', { count: notifications.length, total }) }}
        </p>
        <div class="flex items-center gap-2">
          <button
            type="button"
            data-testid="notifications-prev-page"
            class="rounded-md border px-3 py-1.5 text-sm font-medium text-muted-foreground hover:bg-muted transition-colors"
            :disabled="page <= 1"
            @click="prevPage"
          >
            {{ $t('common.previous') }}
          </button>
          <button
            type="button"
            data-testid="notifications-next-page"
            class="rounded-md border px-3 py-1.5 text-sm font-medium text-muted-foreground hover:bg-muted transition-colors"
            :disabled="page * pageSize >= total"
            @click="nextPage"
          >
            {{ $t('common.next') }}
          </button>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import type { NotificationResponse } from "../lib/api/notifications";
import { useDataFetch } from "../composables/useDataFetch";
import { fetchNotifications, reviewLater } from "../lib/api/notifications";
import { useNotificationsStore } from "../stores/notifications";
import NotificationCard from "../components/NotificationCard.vue";
import PageHeader from '../components/shared/PageHeader.vue'
import FilterBar from '../components/shared/FilterBar.vue'
import SkeletonBlock from "../components/shared/SkeletonBlock.vue";
import ErrorAlert from "../components/shared/ErrorAlert.vue";
import EmptyState from "../components/shared/EmptyState.vue";

const { t } = useI18n();

const store = useNotificationsStore();
const total = ref(0);
const page = ref(1);
const pageSize = ref(20);
const dismissingAll = ref(false);
const dismissedAll = ref(false);
/**
 * Neutral outcome for a no-op re-run (the server marked 0 rows): mutually
 * exclusive with `dismissedAll` so a zero never claims a clear.
 */
const nothingToDismiss = ref(false);
/**
 * THIS surface's dismiss-all error (QA FAR-1926). Local by design — the
 * shared store holds no error state, so a failure here can never render as a
 * stale alert on the bell dropdown or the dashboard panel. Cleared at the
 * start of each attempt and whenever a list load succeeds below.
 */
const dismissAllError = ref<string | null>(null);

const filterLevel = ref("");
const filterScope = ref("");
const filterStatus = ref("active");

function handleFilterUpdate(key: string, value: string) {
  if (key === 'level') filterLevel.value = value
  else if (key === 'scope') filterScope.value = value
  else if (key === 'status') filterStatus.value = value
}

const { loading, error, data, load: loadNotifications } = useDataFetch(
  async (p?: number) => {
    const result = await Promise.race([
      fetchNotifications({
        page: p ?? page.value,
        page_size: pageSize.value,
        level: filterLevel.value || undefined,
        scope: filterScope.value || undefined,
        status: filterStatus.value || undefined,
      }),
      new Promise<never>((_, reject) => setTimeout(() => reject(new Error(t('views.NotificationsPage.request_timed_out'))), 30000)),
    ]);
    return { data: result, error: undefined };
  },
  { initialValue: { items: [] as NotificationResponse[], total: 0, page: 1, page_size: 20 } },
)

const notifications = ref<NotificationResponse[]>([])

watch(data, (d) => {
  if (d) {
    notifications.value = d.items
    total.value = d.total
    page.value = d.page
    // A successful list load retires any earlier dismiss-all error — the
    // fresh server state is the new truth (failures leave `data` untouched,
    // so a failed load never clears it).
    dismissAllError.value = null
  }
}, { immediate: true })

// Auto-apply filters on any change
watch([filterLevel, filterScope, filterStatus], () => {
  page.value = 1;
  void loadNotifications();
});

function resetFilters() {
  filterLevel.value = "";
  filterScope.value = "";
  filterStatus.value = "active";
  page.value = 1;
  void loadNotifications();
}

function prevPage() {
  if (page.value > 1) {
    page.value -= 1
    void loadNotifications()
  }
}

function nextPage() {
  page.value += 1
  void loadNotifications()
}

function onDismissed(id: string) {
  notifications.value = notifications.value.filter((n) => n.id !== id);
  total.value = Math.max(0, total.value - 1);
}

async function onDismissAll(): Promise<void> {
  // Gated to the live view (QA FAR-1926): the bulk action only clears LIVE
  // notifications, so a dismissed-status list is never a valid target. The
  // button's :disabled carries the same condition; this guard covers any
  // programmatic invocation so the API is never posted from a dismissed view.
  if (filterStatus.value !== "active") return;
  dismissingAll.value = true;
  dismissedAll.value = false;
  nothingToDismiss.value = false;
  dismissAllError.value = null;
  try {
    // One shared action path (FAR-1926): same store action as the bell
    // dropdown and the dashboard panel — per-user scope only.
    const marked = await store.dismissAll();
    // Truthful copy: the SERVER's marked count picks the outcome — a no-op
    // reports "Nothing to dismiss", never the dismissed-success message.
    nothingToDismiss.value = marked === 0;
    dismissedAll.value = marked > 0;
    // Re-read THIS page's filtered list from the server's post-state so the
    // active view empties for real, never because of a local mutation.
    await loadNotifications();
  } catch (e: unknown) {
    // The store throws an Error carrying the formatted API message; keep it
    // in THIS surface's local ref so no other surface can render it.
    // The list is deliberately left untouched: nothing was cleared.
    dismissAllError.value = e instanceof Error ? e.message : String(e);
  } finally {
    dismissingAll.value = false;
  }
}

async function onReviewLater(id: string) {
  try {
    await reviewLater(id);
    notifications.value = notifications.value.filter((n) => n.id !== id);
    total.value = Math.max(0, total.value - 1);
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : t('views.NotificationsPage.failed_to_dismiss_notification');
  }
}
</script>
