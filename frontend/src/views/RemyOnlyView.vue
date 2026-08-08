<template>
  <div class="remy-only-view flex h-screen flex-col overflow-hidden bg-background" data-testid="remy-only-view">
    <header class="flex items-center justify-between gap-3 border-b bg-card px-4 py-2">
      <div class="flex min-w-0 items-center gap-2">
        <div class="flex items-center justify-center rounded-lg bg-primary/10 p-1.5">
          <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect width="18" height="10" x="3" y="11" rx="2" />
            <circle cx="12" cy="5" r="2" />
            <path d="M12 7v4" />
            <line x1="8" y1="16" x2="8" y2="16" />
            <line x1="16" y1="16" x2="16" y2="16" />
          </svg>
        </div>
        <div class="min-w-0">
          <h1 class="truncate text-sm font-semibold">Remy</h1>
          <span class="text-xs text-muted-foreground">{{ $t('components.remy.RemyOnlyView.remy_only_mode') }}</span>
        </div>
      </div>
      <Button
        variant="outline"
        size="sm"
        data-testid="remy-only-return-home"
        @click="returnHome"
      >
        <ArrowLeft class="mr-1 h-3.5 w-3.5" aria-hidden="true" />
        {{ $t('components.remy.RemyOnlyView.return_home') }}
      </Button>
    </header>

    <div
      class="border-b bg-primary/5 px-4 py-1.5 text-xs text-muted-foreground"
      data-testid="remy-only-banner"
    >
      {{ $t('components.remy.RemyOnlyView.banner') }}
    </div>

    <template v-if="remyUnavailable">
      <div class="flex flex-1 items-center justify-center p-8" data-testid="remy-only-unavailable">
        <div class="max-w-md space-y-2 text-center">
          <h2 class="text-lg font-semibold">{{ $t('components.remy.RemyOnlyView.remy_unavailable') }}</h2>
          <p class="text-sm text-muted-foreground">{{ $t('components.remy.RemyOnlyView.remy_unavailable_desc') }}</p>
          <Button variant="outline" size="sm" data-testid="remy-only-return-home" @click="returnHome">
            {{ $t('components.remy.RemyOnlyView.return_home') }}
          </Button>
        </div>
      </div>
    </template>

    <template v-else-if="store.sessionsLoading">
      <div class="flex flex-1 items-center justify-center">
        <span class="text-sm text-muted-foreground">{{ $t('common.loading') }}</span>
      </div>
    </template>

    <template v-else>
      <div
        class="flex items-center gap-1.5 overflow-x-auto border-b px-3 py-1.5"
        data-testid="remy-only-tab-bar"
      >
        <button
          v-for="tab in tabsStore.tabs"
          :key="tab.tabId"
          class="remy-only-tab"
          :class="{ active: tab.sessionId === store.activeSessionId }"
          :data-testid="`remy-only-tab-${tab.tabId}`"
          :aria-current="tab.sessionId === store.activeSessionId ? 'page' : undefined"
          :title="sessionTitle(tab.sessionId)"
          @click="selectTab(tab)"
        >
          <span class="max-w-[180px] truncate">{{ sessionTitle(tab.sessionId) }}</span>
          <span
            v-if="tab.sessionId === store.activeSessionId && store.isStreaming"
            class="remy-live-dot"
            :title="$t('components.remy.RemyOnlyView.live')"
            :aria-label="$t('components.remy.RemyOnlyView.live')"
          />
          <button
            class="remy-only-tab-close"
            :aria-label="$t('components.remy.RemyOnlyView.close_tab')"
            :title="$t('components.remy.RemyOnlyView.close_tab')"
            @click.stop="closeTab(tab)"
          >
            <X class="h-3 w-3" aria-hidden="true" />
          </button>
        </button>
        <button
          class="remy-only-new-tab"
          data-testid="remy-only-new-tab"
          :title="$t('components.remy.RemyOnlyView.new_tab')"
          :aria-label="$t('components.remy.RemyOnlyView.new_tab')"
          @click="startNew"
        >
          <Plus class="h-3.5 w-3.5" aria-hidden="true" />
        </button>
      </div>

      <div class="flex items-center gap-1 border-b px-3">
        <button
          class="remy-only-subtab"
          :class="{ active: subTab === 'chat' }"
          @click="subTab = 'chat'"
        >
          {{ $t('components.remy.RemyOnlyView.chat_tab') }}
        </button>
        <button
          class="remy-only-subtab"
          :class="{ active: subTab === 'skills' }"
          @click="subTab = 'skills'"
        >
          {{ $t('components.remy.RemyOnlyView.skills_tab') }}
        </button>
        <button
          class="remy-only-subtab"
          :class="{ active: subTab === 'sources' }"
          @click="subTab = 'sources'"
        >
          {{ $t('components.remy.RemyOnlyView.sources_tab') }}
        </button>
        <button
          class="remy-only-subtab"
          :class="{ active: subTab === 'sessions' }"
          @click="subTab = 'sessions'"
        >
          {{ $t('components.remy.RemyOnlyView.sessions_tab') }}
        </button>
      </div>

      <div
        v-if="tabsStore.tabs.length === 0"
        class="flex flex-1 items-center justify-center p-8"
        data-testid="remy-only-empty"
      >
        <div class="space-y-3 text-center">
          <h2 class="text-lg font-semibold">{{ $t('components.remy.RemyOnlyView.no_tabs_yet') }}</h2>
          <p class="text-sm text-muted-foreground">{{ $t('components.remy.RemyOnlyView.start_new') }}</p>
          <Button size="sm" @click="startNew">{{ $t('components.remy.RemyOnlyView.start_new') }}</Button>
        </div>
      </div>

      <div
        v-else-if="!store.activeSessionId"
        class="flex flex-1 items-center justify-center p-8"
        data-testid="remy-only-no-active"
      >
        <div class="space-y-3 text-center">
          <p class="text-sm text-muted-foreground">{{ $t('components.remy.RemyOnlyView.select_tab_or_new') }}</p>
          <Button size="sm" @click="startNew">{{ $t('components.remy.RemyOnlyView.start_new') }}</Button>
        </div>
      </div>

      <div v-else class="flex flex-1 overflow-hidden" data-testid="remy-only-chat">
        <div class="flex flex-1 flex-col overflow-hidden">
          <RemyChat v-show="subTab === 'chat'" ref="chatRef" remyOnly class="flex-1" />
          <div v-show="subTab === 'skills'" class="flex flex-1 overflow-hidden">
            <RemySkillManager />
          </div>
          <div v-show="subTab === 'sources'" class="flex flex-1 overflow-hidden">
            <RemyContextSources />
          </div>
          <div v-show="subTab === 'sessions'" class="flex flex-1 overflow-hidden">
            <RemySessionDrawer @select-session="onSessionSelected" />
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Button } from '@/components/ui/button'
import { ArrowLeft, Plus, X } from '@lucide/vue'
import { shortId } from '@/utils/format'
import { useRemyStore } from '@/composables/useRemyStore'
import { useRemyTabsStore, type RemyTab } from '@/composables/useRemyTabsStore'
import { usePlanStore } from '@/stores/planStore'
import RemyChat from '@/components/remy/RemyChat.vue'
import RemySkillManager from '@/components/remy/RemySkillManager.vue'
import RemyContextSources from '@/components/remy/RemyContextSources.vue'
import RemySessionDrawer from '@/components/remy/RemySessionDrawer.vue'

const router = useRouter()
const store = useRemyStore()
const tabsStore = useRemyTabsStore()
const planStore = usePlanStore()
const chatRef = ref<InstanceType<typeof RemyChat> | null>(null)
const subTab = ref<'chat' | 'skills' | 'sources' | 'sessions'>('chat')

const remyUnavailable = computed(() => !planStore.devMode)

function sessionTitle(sessionId: string): string {
  const session = store.sessions.find(s => s.id === sessionId)
  if (!session) return '...'
  if (session.name) return session.name
  return session.session_number ? `Session #${session.session_number}` : shortId(session.id)
}

function returnHome() {
  router.push('/')
}

async function startNew() {
  chatRef.value?.disconnect()
  await tabsStore.addTab()
  subTab.value = 'chat'
}

function selectTab(tab: RemyTab) {
  if (tab.sessionId === store.activeSessionId) {
    subTab.value = 'chat'
    return
  }
  chatRef.value?.disconnect()
  tabsStore.resumeTab(tab.sessionId)
  subTab.value = 'chat'
}

function closeTab(tab: RemyTab) {
  chatRef.value?.disconnect()
  tabsStore.closeTab(tab.tabId)
}

function onSessionSelected() {
  if (store.activeSessionId) {
    chatRef.value?.disconnect()
    tabsStore.resumeTab(store.activeSessionId)
    subTab.value = 'chat'
  }
}

watch(
  () => store.activeSessionId,
  (id) => {
    if (!id) return
    const live = Array.isArray(store.sessions) ? store.sessions.some(s => s.id === id) : false
    if (live && !tabsStore.tabs.some(t => t.sessionId === id)) {
      tabsStore.resumeTab(id)
    }
  },
)

onMounted(async () => {
  if (!planStore.loaded) {
    await planStore.fetchPlan().catch(() => {})
  }
  await store.fetchSessions()
  tabsStore.reconcile()
  if (store.activeSessionId) {
    await store.loadSession(store.activeSessionId)
  }
})
</script>

<style scoped>
.remy-only-tab {
  @apply flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium transition-colors border-b-2 border-transparent;
  color: hsl(var(--muted-foreground));
  max-width: 220px;
}
.remy-only-tab:hover {
  color: hsl(var(--foreground));
  background-color: hsl(var(--accent));
}
.remy-only-tab.active {
  color: hsl(var(--primary));
  background-color: hsla(var(--primary) / 0.08);
}
.remy-only-tab-close {
  @apply flex items-center justify-center rounded p-0.5 transition-colors shrink-0;
  color: hsl(var(--muted-foreground));
}
.remy-only-tab-close:hover {
  color: hsl(var(--foreground));
  background-color: hsl(var(--accent));
}
.remy-live-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background-color: hsl(var(--primary));
  animation: remy-live-pulse 1.5s ease-in-out infinite;
}
@keyframes remy-live-pulse {
  0%,
  100% {
    opacity: 1;
    transform: scale(1);
  }
  50% {
    opacity: 0.4;
    transform: scale(0.75);
  }
}
.remy-only-new-tab {
  @apply flex items-center justify-center rounded-md p-1.5 transition-colors shrink-0;
  color: hsl(var(--muted-foreground));
}
.remy-only-new-tab:hover {
  color: hsl(var(--foreground));
  background-color: hsl(var(--accent));
}
.remy-only-subtab {
  @apply px-2.5 py-1.5 text-xs font-medium transition-colors border-b-2 border-transparent;
  color: hsl(var(--muted-foreground));
}
.remy-only-subtab:hover {
  color: hsl(var(--foreground));
}
.remy-only-subtab.active {
  color: hsl(var(--primary));
  border-bottom-color: hsl(var(--primary));
}
</style>
