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

      <!-- FAR-1234: point-in-time run metadata resolved when the list loads.
           The notification body still says what the trigger knew; this line
           says what the linked run looks like NOW. -->
      <p
        v-if="hasRunState"
        data-testid="notification-run-state"
        class="mt-1 flex flex-wrap items-center gap-x-1 text-xs"
        :class="runStateClass"
        role="status"
        aria-live="polite"
      >
        <span class="font-medium">{{ runStateLabel }}</span>
        <span v-if="cancelReasonText" class="text-muted-foreground">{{ cancelReasonText }}</span>
      </p>

      <!-- HITL awaiting affordance: only while the linked run can still be
           reviewed. A terminal run (cancelled/failed/…) demotes this to an
           explanatory label so a stale request never reads as live work. -->
      <div
        v-if="hitlAffordance"
        class="mt-2 flex items-center gap-2 rounded-md px-3 py-2 text-xs"
        :class="hitlAffordanceClass"
        role="status"
        aria-live="polite"
      >
        <svg
          v-if="isHitlActionable"
          xmlns="http://www.w3.org/2000/svg"
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          class="shrink-0 text-muted-foreground"
          aria-hidden="true"
        >
          <circle cx="12" cy="12" r="10"/>
          <line x1="12" y1="8" x2="12" y2="12"/>
          <line x1="12" y1="16" x2="12.01" y2="16"/>
        </svg>
        <span>{{ hitlAffordance }}</span>
      </div>

      <div class="mt-2 flex items-center gap-2">
        <router-link
          v-if="notification.action_url"
          :to="notification.action_url"
          class="text-xs font-medium text-primary hover:underline"
        >
          {{ isHitlAwaiting ? $t('components.NotificationCard.awaiting_hitl_view_run') : $t('components.NotificationCard.view') }}
        </router-link>
      </div>
    </div>
    <!-- FAR-1234: visibility is driven by the scoped rules below (hover,
         focus-within, and no-hover pointers) — not by `hidden`+`group-hover`,
         which left these controls out of the tab order entirely. -->
    <div class="notification-actions absolute right-2 top-2 gap-1">
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
import { useI18n } from "vue-i18n";
import type { NotificationResponse } from "../lib/api/notifications";
import { dismissNotification } from "../lib/api/notifications";
import DismissDialog from "./DismissDialog.vue";
import { formatRelativeTime } from "../lib/formatDate";
import { runStatusLabel, cancelReasonLabel } from "../utils/runUtils";

const props = defineProps<{
  notification: NotificationResponse;
  showBody?: boolean;
}>();

const emit = defineEmits<{
  dismissed: [id: string];
  "review-later": [id: string];
}>();

const { t } = useI18n();

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

/** The run's CURRENT status, or null when the notification is not run-linked
 *  (or the run could not be resolved at read time). */
const runStatus = computed(() => props.notification.run_status ?? null);

const hasRunState = computed(() => runStatus.value !== null);

const runIsTerminal = computed(() => props.notification.run_terminal === true);

/** Localised status word, resolved from the shared run-status vocabulary
 *  (the same `runStatusLabel` the runs list and run detail use). Unknown
 *  statuses humanise the raw value; absent ones never render (hasRunState
 *  guards the block). */
const runStateLabel = computed(() => runStatusLabel(runStatus.value));

const runStateClass = computed(() => {
  if (runIsTerminal.value) return "text-muted-foreground";
  return "text-primary";
});

/** Cancel-reason phrase (FAR-1233 ``runs.cancel_reason``), shown only alongside
 *  a cancelled run. Resolved from the shared `cancelReasonLabel` util so the
 *  copy matches the run-detail view; a NULL/unknown reason is the first-class
 *  "never recorded" case and is stated as such rather than guessed. */
const cancelReasonText = computed(() => {
  if (runStatus.value !== "cancelled") return "";
  return cancelReasonLabel(props.notification.run_cancel_reason, t);
});

/**
 * HITL awaiting notifications (category === "hitl.awaiting") signal an open
 * human-in-the-loop gate for the linked run. The backend never retracts or
 * status-flips these notifications on gate resume, so the notification alone
 * cannot tell us whether the gate still stands — but FAR-1234 now resolves the
 * linked run's CURRENT state at read time, so:
 *   - terminal run  -> the request is stale: demote to an explanatory label;
 *   - live/unresolved run -> keep the neutral "awaiting your review" wording
 *     (never a "lapsed" claim, which would be a guess).
 */
const isHitlAwaiting = computed(() => {
  const cat = props.notification.category || "";
  return cat === "hitl.awaiting";
});

const isHitlActionable = computed(
  () => isHitlAwaiting.value && (!hasRunState.value || !runIsTerminal.value),
);

const hitlAffordance = computed(() => {
  if (!isHitlAwaiting.value) return "";
  if (isHitlActionable.value) return t("components.NotificationCard.awaiting_hitl");
  return t("components.NotificationCard.hitl_run_terminal");
});

const hitlAffordanceClass = computed(() => {
  if (isHitlActionable.value) return "bg-muted/60 text-muted-foreground";
  return "bg-muted/40 text-muted-foreground";
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

<style scoped>
/*
 * FAR-1234 — these actions used to be `hidden group-hover:flex`.
 * `display: none` removes an element from the tab order, so a keyboard (or
 * assistive-tech) user had NO way to dismiss a notification, and a pointer
 * without hover (touch) never revealed them at all. Reveal on card hover (the
 * original behaviour), on focus-within (keyboard), and whenever the device
 * cannot hover (touch).
 */
.notification-card .notification-actions {
  display: none;
}

.notification-card:hover .notification-actions,
.notification-card:focus-within .notification-actions {
  display: flex;
}

@media (hover: none) {
  /*
   * Touch: the controls are always visible AND dropped into normal flow as a
   * trailing full-width row — absolutely positioned over the card they would
   * cover the title/body, which wraps to several lines on a narrow screen.
   */
  .notification-card {
    flex-wrap: wrap;
  }

  .notification-card .notification-actions {
    position: static;
    display: flex;
    width: 100%;
    justify-content: flex-end;
  }
}
</style>
