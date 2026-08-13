<template>
  <div class="page-wide">
    <PageHeader :title="$t('views.NotificationsPage.title')" :subtitle="$t('views.NotificationsPage.view_and_manage_your_notifications')" />

    <!-- Filters -->
    <FilterBar
      :filters="[
        { key: 'level', label: $t('views.AdminErrorsView.all_levels'), options: [
          { value: 'error', label: $t('views.NotificationsPage.level_error') },
          { value: 'warning', label: $t('views.NotificationsPage.level_warning') },
          { value: 'info', label: $t('views.NotificationsPage.level_info') },
          { value: 'debug', label: $t('views.NotificationsPage.level_debug') },
        ]},
        { key: 'scope', label: $t('views.NotificationsPage.all_scopes'), options: [
          { value: 'user', label: $t('views.NotificationsPage.scope_personal') },
          { value: 'org', label: $t('components.OwnershipPicker.orgwide') },
          { value: 'admin', label: $t('views.NotificationsPage.scope_admin') },
        ]},
        { key: 'status', label: $t('views.NotificationsPage.all_status'), options: [
          { value: 'active', label: $t('common.active') },
          { value: 'dismissed_self', label: $t('views.NotificationsPage.dismissed_self') },
          { value: 'dismissed_scope', label: $t('views.NotificationsPage.dismissed_scope') },
        ]},
      ]"
      :filter-values="{ level: filterLevel, scope: filterScope, status: filterStatus }"
      @update:filter="handleFilterUpdate"
    >
      <template #after>
        <Button type="button" variant="default" data-testid="notifications-apply-filters" @click="applyFilters">{{ $t('views.NotificationsPage.apply_filters') }}</Button>
        <button type="button" data-testid="notifications-reset-filters" class="rounded-md border px-4 py-2 text-sm font-medium text-muted-foreground hover:bg-muted transition-colors" @click="resetFilters">{{ $t('common.reset') }}</button>
      </template>
    </FilterBar>

    <!-- States -->
    <LoadingSpinner v-if="loading" />
    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadNotifications" />
    <EmptyState v-else-if="notifications.length === 0" :title="$t('views.NotificationsPage.no_notifications')" :description="$t('views.NotificationsPage.no_notifications_matching_filters')">
      <button type="button" data-testid="notifications-clear-filters" class="text-sm text-primary hover:underline" @click="resetFilters">{{ $t('views.NotificationsPage.clear_filters') }}</button>
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
import NotificationCard from "../components/NotificationCard.vue";
import PageHeader from '../components/shared/PageHeader.vue'
import FilterBar from '../components/shared/FilterBar.vue'
import LoadingSpinner from "../components/shared/LoadingSpinner.vue";
import ErrorAlert from "../components/shared/ErrorAlert.vue";
import { Button } from "@/components/ui/button";
import EmptyState from "../components/shared/EmptyState.vue";

const { t } = useI18n();

const total = ref(0);
const page = ref(1);
const pageSize = ref(20);

const filterLevel = ref("");
const filterScope = ref("");
const filterStatus = ref("");

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
  }
}, { immediate: true })

function applyFilters() {
  page.value = 1;
  void loadNotifications();
}

function resetFilters() {
  filterLevel.value = "";
  filterScope.value = "";
  filterStatus.value = "";
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
