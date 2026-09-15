<template>
  <div
    class="notification-card group relative flex items-start gap-3 rounded-lg border p-3 transition-colors hover:bg-muted/50"
  >
    <span
      class="notification-level-badge mt-0.5 shrink-0 inline-flex h-5 w-5 items-center justify-center rounded-full text-[10px] font-bold"
      :class="levelClass"
      aria-hidden="true"
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

      <!-- Lapsed HITL affordance -->
      <div
        v-if="isLapsedHitl"
        class="mt-2 flex items-center gap-2 rounded-md bg-muted/60 px-3 py-2 text-xs text-muted-foreground"
        role="status"
        aria-live="polite"
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="shrink-0 text-muted-foreground" aria-hidden="true">
          <circle cx="12" cy="12" r="10"/>
          <line x1="12" y1="8" x2="12" y2="12"/>
          <line x1="12" y1="16" x2="12.01" y2="16"/>
        </svg>
        <span>{{ $t('components.NotificationCard.lapsed_hitl') }}</span>
      </div>

      <div class="mt-2 flex items-center gap-2">
        <router-link
          v-if="notification.action_url"
          :to="notification.action_url"
          class="text-xs font-medium text-primary hover:underline"
        >
          {{ isLapsedHitl ? $t('components.NotificationCard.lapsed_hitl_view_run') : 'View' }}
        </router-link>
      </div>
    </div>
    <div class="notification-actions absolute right-2 top-2 hidden gap-1 group-hover:flex">
      <button
        type="button"
        class="rounded px-2 py-1 text-[11px] font-medium text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
        :aria-label="$t('components.NotificationCard.review_later')"
        @click="$emit('review-later', notification.id)"
      >
        {{ $t('components.NotificationCard.review_later') }}
      </button>
      <button
        type="button"
        class="rounded px-2 py-1 text-[11px] font-medium text-muted-foreground hover:bg-muted hover:text-destructive transition-colors"
        :aria-expanded="showDismiss"
        :aria-label="$t('components.NotificationCard.dismiss_this_notification')"
        @click="showDismiss = true"
      >
        {{ $t('components.NotificationCard.dismiss_this_notification') }}
      </button>
    </div>
  </div>
  <DismissDialog
    :notification="notification"
    v-model="showDismiss"
    :trigger-ref="dismissButtonRef"
    @confirm="onDismiss"
  />
  <p v-if="dismissError" class="mt-1 text-xs text-destructive" role="alert">{{ dismissError }}</p>
</template>

<script setup lang="ts">
import { ref, computed } from "vue";
import type { NotificationResponse } from "../lib/api/notifications";
import { dismissNotification } from "../lib/api/notifications";
import DismissDialog from "./DismissDialog.vue";
import { formatRelativeTime } from "../lib/formatDate";

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
const dismissButtonRef = ref<HTMLElement | null>(null);

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

const relativeTime = computed(() => formatRelativeTime(props.notification.created_at));

/**
 * Detect lapsed HITL notifications: category starts with "hitl." and the
 * notification title contains "HITL review" or "review needed" — indicating
 * it was an awaiting notification whose gate has since lapsed.
 * We show a lapsed affordance when the category is hitl.awaiting (the original
 * review request) because the only way a user sees this is if the gate expired
 * and the run was cancelled. The notification is immutable (not retracted).
 */
const isLapsedHitl = computed(() => {
  const cat = props.notification.category || "";
  return cat === "hitl.awaiting";
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
