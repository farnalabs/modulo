<template>
  <div class="remy-sessions">
    <div class="flex items-center justify-between p-3 border-b">
      <h3
        class="text-xs font-semibold uppercase tracking-wider text-muted-foreground"
      >
        Sessions
      </h3>
      <Button severity="secondary" text icon-only @click="handleNewSession" :title="$t('components.remy.RemySessionDrawer.new_session')" :aria-label="$t('components.remy.new_session')">
        <svg
          xmlns="http://www.w3.org/2000/svg"
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
        >
          <line x1="12" y1="5" x2="12" y2="19" />
          <line x1="5" y1="12" x2="19" y2="12" />
        </svg>
      </Button>
    </div>

    <div
      v-if="store.sessionsLoading"
      class="flex items-center justify-center py-8"
    >
      <span class="text-sm text-muted-foreground">{{ $t('components.remy.RemySessionDrawer.loading') }}</span>
    </div>

    <div
      v-else-if="store.sortedSessions.length === 0"
      class="flex flex-col items-center justify-center py-8 px-4 text-center"
    >
      <p class="text-sm text-muted-foreground">{{ $t('components.remy.RemySessionDrawer.no_sessions_yet') }}</p>
      <Button severity="primary" link size="small" @click="handleNewSession" class="mt-2">{{ $t('components.remy.RemySessionDrawer.start_a_new_chat') }}</Button>
    </div>

    <div v-else class="divide-y">
      <button
        v-for="session in store.sortedSessions"
        :key="session.id"
        type="button"
        class="remy-session-item w-full text-left"
        :class="{ active: session.id === store.activeSessionId }"
        @click="selectSession(session.id)"
      >
        <div class="flex items-start justify-between gap-2">
          <span class="text-sm font-medium truncate flex-1">
            {{ session.name || `Session ${session.session_number ? '#' + session.session_number : shortId(session.id)}` }}
          </span>
          <button
            type="button"
            class="remy-session-delete shrink-0"
            @click.stop="handleDelete(session.id)"
            title="Delete"
            :aria-label="$t('components.remy.delete_session')"
          >
            <svg
              xmlns="http://www.w3.org/2000/svg"
              width="12"
              height="12"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
            >
              <polyline points="3 6 5 6 21 6" />
              <path
                d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"
              />
            </svg>
          </button>
        </div>
        <div class="flex items-center gap-2 mt-1">
          <span class="text-xs text-muted-foreground"
            >{{ session.message_count }} msgs</span
          >
          <span class="text-xs text-muted-foreground">&middot;</span>
          <span class="text-xs text-muted-foreground">{{
            formatTime(session.updated_at)
          }}</span>
        </div>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { shortId } from "@/utils/format";
import { useRemyStore } from "@/composables/useRemyStore";
import Button from 'primevue/button'
import { formatDateShort } from "@/lib/formatDate";

const emit = defineEmits<{
  close: [];
  selectSession: [];
}>();

const store = useRemyStore();

function selectSession(id: string) {
  store.loadSession(id);
  emit("selectSession");
}

async function handleNewSession() {
  try {
    const session = await store.createSession();
    if (session) {
      emit("selectSession");
    }
  } catch (e) {
    console.error("Failed to create session:", e);
  }
}

async function handleDelete(id: string) {
  try {
    await store.deleteSession(id);
  } catch (e) {
    console.error("Failed to delete session:", e);
  }
}

function formatTime(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  const ts = d.getTime();
  if (Number.isNaN(ts)) return iso;
  const now = new Date();
  const diffMs = now.getTime() - ts;
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.floor(diffHr / 24);
  if (diffDay < 7) return `${diffDay}d ago`;
  return formatDateShort(d);
}
</script>

<style scoped>
.remy-sessions {
  @apply flex flex-col h-full;
}
.remy-session-item {
  @apply px-3 py-2.5 transition-colors;
}
.remy-session-item:hover {
  background-color: hsl(var(--accent));
}
.remy-session-item.active {
  background-color: hsla(var(--primary) / 0.08);
  border-left: 2px solid hsl(var(--primary));
}
.remy-session-delete {
  @apply rounded p-1 opacity-0 transition-opacity;
  color: hsl(var(--muted-foreground));
}
.remy-session-item:hover .remy-session-delete {
  opacity: 1;
}
.remy-session-delete:hover {
  color: hsl(var(--destructive));
  background-color: hsla(var(--destructive) / 0.1);
}
</style>
