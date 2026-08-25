<template>
  <div
    class="notification-card group relative flex items-start gap-3 rounded-lg border p-3 transition-colors hover:bg-muted/50"
  >
    <span
      class="notification-level-badge mt-0.5 shrink-0 inline-flex h-5 w-5 items-center justify-center rounded-full text-[10px] font-bold"
      :class="levelClass"
    >
      {{ levelAbbreviation }}
    </span>
    <div class="min-w-0 flex-1">
      <div class="flex items-center gap-2 text-xs text-muted-foreground">
        <span class="notification-scope-badge rounded bg-muted px-1.5 py-0.5 font-medium">{{ scopeLabel }}</span>
        <span>{{ relativeTime }}</span>
      </div>
      <p class="mt-0.5 text-sm font-medium leading-snug text-foreground">{{ notification.title }}</p>
      <p v-if="showBody" class="mt-0.5 line-clamp-3 text-xs text-muted-foreground">{{ notification.body }}</p>
      <div class="mt-2 flex items-center gap-2">
        <router-link
          v-if="notification.action_url"
          :to="notification.action_url"
          class="text-xs font-medium text-primary hover:underline"
        >
          View
        </router-link>
      </div>
    </div>
    <div class="notification-actions absolute right-2 top-2 hidden gap-1 group-hover:flex">
      <button
        type="button"
        class="rounded px-2 py-1 text-[11px] font-medium text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
        @click="$emit('review-later', notification.id)"
        :title="$t('components.NotificationCard.hide_from_dashboard_keep_in_notifications_page')"
      >
        Review Later
      </button>
      <button
        type="button"
        class="rounded px-2 py-1 text-[11px] font-medium text-muted-foreground hover:bg-muted hover:text-destructive transition-colors"
        @click="showDismiss = true"
        :title="$t('components.NotificationCard.dismiss_this_notification')"
      >
        Dismiss
      </button>
    </div>
  </div>
  <DismissDialog
    v-if="showDismiss"
    :notification="notification"
    @confirm="onDismiss"
    @cancel="showDismiss = false"
  />
  <p v-if="dismissError" class="mt-1 text-xs text-destructive">{{ dismissError }}</p>
</template>

<script setup lang="ts">
import { ref, computed } from "vue";
import type { NotificationResponse } from "../lib/api/notifications";
import { dismissNotification } from "../lib/api/notifications";
import DismissDialog from "./DismissDialog.vue";
import { formatDistanceToNow } from "date-fns";

const props = defineProps<{
  notification: NotificationResponse;
  showBody?: boolean;
}>();

const emit = defineEmits<{
  dismissed: [id: string];
  "review-later": [id: string];
}>();

const showDismiss = ref(false);
const dismissError = ref("");

const levelClass = computed(() => {
  const map: Record<string, string> = {
    error: "bg-destructive/10 text-destructive",
    warning: "bg-warning/10 text-warning",
    info: "bg-primary/10 text-primary",
    debug: "bg-muted text-muted-foreground",
  };
  return map[props.notification.level] || "bg-muted text-muted-foreground";
});

const levelAbbreviation = computed(() => {
  const level = props.notification.level;
  if (!level) return "?";
  return level.charAt(0).toUpperCase();
});

const scopeLabel = computed(() => props.notification.scope_label);

const relativeTime = computed(() => {
  const raw = props.notification.created_at;
  if (!raw) return "";
  const created = new Date(raw);
  if (Number.isNaN(created.getTime())) return "";
  return formatDistanceToNow(created, { addSuffix: true });
});

async function onDismiss(scope: "self" | "scope") {
  dismissError.value = "";
  try {
    await dismissNotification(props.notification.id, scope);
    showDismiss.value = false;
    emit("dismissed", props.notification.id);
  } catch (e: unknown) {
    dismissError.value = e instanceof Error ? e.message : "Failed to dismiss notification";
  }
}
</script>
