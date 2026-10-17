<template>
  <!-- FAR-1926: teleported to <body> so the panel's `fixed` placement is not
       trapped in the host chrome's stacking context — the sticky sidebar
       rails paint BELOW <main> (both are z-index:auto positioned siblings in
       tree order), so a non-teleported panel is geometrically on screen yet
       covered by page content. At body level the z-50 panel paints above
       every in-page surface, while `fixed` escapes the rails' overflow clip. -->
  <teleport to="body">
    <dialog
      id="notification-dropdown"
      ref="dropdownRef"
      data-testid="notification-dropdown"
      open
      :aria-label="$t('components.NotificationDropdown.title')"
      class="z-50 block rounded-md border border-border bg-popover text-popover-foreground shadow-md"
      :style="positionStyle"
      @keydown.esc.stop.prevent="$emit('close')"
    >
      <div class="flex items-center justify-between border-b border-border px-3 py-2">
        <h2 class="text-sm font-semibold text-foreground">
          {{ $t("components.NotificationDropdown.title") }}
        </h2>
        <button
          type="button"
          data-testid="notification-mark-all-read"
          class="rounded px-2 py-1 text-xs font-medium text-primary hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50"
          :disabled="store.unreadCount === 0 || dismissingAll"
          @click="onDismissAll"
        >
          {{ dismissingAll ? $t("components.NotificationDropdown.dismissing_all") : $t("components.NotificationDropdown.dismiss_all") }}
        </button>
      </div>

      <!-- Failure, success and no-op are MUTUALLY EXCLUSIVE outcomes of THIS
           panel's own attempt; dismissAllError is a local ref (never shared
           store state, QA FAR-1926) and dies with the panel on close. -->
      <p
        v-if="dismissAllError"
        data-testid="notification-mark-all-read-error"
        role="alert"
        class="border-b border-border px-3 py-2 text-xs text-destructive"
      >
        {{ dismissAllError }}
      </p>
      <output
        v-else-if="dismissedAll"
        data-testid="notification-mark-all-read-success"
        :aria-label="$t('components.NotificationDropdown.dismissed_all_status')"
        aria-live="polite"
        class="block border-b border-border px-3 py-2 text-xs text-muted-foreground"
      >
        {{ $t("components.NotificationDropdown.dismissed_all_status") }}
      </output>
      <!-- The server marked nothing (no-op re-run): neutral copy, never the
           dismissed-success claim. -->
      <output
        v-else-if="nothingToDismiss"
        data-testid="notification-mark-all-read-nothing"
        :aria-label="$t('components.NotificationDropdown.nothing_to_dismiss')"
        aria-live="polite"
        class="block border-b border-border px-3 py-2 text-xs text-muted-foreground"
      >
        {{ $t("components.NotificationDropdown.nothing_to_dismiss") }}
      </output>

      <LoadingSpinner v-if="store.loadingRecent" />
      <!-- Fetch failed AND there is nothing previously loaded to fall back to:
           an error state, never the "No notifications" empty state, which would
           falsely claim the feed is clear (FAR-249 QA). When earlier items ARE
           kept (fail-open) the list below stays visible instead. -->
      <div
        v-else-if="store.recentNotifications.length === 0 && store.fetchError"
        data-testid="notification-dropdown-error"
        role="alert"
        class="px-3 py-4 text-center text-sm text-destructive"
      >
        <p>{{ $t("components.NotificationDropdown.failed_to_load") }}</p>
        <button
          type="button"
          data-testid="notification-dropdown-retry"
          class="mt-2 rounded px-2 py-1 text-xs font-medium text-primary hover:bg-muted"
          @click="store.fetchRecent()"
        >
          {{ $t("components.NotificationDropdown.retry") }}
        </button>
      </div>
      <p
        v-else-if="store.recentNotifications.length === 0"
        data-testid="notification-dropdown-empty"
        class="px-3 py-4 text-center text-sm text-muted-foreground"
      >
        {{ $t("components.NotificationDropdown.no_notifications") }}
      </p>
      <ul v-else class="max-h-80 overflow-y-auto divide-y divide-border">
        <li v-for="n in store.recentNotifications" :key="n.id">
          <component
            :is="n.action_url ? 'router-link' : 'div'"
            :to="n.action_url || undefined"
            class="block px-3 py-2 transition-colors hover:bg-muted/50"
            @click="$emit('close')"
          >
            <div class="flex items-start gap-2">
              <span
                class="mt-1.5 h-2 w-2 shrink-0 rounded-full"
                :class="levelDotClass(n.level)"
                aria-hidden="true"
              />
              <div class="min-w-0">
                <p class="truncate text-sm font-medium text-foreground">{{ n.title }}</p>
                <p class="line-clamp-2 text-xs text-muted-foreground">{{ n.body }}</p>
                <p class="mt-0.5 text-[11px] text-muted-foreground">
                  {{ formatRelativeTime(n.created_at) }}
                </p>
              </div>
            </div>
          </component>
        </li>
      </ul>

      <div class="border-t border-border px-3 py-2 text-center">
        <router-link
          to="/notifications"
          data-testid="notification-dropdown-view-all"
          class="text-xs font-medium text-primary hover:underline"
          @click="$emit('close')"
        >
          {{ $t("components.NotificationDropdown.view_all") }}
        </router-link>
      </div>
    </dialog>
  </teleport>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { useNotificationsStore } from "../stores/notifications";
import { formatRelativeTime } from "../lib/formatDate";
import LoadingSpinner from "./shared/LoadingSpinner.vue";

const props = defineProps<{
  /**
   * The trigger to anchor to (the bell button). Because the panel teleports
   * to <body>, the bell is no longer a DOM ancestor — the host passes it so
   * placement can measure it. Falls back to the panel's parentElement when
   * omitted (direct mounts, unit tests).
   */
  anchorEl?: HTMLElement | null;
}>();

defineEmits<{ close: [] }>();

/** Tailwind `w-80` — the panel's design width, bounded below on narrow screens. */
const PANEL_WIDTH = 320;
/** Viewport gutter the panel never crosses horizontally or vertically. */
const VIEWPORT_MARGIN = 8;

const store = useNotificationsStore();
const dropdownRef = ref<HTMLElement | null>(null);
const dismissingAll = ref(false);
const dismissedAll = ref(false);
/** No-op outcome (server marked 0 rows): mutually exclusive with success. */
const nothingToDismiss = ref(false);
/**
 * THIS panel's dismiss-all error (QA FAR-1926) — a local ref, never shared
 * store state, so a failure here cannot leak onto the dashboard panel or the
 * notifications page. Cleared at the start of each attempt; it also resets
 * naturally on close because the bell mounts this panel behind `v-if`.
 */
const dismissAllError = ref<string | null>(null);

/**
 * Placement of the panel (FAR-1926). Starts as the legacy bell-anchored
 * popover so the panel is never unstyled for a frame, then `computePosition`
 * replaces it with a viewport-clamped `fixed` placement on mount.
 */
const positionStyle = ref<Record<string, string>>({
  position: "absolute",
  right: "0",
  top: "100%",
  marginTop: "4px",
  width: `${PANEL_WIDTH}px`,
});

/**
 * Collision-aware placement: right-align the panel to the bell when that
 * stays inside the window, otherwise clamp it into the viewport. `fixed` (not
 * `absolute`) is load-bearing — the rails and the expanded sidebar clip their
 * descendants with `overflow-hidden`, and only a fixed element, whose
 * containing block is the viewport, escapes that clip. Combined with the
 * teleport to <body>, that is what keeps the panel fully visible (and above
 * page content) in the mobile icon rail, the mobile drawer header, the
 * collapsed desktop rail and the expanded desktop sidebar alike.
 */
function computePosition(): void {
  const panel = dropdownRef.value;
  // The bell passes its button element; direct mounts fall back to the
  // panel's parent (the bell root when teleport is stubbed inline).
  const anchor = props.anchorEl ?? panel?.parentElement;
  if (!panel || !anchor) return;
  const rect = anchor.getBoundingClientRect();
  const viewportWidth = window.innerWidth;
  const viewportHeight = window.innerHeight;
  const width = Math.min(PANEL_WIDTH, viewportWidth - VIEWPORT_MARGIN * 2);
  const maxLeft = Math.max(VIEWPORT_MARGIN, viewportWidth - width - VIEWPORT_MARGIN);
  const left = Math.min(Math.max(rect.right - width, VIEWPORT_MARGIN), maxLeft);
  const top = rect.bottom + 4; // the legacy `top-full mt-1` gap
  positionStyle.value = {
    position: "fixed",
    left: `${left}px`,
    top: `${top}px`,
    width: `${width}px`,
    // Never taller than the space below the bell; the inner list scrolls.
    maxHeight: `${Math.max(160, viewportHeight - top - VIEWPORT_MARGIN)}px`,
    overflowY: "auto",
  };
}

onMounted(() => {
  // The bell fetches on open; a direct mount (tests, future embedders) still
  // gets a list without a second click.
  if (!store.loadingRecent && store.recentNotifications.length === 0) {
    void store.fetchRecent();
  }
  // Synchronous, same task as the mount — the clamped placement lands before
  // the first paint, so the panel never flashes at its unclamped anchor.
  computePosition();
  window.addEventListener("resize", computePosition);
  dropdownRef.value
    ?.querySelector<HTMLElement>('button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])')
    ?.focus();
});

onUnmounted(() => {
  window.removeEventListener("resize", computePosition);
});

async function onDismissAll(): Promise<void> {
  dismissingAll.value = true;
  dismissedAll.value = false;
  nothingToDismiss.value = false;
  dismissAllError.value = null;
  try {
    // One shared action path (FAR-1926); the returned count keeps the copy
    // truthful: a no-op (marked === 0) reports "Nothing to dismiss".
    const marked = await store.dismissAll();
    dismissedAll.value = marked > 0;
    nothingToDismiss.value = marked === 0;
  } catch (e: unknown) {
    // The store throws an Error carrying the formatted API message; keep it
    // in this panel's local ref (never shared state). Focus stays on the
    // button so the user can retry, and the list is never shown cleared.
    dismissAllError.value = e instanceof Error ? e.message : String(e);
  } finally {
    dismissingAll.value = false;
  }
}

/** Level severity dot — matches the dashboard panel's colour language. */
function levelDotClass(level: string): string {
  switch (level) {
    case "error":
      return "bg-destructive";
    case "warning":
      return "bg-amber-500";
    case "info":
      return "bg-blue-500";
    default:
      return "bg-muted-foreground";
  }
}
</script>
