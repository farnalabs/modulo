<template>
  <div class="page-wide">
    <PageHeader title="Notifications" :subtitle="$t('views.NotificationsPage.view_and_manage_your_notifications')" />

    <!-- Filters -->
    <FilterBar
      :filters="[
        { key: 'level', label: $t('views.AdminErrorsView.all_levels'), options: [
          { value: 'error', label: 'Error' },
          { value: 'warning', label: 'Warning' },
          { value: 'info', label: 'Info' },
          { value: 'debug', label: 'Debug' },
        ]},
        { key: 'scope', label: $t('views.NotificationsPage.all_scopes'), options: [
          { value: 'user', label: 'Personal' },
          { value: 'org', label: $t('components.OwnershipPicker.orgwide') },
          { value: 'admin', label: 'Admin' },
        ]},
        { key: 'status', label: $t('views.NotificationsPage.all_status'), options: [
          { value: 'active', label: 'Active' },
          { value: 'dismissed_self', label: $t('views.NotificationsPage.dismissed_self') },
          { value: 'dismissed_scope', label: $t('views.NotificationsPage.dismissed_scope') },
        ]},
      ]"
      :filter-values="{ level: filterLevel, scope: filterScope, status: filterStatus }"
      @update:filter="handleFilterUpdate"
    >
      <template #after>
        <Button type="button" variant="default" @click="applyFilters">Apply Filters</Button>
        <button type="button" class="rounded-md border px-4 py-2 text-sm font-medium text-muted-foreground hover:bg-muted transition-colors" @click="resetFilters">Reset</button>
      </template>
    </FilterBar>

    <!-- States -->
    <LoadingSpinner v-if="loading" />
    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadNotifications" />
    <EmptyState v-else-if="notifications.length === 0" :title="$t('views.NotificationsPage.no_notifications')" description="No notifications matching your filters.">
      <button type="button" class="text-sm text-primary hover:underline" @click="resetFilters">{{ $t('views.NotificationsPage.clear_filters') }}</button>
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
          Showing {{ notifications.length }} of {{ total }} notifications
        </p>
        <div class="flex items-center gap-2">
          <button
            type="button"
            class="rounded-md border px-3 py-1.5 text-sm font-medium text-muted-foreground hover:bg-muted transition-colors"
            :disabled="page <= 1"
            @click="prevPage"
          >
            Previous
          </button>
          <button
            type="button"
            class="rounded-md border px-3 py-1.5 text-sm font-medium text-muted-foreground hover:bg-muted transition-colors"
            :disabled="page * pageSize >= total"
            @click="nextPage"
          >
            Next
          </button>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from "vue";
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
      new Promise<never>((_, reject) => setTimeout(() => reject(new Error('Notifications request timed out after 30s')), 30000)),
    ]);
    return { data: result, error: undefined };
  },
  { initialValue: { items: [] as NotificationResponse[], total: 0, page: 1, page_size: 20 } },
)

const notifications = ref<NotificationResponse[]>([])

watch(data, (d) => {
  if (d) {
    notifications.value = (d as any).items ?? []
    total.value = (d as any).total ?? 0
    page.value = (d as any).page ?? 1
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
    error.value = e instanceof Error ? e.message : "Failed to dismiss notification";
  }
}
</script>
