<template>
  <div class="assistant-only-view flex h-screen flex-col overflow-hidden bg-background" data-testid="assistant-only-view">
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
          <h1 class="truncate text-sm font-semibold">{{ $t('components.assistant.AssistantOnlyView.assistant_title') }}</h1>
          <span class="text-xs text-muted-foreground">{{ $t('components.assistant.AssistantOnlyView.assistant_only_mode') }}</span>
        </div>
      </div>
      <Button severity="secondary" outlined size="small" data-testid="assistant-only-return-home" @click="returnHome">
        <ArrowLeft class="mr-1 h-3.5 w-3.5" aria-hidden="true" />
        {{ $t('components.assistant.AssistantOnlyView.return_home') }}
      </Button>
    </header>

    <div
      class="border-b bg-primary/5 px-4 py-1.5 text-xs text-muted-foreground"
      data-testid="assistant-only-banner"
    >
      {{ $t('components.assistant.AssistantOnlyView.banner') }}
    </div>

    <template v-if="assistantUnavailable">
      <div class="flex flex-1 items-center justify-center p-8" data-testid="assistant-only-unavailable">
        <div class="max-w-md space-y-2 text-center">
          <h2 class="text-lg font-semibold">{{ $t('components.assistant.AssistantOnlyView.assistant_unavailable') }}</h2>
          <p class="text-sm text-muted-foreground">{{ $t('components.assistant.AssistantOnlyView.assistant_unavailable_desc') }}</p>
          <Button severity="secondary" outlined size="small" data-testid="assistant-only-return-home" @click="returnHome">
            {{ $t('components.assistant.AssistantOnlyView.return_home') }}
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
      <div class="flex items-center gap-1.5 overflow-x-auto border-b px-3 py-1.5">
        <div
          role="tablist"
          :aria-label="$t('components.assistant.AssistantOnlyView.sessions_tab')"
          :aria-owns="sessionTabOwns"
          class="contents"
          data-testid="assistant-only-tab-bar"
        ></div>
        <div
          v-for="tab in tabsStore.tabs"
          :key="tab.tabId"
          class="flex shrink-0 items-center gap-1"
        >
            <button
              type="button"
              :ref="(el) => setTabButtonRef(tab.tabId, el)"
              class="assistant-only-tab"
              :class="{ active: tab.sessionId === store.activeSessionId }"
              :data-testid="`assistant-only-tab-${tab.tabId}`"
              :id="`assistant-only-tab-button-${tab.tabId}`"
              role="tab"
              :aria-selected="tab.sessionId === store.activeSessionId ? 'true' : 'false'"
              :aria-controls="panelsVisible ? 'assistant-only-panel-chat' : undefined"
              :title="sessionTitle(tab.sessionId)"
              @click="selectTab(tab)"
              @keydown.left.prevent="onSessionTablistKeydown($event, tab)"
              @keydown.right.prevent="onSessionTablistKeydown($event, tab)"
              @keydown.home.prevent="onSessionTablistKeydown($event, tab)"
              @keydown.end.prevent="onSessionTablistKeydown($event, tab)"
            >
              <span class="max-w-[180px] truncate">{{ sessionTitle(tab.sessionId) }}</span>
              <span
                v-if="tab.sessionId === store.activeSessionId && store.isStreaming"
                class="assistant-live-dot"
                :title="$t('components.assistant.AssistantOnlyView.live')"
                :aria-label="$t('components.assistant.AssistantOnlyView.live')"
              />
            </button>
            <button
              type="button"
              class="assistant-only-tab-close"
              :data-testid="`assistant-only-tab-close-${tab.tabId}`"
              :aria-label="$t('components.assistant.AssistantOnlyView.close_tab')"
              :title="$t('components.assistant.AssistantOnlyView.close_tab')"
              @click="closeTab(tab)"
            >
              <X class="h-3 w-3" aria-hidden="true" />
            </button>
          </div>
        <button
          type="button"
          class="assistant-only-new-tab"
          data-testid="assistant-only-new-tab"
          :title="$t('components.assistant.AssistantOnlyView.new_tab')"
          :aria-label="$t('components.assistant.AssistantOnlyView.new_tab')"
          @click="startNew"
        >
          <Plus class="h-3.5 w-3.5" aria-hidden="true" />
        </button>
      </div>

      <div
        role="tablist"
        :aria-label="$t('components.assistant.AssistantOnlyView.sub_tabs_label')"
        class="flex items-center gap-1 border-b px-3"
        data-testid="assistant-only-subtab-bar"
      >
        <button
          type="button"
          v-for="st in subTabs"
          :key="st.key"
          :ref="(el) => setSubTabButtonRef(st.key, el)"
          class="assistant-only-subtab"
          :class="{ active: subTab === st.key }"
          :data-testid="`assistant-only-subtab-${st.key}`"
          :id="`assistant-only-subtab-${st.key}`"
          role="tab"
          :aria-selected="subTab === st.key ? 'true' : 'false'"
          :aria-controls="panelsVisible ? `assistant-only-panel-${st.key}` : undefined"
          @click="subTab = st.key"
          @keydown.left.prevent="onSubTablistKeydown($event, st.key)"
          @keydown.right.prevent="onSubTablistKeydown($event, st.key)"
          @keydown.home.prevent="onSubTablistKeydown($event, st.key)"
          @keydown.end.prevent="onSubTablistKeydown($event, st.key)"
        >
          {{ $t(`components.assistant.AssistantOnlyView.${st.labelKey}`) }}
        </button>
      </div>

      <div
        v-if="tabsStore.tabs.length === 0"
        class="flex flex-1 items-center justify-center p-8"
        data-testid="assistant-only-empty"
      >
        <div class="space-y-3 text-center">
          <h2 class="text-lg font-semibold">{{ $t('components.assistant.AssistantOnlyView.no_tabs_yet') }}</h2>
          <p class="text-sm text-muted-foreground">{{ $t('components.assistant.AssistantOnlyView.start_new') }}</p>
          <Button size="small" @click="startNew">{{ $t('components.assistant.AssistantOnlyView.start_new') }}</Button>
        </div>
      </div>

      <div
        v-else-if="!store.activeSessionId"
        class="flex flex-1 items-center justify-center p-8"
        data-testid="assistant-only-no-active"
      >
        <div class="space-y-3 text-center">
          <p class="text-sm text-muted-foreground">{{ $t('components.assistant.AssistantOnlyView.select_tab_or_new') }}</p>
          <Button size="small" @click="startNew">{{ $t('components.assistant.AssistantOnlyView.start_new') }}</Button>
        </div>
      </div>

      <div v-else class="flex flex-1 overflow-hidden" data-testid="assistant-only-chat">
        <div class="flex flex-1 flex-col overflow-hidden">
          <div
            v-show="subTab === 'chat'"
            id="assistant-only-panel-chat"
            role="tabpanel"
            :aria-labelledby="activeSessionTabId"
            class="flex flex-1 flex-col overflow-hidden"
          >
            <AssistantChat ref="chatRef" assistantOnly class="flex-1" />
          </div>
          <div
            v-show="subTab === 'skills'"
            id="assistant-only-panel-skills"
            role="tabpanel"
            aria-labelledby="assistant-only-subtab-skills"
            class="flex flex-1 overflow-hidden"
          >
            <AssistantSkillManager />
          </div>
          <div
            v-show="subTab === 'sources'"
            id="assistant-only-panel-sources"
            role="tabpanel"
            aria-labelledby="assistant-only-subtab-sources"
            class="flex flex-1 overflow-hidden"
          >
            <AssistantContextSources />
          </div>
          <div
            v-show="subTab === 'sessions'"
            id="assistant-only-panel-sessions"
            role="tabpanel"
            aria-labelledby="assistant-only-subtab-sessions"
            class="flex flex-1 overflow-hidden"
          >
            <AssistantSessionDrawer @select-session="onSessionSelected" />
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import type { ComponentPublicInstance } from 'vue'
import { useRouter } from 'vue-router'
import Button from 'primevue/button'
import { ArrowLeft, Plus, X } from '@lucide/vue'
import { shortId } from '@/utils/format'
import { useAssistantStore } from '@/composables/useAssistantStore'
import { useAssistantTabsStore, type AssistantTab } from '@/composables/useAssistantTabsStore'
import { usePlanStore } from '@/stores/planStore'
import AssistantChat from '@/components/assistant/AssistantChat.vue'
import AssistantSkillManager from '@/components/assistant/AssistantSkillManager.vue'
import AssistantContextSources from '@/components/assistant/AssistantContextSources.vue'
import AssistantSessionDrawer from '@/components/assistant/AssistantSessionDrawer.vue'

const router = useRouter()
const store = useAssistantStore()
const tabsStore = useAssistantTabsStore()
const planStore = usePlanStore()
const chatRef = ref<InstanceType<typeof AssistantChat> | null>(null)

type SubTabKey = 'chat' | 'skills' | 'sources' | 'sessions'
const subTab = ref<SubTabKey>('chat')

const subTabs = computed<{ key: SubTabKey; labelKey: string }[]>(() => [
  { key: 'chat', labelKey: 'chat_tab' },
  { key: 'skills', labelKey: 'skills_tab' },
  { key: 'sources', labelKey: 'sources_tab' },
  { key: 'sessions', labelKey: 'sessions_tab' },
])

const tabButtonRefs = ref<Record<string, HTMLButtonElement | null>>({})
const subTabButtonRefs = ref<Record<SubTabKey, HTMLButtonElement | null>>({
  chat: null,
  skills: null,
  sources: null,
  sessions: null,
})

function setTabButtonRef(tabId: string, el: Element | ComponentPublicInstance | null) {
  tabButtonRefs.value[tabId] = el as HTMLButtonElement | null
}

function setSubTabButtonRef(key: SubTabKey, el: Element | ComponentPublicInstance | null) {
  subTabButtonRefs.value[key] = el as HTMLButtonElement | null
}

const activeSessionTabId = computed(() => {
  const active = tabsStore.tabs.find(t => t.sessionId === store.activeSessionId)
  return active ? `assistant-only-tab-button-${active.tabId}` : 'assistant-only-subtab-chat'
})

// The tabpanels (#assistant-only-panel-*) only render when there is at least one tab
// AND an active session; omit aria-controls when the controlled panel is absent.
const panelsVisible = computed(() => tabsStore.tabs.length > 0 && !!store.activeSessionId)

// The session tab buttons live outside the tablist container (so the per-tab
// close buttons are not owned by it); the tablist claims them via aria-owns.
const sessionTabOwns = computed(() =>
  tabsStore.tabs.map(t => `assistant-only-tab-button-${t.tabId}`).join(' ') || undefined
)

function onSessionTablistKeydown(event: KeyboardEvent, tab: AssistantTab) {
  const tabs = tabsStore.tabs
  const index = tabs.findIndex(t => t.tabId === tab.tabId)
  if (index === -1) return
  let nextIndex: number
  switch (event.key) {
    case 'ArrowRight':
      nextIndex = (index + 1) % tabs.length
      break
    case 'ArrowLeft':
      nextIndex = (index - 1 + tabs.length) % tabs.length
      break
    case 'Home':
      nextIndex = 0
      break
    case 'End':
      nextIndex = tabs.length - 1
      break
    default:
      return
  }
  event.preventDefault()
  const target = tabs[nextIndex]
  tabButtonRefs.value[target.tabId]?.focus()
  selectTab(target)
}

function onSubTablistKeydown(event: KeyboardEvent, key: SubTabKey) {
  const index = subTabs.value.findIndex(st => st.key === key)
  if (index === -1) return
  let nextIndex: number
  switch (event.key) {
    case 'ArrowRight':
      nextIndex = (index + 1) % subTabs.value.length
      break
    case 'ArrowLeft':
      nextIndex = (index - 1 + subTabs.value.length) % subTabs.value.length
      break
    case 'Home':
      nextIndex = 0
      break
    case 'End':
      nextIndex = subTabs.value.length - 1
      break
    default:
      return
  }
  event.preventDefault()
  const target = subTabs.value[nextIndex]
  subTabButtonRefs.value[target.key]?.focus()
  subTab.value = target.key
}

const assistantUnavailable = computed(() => !planStore.devMode)

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

function selectTab(tab: AssistantTab) {
  if (tab.sessionId === store.activeSessionId) {
    subTab.value = 'chat'
    return
  }
  chatRef.value?.disconnect()
  tabsStore.resumeTab(tab.sessionId)
  subTab.value = 'chat'
}

function closeTab(tab: AssistantTab) {
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
@reference "../style.css";
.assistant-only-tab {
  @apply flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium transition-colors border-b-2 border-transparent;
  color: hsl(var(--muted-foreground));
  max-width: 220px;
}
.assistant-only-tab:hover {
  color: hsl(var(--foreground));
  background-color: hsl(var(--accent));
}
.assistant-only-tab.active {
  color: hsl(var(--primary));
  background-color: hsla(var(--primary) / 0.08);
}
.assistant-only-tab-close {
  @apply flex items-center justify-center rounded p-0.5 transition-colors shrink-0 border-0 bg-transparent;
  color: hsl(var(--muted-foreground));
}
.assistant-only-tab-close:hover {
  color: hsl(var(--foreground));
  background-color: hsl(var(--accent));
}
.assistant-live-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background-color: hsl(var(--primary));
  animation: assistant-live-pulse 1.5s ease-in-out infinite;
}
@keyframes assistant-live-pulse {
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
.assistant-only-new-tab {
  @apply flex items-center justify-center rounded-md p-1.5 transition-colors shrink-0;
  color: hsl(var(--muted-foreground));
}
.assistant-only-new-tab:hover {
  color: hsl(var(--foreground));
  background-color: hsl(var(--accent));
}
.assistant-only-subtab {
  @apply px-2.5 py-1.5 text-xs font-medium transition-colors border-b-2 border-transparent;
  color: hsl(var(--muted-foreground));
}
.assistant-only-subtab:hover {
  color: hsl(var(--foreground));
}
.assistant-only-subtab.active {
  color: hsl(var(--primary));
  border-bottom-color: hsl(var(--primary));
}
</style>
