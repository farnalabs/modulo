<template>
  <div class="mx-auto max-w-6xl space-y-6 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">Notifications</h1>
      <p class="mt-1 text-muted-foreground">View and manage your notifications</p>
    </header>

    <!-- Filters -->
    <div class="card p-4">
      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <select v-model="filterLevel" class="rounded-md border bg-background px-3 py-2 text-sm">
          <option value="">All Levels</option>
          <option value="error">Error</option>
          <option value="warning">Warning</option>
          <option value="info">Info</option>
          <option value="debug">Debug</option>
        </select>
        <select v-model="filterScope" class="rounded-md border bg-background px-3 py-2 text-sm">
          <option value="">All Scopes</option>
          <option value="user">Personal</option>
          <option value="org">Org-wide</option>
          <option value="admin">Admin</option>
        </select>
        <select v-model="filterStatus" class="rounded-md border bg-background px-3 py-2 text-sm">
          <option value="">All Status</option>
          <option value="active">Active</option>
          <option value="dismissed_self">Dismissed (self)</option>
          <option value="dismissed_scope">Dismissed (scope)</option>
        </select>
        <div class="flex items-end gap-2">
          <button
            type="button"
            class="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 transition-colors"
            @click="applyFilters"
          >
            Apply Filters
          </button>
          <button
            type="button"
            class="rounded-md border px-4 py-2 text-sm font-medium text-muted-foreground hover:bg-muted transition-colors"
            @click="resetFilters"
          >
            Reset
          </button>
        </div>
      </div>
    </div>

    <!-- States -->
    <LoadingSpinner v-if="loading" />
    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadNotifications" />
    <EmptyState v-else-if="notifications.length === 0" title="No notifications" description="No notifications matching your filters.">
      <button type="button" class="text-sm text-primary hover:underline" @click="resetFilters">Clear filters</button>
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
import { ref, onMounted } from "vue";
import type { NotificationResponse } from "../lib/api/notifications";
import { fetchNotifications, reviewLater } from "../lib/api/notifications";
import NotificationCard from "../components/NotificationCard.vue";
import LoadingSpinner from "../components/shared/LoadingSpinner.vue";
import ErrorAlert from "../components/shared/ErrorAlert.vue";
import EmptyState from "../components/shared/EmptyState.vue";

const notifications = ref<NotificationResponse[]>([]);
const loading = ref(false);
const error = ref<string | null>(null);
const total = ref(0);
const page = ref(1);
const pageSize = ref(20);

const filterLevel = ref("");
const filterScope = ref("");
const filterStatus = ref("");

async function loadNotifications(p?: number) {
  loading.value = true;
  error.value = null;
  try {
    const result = await fetchNotifications({
      page: p ?? page.value,
      page_size: pageSize.value,
      level: filterLevel.value || undefined,
      scope: filterScope.value || undefined,
      status: filterStatus.value || undefined,
    });
    notifications.value = result.items;
    total.value = result.total;
    page.value = result.page;
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    loading.value = false;
  }
}

function applyFilters() {
  page.value = 1;
  void loadNotifications(1);
}

function resetFilters() {
  filterLevel.value = "";
  filterScope.value = "";
  filterStatus.value = "";
  page.value = 1;
  void loadNotifications(1);
}

function prevPage() {
  if (page.value > 1) void loadNotifications(page.value - 1);
}

function nextPage() {
  void loadNotifications(page.value + 1);
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
  } catch {
    // Silent
  }
}

onMounted(() => void loadNotifications());
</script>
