<template>
  <div
    v-if="store.panelState !== 'closed'"
    class="remy-panel"
    :class="panelClasses"
    :style="panelStyle"
  >
    <!-- eslint-disable-next-line vuejs-accessibility/no-static-element-interactions -- the titlebar is keyboard-accessible via the arrow-key handlers -->
    <div
      class="remy-titlebar"
      @mousedown="startDrag"
      @keydown.up="handleTitlebarArrowKey($event, 0, -1)"
      @keydown.down="handleTitlebarArrowKey($event, 0, 1)"
      @keydown.left="handleTitlebarArrowKey($event, -1, 0)"
      @keydown.right="handleTitlebarArrowKey($event, 1, 0)"
    >
      <div class="flex items-center gap-2 flex-1 min-w-0">
        <template v-if="editingName && store.activeSession">
          <input
            id="remypanel-name-input"
            ref="nameInputRef"
            v-model="editNameValue"
            :aria-label="$t('components.remy.RemyPanel.session_label')"
            class="remy-name-input text-sm font-semibold"
            @keydown.enter="saveName"
            @keydown.escape="cancelEditName"
            @blur="saveName"
            @mousedown.stop
            @click.stop
          />
        </template>
        <template v-else>
          <button type="button"
            class="text-sm font-semibold truncate cursor-pointer hover:opacity-80 bg-transparent border-0 p-0 text-left"
            :title="$t('components.remy.RemyPanel.click_to_rename')"
            @click.stop="startEditName"
            @dblclick.stop="startEditName"
          >
            <template v-if="store.activeSession && store.activeSession.name">
              {{ store.activeSession.name }}
            </template>
            <template v-else-if="store.activeSession">
              {{ $t('components.remy.RemyPanel.session_label') }} {{ store.activeSession.session_number ? '#' + store.activeSession.session_number : shortId(store.activeSession.id) }}
            </template>
            <template v-else>{{ $t('components.remy.RemyPanel.remy') }}</template>
          </button>
        </template>
        <span v-if="store.isStreaming" class="remy-pulse-dot" />
      </div>
      <div class="flex items-center gap-1">
        <button
          v-if="store.activeSessionId && store.messages.length > 0"
          type="button"
          class="remy-titlebar-btn"
          @click="exportTranscript"
          title="Export Transcript"
          :aria-label="$t('components.remy.export_transcript')"
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
            <polyline points="7 10 12 15 17 10" />
            <line x1="12" y1="15" x2="12" y2="3" />
          </svg>
        </button>
        <button
          v-if="store.activeSessionId"
          type="button"
          class="remy-titlebar-btn"
          @click="store.resetSessionPermissions()"
          title="Reset Permissions"
          :aria-label="$t('components.remy.reset_permissions')"
        >
          <svg
            xmlns="http://www.w3.org/2000/svg"
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
          >
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
          </svg>
        </button>
        <button type="button"
          v-if="store.panelState === 'floating'"
          class="remy-titlebar-btn"
          @click="store.setPanelState('docked')"
          title="Dock"
          :aria-label="$t('components.remy.dock_panel')"
        >
          <svg
            xmlns="http://www.w3.org/2000/svg"
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
          >
            <rect x="3" y="3" width="18" height="18" rx="2" />
            <line x1="9" y1="3" x2="9" y2="21" />
          </svg>
        </button>
        <button type="button"
          v-if="store.panelState === 'docked'"
          class="remy-titlebar-btn"
          @click="store.setPanelState('floating')"
          title="Undock"
          :aria-label="$t('components.remy.undock_panel')"
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <rect x="3" y="3" width="18" height="18" rx="2" />
            <path d="M15 3l6 0 0 6" />
            <path d="M21 3l-9 9" />
          </svg>
        </button>
        <button type="button"
          v-if="planStore.featureEnabled('remy_ui_driving')"
          class="remy-titlebar-btn text-xs font-medium px-1.5"
          @click="cycleSpeed"
          :title="`UI Navigation Speed — ${currentSpeedLabel} — ${speedDescriptions[currentSpeed] ?? ''}`"
          :aria-label="`Speed: ${currentSpeedLabel} — ${speedDescriptions[currentSpeed] ?? ''}`"
        >
          <span>{{ speedIcon }}</span><span class="ml-0.5 text-[10px] uppercase tracking-wider">{{ currentSpeedLabel }}</span>
        </button>
        <button type="button"
          v-if="store.panelState !== 'maximised'"
          class="remy-titlebar-btn"
          @click="store.setPanelState('maximised')"
          title="Maximise"
          :aria-label="$t('components.remy.maximise')"
        >
          <svg
            xmlns="http://www.w3.org/2000/svg"
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
          >
            <rect x="3" y="3" width="18" height="18" rx="2" />
          </svg>
        </button>
        <button type="button"
          v-else
          class="remy-titlebar-btn"
          @click="store.setPanelState('docked')"
          title="Minimise"
          :aria-label="$t('components.remy.minimise')"
        >
          <svg
            xmlns="http://www.w3.org/2000/svg"
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
          >
            <rect x="3" y="3" width="18" height="18" rx="2" />
            <line x1="3" y1="12" x2="21" y2="12" />
          </svg>
        </button>
        <button type="button"
          class="remy-titlebar-btn"
          @click="store.setPanelState('closed')"
          title="Close"
          :aria-label="$t('components.remy.close_panel')"
        >
          <svg
            xmlns="http://www.w3.org/2000/svg"
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
          >
            <line x1="18" y1="6" x2="6" y2="18" />
            <line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </button>
      </div>
    </div>

    <div
      v-if="speedFlash"
      class="text-center text-xs text-muted-foreground/60 py-1 px-3 border-b select-none"
    >
      {{ speedFlash }}
    </div>

    <div
      v-if="store.error"
      class="flex items-center justify-between px-3 py-2 text-sm border-b"
      :class="isRateLimitError ? 'text-orange-600 bg-orange-50 border-orange-200' : 'text-destructive bg-destructive/5'"
    >
      <span>{{ store.error }}</span>
      <button type="button"
        class="shrink-0 ml-2 hover:opacity-80"
        :class="isRateLimitError ? 'text-orange-600' : 'text-destructive'"
        @click="store.error = null"
        :aria-label="$t('components.remy.dismiss_error')"
      >
        &times;
      </button>
    </div>
    <div v-if="currentSpeed === 'review' && store.activeSession" class="flex items-center justify-between px-3 py-1.5 text-xs border-b bg-muted/30">
      <span>⏸ {{ $t('components.remy.RemyPanel.stops_after_each_navigation') }}</span>
      <button type="button" class="text-xs font-medium underline hover:no-underline" @click="resumeUiCommands">{{ $t('components.remy.RemyPanel.resume') }}</button>
    </div>
    <div class="remy-body">
      <div class="remy-sidebar" :class="{ open: showSidebar }">
        <RemySessionDrawer
          @close="showSidebar = false"
          @select-session="showSidebar = false"
        />
      </div>
      <div class="remy-main">
          <div class="remy-chat-tabs flex items-center border-b px-2">
            <button type="button"
              class="remy-tab"
              :class="{ active: activeTab === 'chat' }"
              @click="activeTab = 'chat'"
            >
              Chat
            </button>
            <button type="button"
              class="remy-tab"
              :class="{ active: activeTab === 'skills' }"
              @click="activeTab = 'skills'"
            >
              Skills
            </button>
            <button type="button"
              class="remy-tab"
              :class="{ active: activeTab === 'sessions' }"
              @click="activeTab = 'sessions'"
            >
              Sessions
            </button>
            <button type="button"
              class="remy-tab"
              :class="{ active: activeTab === 'sources' }"
              @click="activeTab = 'sources'"
            >
              Sources
            </button>
          </div>
          <RemyChat v-show="activeTab === 'chat'" ref="chatRef" />
          <RemySkillManager v-if="activeTab === 'skills'" />
          <div
            v-show="activeTab === 'sessions'"
            class="flex-1 overflow-auto p-2"
          >
            <RemySessionDrawer @select-session="activeTab = 'chat'" />
          </div>
          <RemyContextSources v-show="activeTab === 'sources'" />
      </div>
    </div>

    <div
      v-if="store.panelState === 'floating' || store.panelState === 'docked'"
      class="remy-resize-handle"
      aria-hidden="true"
      @mousedown="startResize"
    />
  </div>

  <button type="button"
    v-else
    class="remy-floating-btn"
    @click="store.setPanelState('floating')"
    title="Open Remy"
    :aria-label="$t('components.remy.open_remy')"
  >
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
    >
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
    </svg>
  </button>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onUnmounted } from "vue";
import { useStorage } from '@vueuse/core';
import { shortId } from "@/utils/format";
import { useRemyStore } from "@/composables/useRemyStore";
import { useRemyContext } from "@/composables/useRemyContext";
import { setActionSpeed, resumeUiCommands } from "@/composables/useUiCommandExecutor";
import { usePlanStore } from "@/stores/planStore";
import RemyChat from "./RemyChat.vue";
import RemySessionDrawer from "./RemySessionDrawer.vue";
import RemySkillManager from "./RemySkillManager.vue";
import RemyContextSources from "./RemyContextSources.vue";

const store = useRemyStore();
const planStore = usePlanStore();
const { pageContext } = useRemyContext();
watch(
  pageContext,
  (ctx) => {
    store.setPageContext({ ...ctx, entities: [...ctx.entities] });
  },
  { immediate: true },
);
const showSidebar = ref(false);
const activeTab = ref<"chat" | "skills" | "sessions" | "sources">("chat");

const speedLabels = ['review', 'normal', 'lightning']
const speedIcons = ['⏸', '▶', '⚡']
const speedDescriptions: Record<string, string> = {
  review: 'Stops after each navigation so you can review',
  normal: 'Navigates at a pace you can comfortably follow',
  lightning: 'Navigates as fast as possible',
}
const currentSpeed = useStorage('remy-action-speed', 'normal')
const currentSpeedLabel = computed(() => {
  const idx = speedLabels.indexOf(currentSpeed.value)
  return speedLabels[idx >= 0 ? idx : 1]
})
const speedIcon = computed(() => {
  const idx = speedLabels.indexOf(currentSpeed.value)
  return speedIcons[idx >= 0 ? idx : 1]
})
const speedFlash = ref('')
let speedFlashTimer: ReturnType<typeof setTimeout> | null = null
function showSpeedFlash(msg: string) {
  speedFlash.value = msg
  if (speedFlashTimer) clearTimeout(speedFlashTimer)
  speedFlashTimer = setTimeout(() => { speedFlash.value = '' }, 2500)
}
function cycleSpeed() {
  const idx = speedLabels.indexOf(currentSpeed.value)
  const next = speedLabels[(idx + 1) % speedLabels.length]
  currentSpeed.value = next
  setActionSpeed(next)
  const desc = speedDescriptions[next]
  if (desc) showSpeedFlash(`UI Nav: ${speedIcons[speedLabels.indexOf(next)]} ${desc}`)
  if (next !== 'review') resumeUiCommands()
}

const editingName = ref(false)
const editNameValue = ref('')
const nameInputRef = ref<HTMLInputElement | null>(null)

function startEditName() {
  if (!store.activeSession) return
  editNameValue.value = store.activeSession.name || ''
  editingName.value = true
  nextTick(() => {
    nameInputRef.value?.focus()
    nameInputRef.value?.select()
  })
}

async function saveName() {
  if (!store.activeSession || !editingName.value) return
  editingName.value = false
  const newName = editNameValue.value.trim()
  if (!newName || newName === (store.activeSession.name || '')) return
  await store.renameSession(store.activeSession.id, newName)
}

function cancelEditName() {
  editingName.value = false
  editNameValue.value = ''
}

const isRateLimitError = computed(() => {
  return store.error ? store.error.toLowerCase().includes('rate limit') : false
})

// Init speed from localStorage on mount
setActionSpeed(currentSpeed.value)

const DOCKED_MIN_WIDTH = 320
const DOCKED_MAX_WIDTH = 800

const panelClasses = computed(() => ({
  "remy-floating": store.panelState === "floating",
  "remy-docked": store.panelState === "docked",
  "remy-maximised": store.panelState === "maximised",
}));

const panelStyle = computed(() => {
  if (store.panelState === "maximised") return {};
  if (store.panelState === "docked") {
    const clamped = Math.min(Math.max(store.panelSize.width, DOCKED_MIN_WIDTH), DOCKED_MAX_WIDTH)
    return { width: `${clamped}px`, height: "100vh" };
  }
  return {
    left: `${store.panelPosition.x}px`,
    top: `${store.panelPosition.y}px`,
    width: `${store.panelSize.width}px`,
    height: `${store.panelSize.height}px`,
  };
});

const dragging = ref(false);
const dragStart = ref({ x: 0, y: 0, posX: 0, posY: 0 });
const resizing = ref(false);
const resizeStart = ref({ x: 0, y: 0, w: 0, h: 0 });

function startDrag(e: MouseEvent) {
  if (store.panelState !== "floating") return;
  dragging.value = true;
  dragStart.value = {
    x: e.clientX,
    y: e.clientY,
    posX: store.panelPosition.x,
    posY: store.panelPosition.y,
  };
  document.addEventListener("mousemove", onDrag);
  document.addEventListener("mouseup", stopDrag);
}

function onDrag(e: MouseEvent) {
  if (!dragging.value) return;
  store.updatePosition({
    x: Math.max(8, Math.min(dragStart.value.posX + (e.clientX - dragStart.value.x), window.innerWidth - 340)),
    y: Math.max(8, Math.min(dragStart.value.posY + (e.clientY - dragStart.value.y), window.innerHeight - 100)),
  });
}

function stopDrag() {
  dragging.value = false;
  document.removeEventListener("mousemove", onDrag);
  document.removeEventListener("mouseup", stopDrag);
}

function handleTitlebarArrowKey(e: KeyboardEvent, dx: number, dy: number) {
  const tag = (e.target as HTMLElement | null)?.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA") return;
  if (store.panelState !== "floating") return;
  e.preventDefault();
  nudgePanel(dx, dy);
}

function nudgePanel(dx: number, dy: number) {
  if (store.panelState !== "floating") return;
  store.updatePosition({
    x: store.panelPosition.x + dx,
    y: store.panelPosition.y + dy,
  });
}

function startResize(e: MouseEvent) {
  resizing.value = true;
  resizeStart.value = {
    x: e.clientX,
    y: e.clientY,
    w: store.panelSize.width,
    h: store.panelSize.height,
  };
  document.addEventListener("mousemove", onResize);
  document.addEventListener("mouseup", stopResize);
}

function onResize(e: MouseEvent) {
  if (!resizing.value) return;
  const maxW = store.panelState === "docked" ? DOCKED_MAX_WIDTH : window.innerWidth - 16
  store.updateSize({
    width: Math.min(Math.max(DOCKED_MIN_WIDTH, resizeStart.value.w + (e.clientX - resizeStart.value.x)), maxW),
    height: Math.min(Math.max(400, resizeStart.value.h + (e.clientY - resizeStart.value.y)), window.innerHeight - 40),
  });
}

function formatTranscript(): string {
  const session = store.activeSession
  const sessionName = session?.name || (session?.session_number ? `Session #${session.session_number}` : "Remy Chat")
  const date = new Date().toISOString().split("T")[0]
  const lines: string[] = [
    `# ${sessionName}`,
    `Exported: ${date}`,
    `Messages: ${store.messages.length}`,
    "---",
    "",
  ]
  for (const msg of store.messages) {
    const roleLabels: Record<string, string> = {
      user: "**You:**",
      assistant: "**Remy:**",
      tool_use: "**Tool Use:**",
      tool_result: "**Tool Result:**",
      summary: "",
    }
    const label = roleLabels[msg.role] ?? `**${msg.role}:**`
    const content = msg.content ?? ""
    if (label) lines.push(label)
    if (content) lines.push("", content, "")
    if (msg.tool_results_json) {
      lines.push("", "```json", JSON.stringify(msg.tool_results_json, null, 2), "```", "")
    }
    lines.push("---", "")
  }
  return lines.join("\n")
}

function exportTranscript() {
  const text = formatTranscript()
  const session = store.activeSession
  const filename = `remy-transcript-${session?.session_number ?? session?.id ?? "chat"}-${new Date().toISOString().split("T")[0]}.md`
  const blob = new Blob([text], { type: "text/markdown;charset=utf-8" })
  const url = URL.createObjectURL(blob)
  const a = document.createElement("a")
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

function stopResize() {
  resizing.value = false;
  document.removeEventListener("mousemove", onResize);
  document.removeEventListener("mouseup", stopResize);
}

async function handleNewSession() {
  try {
    const session = await store.createSession();
    if (session) {
      if (store.panelState !== 'closed') {
        store.setPanelState("floating");
      }
      activeTab.value = "chat";
    }
  } catch (e) {
    console.error("Failed to create session:", e);
  }
}

function onWindowResize() {
  if (store.panelState === "floating") {
    store.reclampPosition()
  }
}

watch(() => store.requestRename, () => {
  if (store.requestRename > 0) startEditName()
})

onMounted(async () => {
  window.addEventListener("resize", onWindowResize)
  await store.fetchSessions();
  const savedId = store.activeSessionId
  if (savedId && store.sessions.some(s => s.id === savedId)) {
    await store.loadSession(savedId)
  } else if (!store.activeSessionId) {
    await handleNewSession();
  }
});

onUnmounted(() => {
  if (speedFlashTimer) clearTimeout(speedFlashTimer)
  window.removeEventListener("resize", onWindowResize)
  document.removeEventListener("mousemove", onDrag);
  document.removeEventListener("mouseup", stopDrag);
  document.removeEventListener("mousemove", onResize);
  document.removeEventListener("mouseup", stopResize);
});
</script>

<style scoped>
.remy-panel {
  @apply fixed z-40 flex flex-col border rounded-lg shadow-2xl overflow-hidden;
  background-color: hsl(var(--background));
  border-color: hsl(var(--border));
  transition:
    width 150ms ease,
    height 150ms ease,
    left 150ms ease,
    top 150ms ease;
}
.remy-floating {
  border-radius: var(--radius-lg);
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
}
.remy-docked {
  top: 0;
  right: 0;
  border-radius: 0;
  border-top: none;
  border-bottom: none;
  border-right: none;
}
.remy-maximised {
  inset: 0;
  border-radius: 0;
}
.remy-titlebar {
  @apply flex items-center px-3 py-2 border-b select-none cursor-grab;
  background-color: hsl(var(--card));
  border-color: hsl(var(--border));
  min-height: 40px;
}
.remy-titlebar:active {
  cursor: grabbing;
}
.remy-titlebar-btn {
  @apply flex items-center justify-center rounded p-1 transition-colors;
  color: hsl(var(--muted-foreground));
}
.remy-titlebar-btn:hover {
  background-color: hsl(var(--accent));
  color: hsl(var(--foreground));
}
.remy-pulse-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background-color: hsl(var(--primary));
  animation: pulse-dot 1.5s ease-in-out infinite;
}
@keyframes pulse-dot {
  0%,
  100% {
    opacity: 1;
    transform: scale(1);
  }
  50% {
    opacity: 0.5;
    transform: scale(0.8);
  }
}
.remy-body {
  @apply flex flex-1 overflow-hidden;
}
.remy-sidebar {
  @apply w-64 border-r overflow-auto shrink-0 transition-transform;
  background-color: hsl(var(--card));
  border-color: hsl(var(--border));
}
.remy-sidebar:not(.open) {
  display: none;
}
.remy-main {
  @apply flex flex-col flex-1 overflow-hidden;
}
.remy-chat-tabs {
  background-color: hsl(var(--card));
}
.remy-tab {
  @apply px-3 py-2 text-xs font-medium transition-colors border-b-2 border-transparent;
  color: hsl(var(--muted-foreground));
}
.remy-tab:hover {
  color: hsl(var(--foreground));
}
.remy-tab.active {
  color: hsl(var(--primary));
  border-bottom-color: hsl(var(--primary));
}
.remy-floating-btn {
  @apply fixed bottom-6 right-6 z-50 flex items-center justify-center rounded-full shadow-lg transition-all;
  width: 48px;
  height: 48px;
  background-color: hsl(var(--primary));
  color: hsl(var(--primary-foreground));
  border: 1px solid hsla(var(--primary) / 0.3);
}
.remy-floating-btn:hover {
  transform: scale(1.05);
  box-shadow: 0 4px 20px hsla(var(--primary) / 0.3);
}
.remy-resize-handle {
  position: absolute;
  bottom: 0;
  right: 0;
  width: 24px;
  height: 24px;
  cursor: nwse-resize;
  background: linear-gradient(135deg, transparent 50%, hsl(var(--border)) 50%);
}
.remy-name-input {
  @apply rounded px-1 py-0 text-sm font-semibold outline-none flex-1 min-w-0;
  background-color: hsl(var(--background));
  border: 1px solid hsl(var(--ring));
  color: hsl(var(--foreground));
  min-width: 60px;
}
</style>
